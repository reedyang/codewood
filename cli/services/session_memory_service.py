import json
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_global_config_dir, get_app_logger_root, get_app_runtime_attr_name
from ..core.localization import get_display_language, text
from ..core.config.model_providers import (
    DEFAULT_CONTEXT_WINDOW,
    SIMPLE_CHAT_SYSTEM_PROMPT_MIN_CONTEXT_WINDOW,
    parse_context_window,
)
from ..core.console_utils import _ansi_gray
from ..core.logging.app_logging import get_logger

MEMORY_RETRIEVAL_ROUNDS = 3
MEMORY_RETRIEVAL_MSG_MAX_CHARS = 400
MEMORY_RETRIEVAL_QUERY_MAX_CHARS = 2000
MEMORY_FALLBACK_MIN_COSINE_SCORE = 0.55
MEMORY_EXPANSION_MAX_KEYWORD_CHARS = 600
MEMORY_SEMANTIC_MIN_COSINE_SCORE = 0.20
MEMORY_IDENTITY_CLUSTER_TYPES = frozenset({"preference", "identity"})
SESSION_SUMMARY_FIELD_NAMES = ("Goals", "Facts", "Preferences", "Decisions", "Errors", "Next steps")
SESSION_SUMMARY_FACT_SUBFIELDS = ("Paths/Commands/Tool results", "Environment/Workspace", "Errors/Fixes")
SESSION_SUMMARY_FIELD_ITEM_LIMIT = 3
SESSION_SUMMARY_ROLLING_MAX_CHARS = 900
SESSION_SUMMARY_MSG_SNIPPET = 160
SESSION_SUMMARY_LLM_INTERVAL_PAIRS = 6
SESSION_SUMMARY_LLM_MAX_CHARS = 1200
SESSION_SUMMARY_LLM_HISTORY_MSGS = 16
CHAT_RECENT_MESSAGES = 10
CONTEXT_OUTPUT_RESERVE_RATIO = 0.20
CONTEXT_OUTPUT_RESERVE_MIN = 512
CONTEXT_OUTPUT_RESERVE_MAX = 8192
CONTEXT_SAFETY_MARGIN_RATIO = 0.10
CONTEXT_SAFETY_MARGIN_MIN = 256
SYSTEM_BUCKET_RATIO = 0.45
HISTORY_BUCKET_RATIO = 0.35
OP_CONTEXT_BUCKET_RATIO = 0.12

SMALL_CTX_MAX = 16_000
MEDIUM_CTX_MAX = 64_000
AGGRESSIVE_COMPRESS_TRIGGER_PCT = 80
AGGRESSIVE_COMPRESS_TARGET_PCT = 20
AUTO_COMPACT_TRIGGER_PCT = 60
AUTO_COMPACT_TAIL_WINDOW_RATIO = 0.05
CONTEXT_COMPACTION_SUMMARY_PREFIX = "[CONTEXT_COMPACTION_SUMMARY]"


def _message_effective_token_count(msg: Dict[str, Any]) -> Optional[int]:
    """Compute the effective token count for a message dict.

    Priority:
    1. ``_output_tokens`` (API-provided):
       * ``_token_count_includes_reasoning`` = True (DeepSeek): ``output_tokens``.
       * ``_token_count_includes_reasoning`` = False / absent (others): ``output_tokens - reasoning_tokens``.
    2. ``_token_count`` (local estimate, already stored).
    3. ``None`` — caller should fall back to its own estimation.
    """
    output = msg.get("_output_tokens")
    if output is not None:
        if msg.get("_token_count_includes_reasoning"):
            return max(1, int(output))
        reasoning = msg.get("_reasoning_tokens", 0) or 0
        return max(1, int(output) - int(reasoning))
    tc = msg.get("_token_count")
    if isinstance(tc, (int, float)) and int(tc) > 0:
        return int(tc)
    return None


class SessionMemoryService:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        # Token estimation and context assembly were extracted into dedicated
        # collaborators. This service keeps same-named thin wrappers below so
        # existing call sites and tests remain unchanged, while these objects
        # own the real logic (token counting shared across both paths).
        from ..runtime.token_estimator import TokenEstimator
        from ..runtime.llm_context_manager import LLMContextManager

        self.token_estimator = TokenEstimator(self.agent)
        self.llm_context_manager = LLMContextManager(self.agent, self, self.token_estimator)

    def _t(self, key: str, **kwargs: Any) -> str:
        return text(key, get_display_language(self.agent), **kwargs)

    @staticmethod
    def _text_display_width(value: Any) -> int:
        total = 0
        for ch in str(value or ""):
            if not ch or unicodedata.combining(ch):
                continue
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf"):
                continue
            total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return total

    @classmethod
    def _truncate_text_to_display_width(cls, value: Any, max_width: int) -> str:
        limit = max(1, int(max_width or 1))
        out: List[str] = []
        used = 0
        for ch in str(value or ""):
            if not ch:
                continue
            if unicodedata.combining(ch):
                if out:
                    out.append(ch)
                continue
            cat = unicodedata.category(ch)
            ch_w = 0 if cat in ("Cc", "Cf") else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
            if used + ch_w > limit:
                break
            out.append(ch)
            used += ch_w
        return "".join(out)

    @staticmethod
    def _normalize_context_compaction_mode(mode: Any) -> str:
        normalized = str(mode or "").strip().lower()
        return normalized if normalized in {"auto", "manual"} else ""

    @staticmethod
    def _context_compaction_title_key(mode: str) -> str:
        return "compaction.notice.auto" if mode == "auto" else "compaction.notice.manual"

    def _default_context_compaction_title(self, mode: str) -> str:
        return self._t(self._context_compaction_title_key(mode))

    def format_context_compaction_title(self, payload_or_content: Any) -> str:
        payload = payload_or_content if isinstance(payload_or_content, dict) else None
        if payload is None:
            payload = self.parse_context_compaction_summary_content(str(payload_or_content or ""))
        if not isinstance(payload, dict):
            return self._default_context_compaction_title("auto")
        mode = self._normalize_context_compaction_mode(payload.get("mode")) or "auto"
        return self._default_context_compaction_title(mode)

    def format_context_compaction_summary_message(self, payload_or_content: Any) -> str:
        payload = payload_or_content if isinstance(payload_or_content, dict) else None
        if payload is None:
            payload = self.parse_context_compaction_summary_content(str(payload_or_content or ""))
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("summary") or "").strip()

    def build_context_compaction_display_payload(
        self,
        summary_payload_or_content: Any,
    ) -> Dict[str, str]:
        title = self.format_context_compaction_title(summary_payload_or_content).strip()
        body = self.format_context_compaction_summary_message(summary_payload_or_content).strip()
        text = title if not body else f"{title}\n\n{body}"
        return {
            "title": title,
            "body": body,
            "text": text,
        }

    def _model_visible_path_text(self, raw_path: Any) -> str:
        fallback = "(hidden internal runtime directory)"
        if not raw_path:
            return fallback
        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            return fallback
        repo_root = getattr(self.agent, "_self_repo_root", None)
        if repo_root:
            try:
                if resolved.relative_to(Path(repo_root).resolve()) is not None:
                    return fallback
            except Exception:
                pass
        return str(resolved)

    def _model_visible_workspace_directory_text(self) -> str:
        workspace_root = getattr(self.agent, "workspace_root", None)
        if workspace_root:
            visible = self._model_visible_path_text(workspace_root)
            if visible != "(hidden internal runtime directory)":
                return visible
        workspace_config_dir = getattr(self.agent, "workspace_config_dir", None)
        if workspace_config_dir:
            visible = self._model_visible_path_text(workspace_config_dir)
            if visible != "(hidden internal runtime directory)":
                return visible
        return "(hidden internal runtime directory)"

    def _context_usage_state_key(self) -> str:
        chat_id = str(getattr(self.agent, "active_chat_id", "") or "").strip()
        provider = str(getattr(self.agent, "provider", "") or "").strip().lower()
        model_name = str(getattr(self.agent, "model_name", "") or "").strip().lower()
        try:
            context_window = int(
                parse_context_window((getattr(self.agent, "params", None) or {}).get("context_window"))
                or DEFAULT_CONTEXT_WINDOW
            )
        except Exception:
            context_window = DEFAULT_CONTEXT_WINDOW
        plan_mode = "plan" if bool(getattr(self.agent, "_plan_mode_sticky", False)) else "agent"
        memory_enabled = "mem1" if bool(getattr(self.agent, "memory_enabled", True)) else "mem0"
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        size = len(hist)
        last_role = ""
        last_content = ""
        if hist:
            try:
                last = hist[-1] if isinstance(hist[-1], dict) else {}
            except Exception:
                last = {}
            if isinstance(last, dict):
                last_role = str(last.get("role") or "").strip().lower()
                last_content = str(last.get("content") or "")
                if len(last_content) > 120:
                    last_content = last_content[-120:]
        return (
            f"{chat_id}|{provider}:{model_name}|ctx={context_window}|{plan_mode}|"
            f"{memory_enabled}|{size}|{last_role}|{last_content}"
        )

    @staticmethod
    def _normalize_summary_fragment(text: str, max_chars: int = SESSION_SUMMARY_MSG_SNIPPET) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return ""
        value = value.strip(" \t\r\n-•*")
        value = re.sub(r"^[\[(]{1,2}", "", value).strip()
        value = re.sub(r"[\])]{1,2}$", "", value).strip()
        if len(value) > max_chars:
            value = value[: max(1, max_chars - 1)].rstrip() + "…"
        return value

    @staticmethod
    def _summary_fragment_buckets(fragment: str, role: str) -> List[str]:
        text = str(fragment or "").strip().lower()
        if not text:
            return []

        buckets: List[str] = []

        def _push(name: str) -> None:
            if name not in buckets:
                buckets.append(name)

        if re.search(r"(path|paths|file|files|folder|directory|repo|command|commands|tool|tools|result|results|output|stdout|stderr|flag|flags|arg|args|shell|script|rg\b|cat\b|ls\b|find\b|python -m|git\b|powershell|cmd\.exe|bash\b|terminal)", text, re.I):
            _push("Paths/Commands/Tool results")

        if re.search(r"(workspace|workdir|work directory|cwd|directory|absolute path|current workspace|repo root|project root|os info|operating system|windows|linux|macos|environment|env|config dir|workspace root|workspace data|skills directory|chat name|chat id|context window)", text, re.I):
            _push("Environment/Workspace")

        if re.search(r"(error|failed|failure|exception|traceback|timeout|not found|cannot|can't|permission denied|denied|invalid|crash|bug|报错|失败|异常|错误|超时|找不到|无法|权限|冲突|阻塞)", text, re.I):
            _push("Errors/Fixes")

        if re.search(r"(fix|fixed|resolve|resolved|repair|repairing|workaround|debug|debugging|retry|retrying|troubleshoot|troubleshooting|patch|patched|change to|moved|revert|rewrite|redesign|调整|修复|解决|改掉|排查|尝试)", text, re.I):
            _push("Errors/Fixes")

        if re.search(r"(next step|next steps|follow up|todo|plan to|going to|will\s|need to|should\s|接下来|下一步|后续|待办|之后|随后|我会|我们将)", text, re.I):
            _push("Next steps")

        if re.search(r"(decided|decision|choose|chose|went with|switched|changed to|moved|fixed|resolved|completed|done|now use|kept|removed|added|改成|决定|选择|切到|挪到|统一|已改|已完成|解决)", text, re.I):
            _push("Decisions")

        if re.search(r"(prefer|avoid|don't want|do not want|must|always|never|prefer to|keep using|stick with|最好|不要|偏好|习惯|约定|默认|固定|统一|请用|请保持)", text, re.I):
            _push("Preferences")

        if re.search(r"(goal|goal:|want to|need to|trying to|task|objective|aim|目标|想要|希望|需要|要把|任务|做)", text, re.I):
            _push("Goals")

        if not buckets:
            if role == "user":
                if re.search(r"(please|please\s|could you|can you|help me|let's|let us|i want|i need|we need|我要|请|帮我|希望|需要|想要)", text, re.I):
                    _push("Goals")
            if not buckets:
                _push("Paths/Commands/Tool results")

        if "Errors/Fixes" in buckets and "Next steps" in buckets:
            return ["Errors/Fixes", "Next steps"]
        return buckets

    def _summarize_recent_message_for_rolling(self, msg: Dict[str, Any]) -> List[Tuple[str, str]]:
        role = str(msg.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            return []
        raw_content = str(msg.get("content") or "")
        if not raw_content.strip():
            return []
        if role == "assistant" and self._is_internal_assistant_history_message(raw_content):
            return []
        if role == "user" and self._is_excluded_user_message_for_model_context(msg):
            return []
        if self._is_builtin_slash_user_message(role, raw_content):
            return []
        fragments = re.split(r"(?:\r?\n|\s*\|\s*|[。！？!?；;]+)", raw_content)
        out: List[Tuple[str, str]] = []
        seen_local: Set[Tuple[str, str]] = set()
        for fragment in fragments:
            normalized = self._normalize_summary_fragment(fragment)
            if not normalized:
                continue
            buckets = self._summary_fragment_buckets(normalized, role)
            if not buckets:
                continue
            for bucket in buckets:
                key = (bucket, normalized.lower())
                if key in seen_local:
                    continue
                seen_local.add(key)
                out.append((bucket, normalized))
                if len(out) >= 2:
                    return out
        if not out:
            fallback = self._normalize_summary_fragment(raw_content, SESSION_SUMMARY_MSG_SNIPPET)
            if fallback:
                out.append(("Facts", fallback))
        return out

    def _render_fixed_field_summary(
        self,
        field_values: Dict[str, List[str]],
        fact_values: Dict[str, List[str]],
        header: str,
    ) -> str:
        lines = [header]
        for field in SESSION_SUMMARY_FIELD_NAMES:
            if field == "Facts":
                fact_parts = []
                for subfield in SESSION_SUMMARY_FACT_SUBFIELDS:
                    values = [v for v in fact_values.get(subfield, []) if str(v or "").strip()]
                    fact_parts.append(f"{subfield}: {'; '.join(values) if values else 'None'}")
                line = f"Facts: {'; '.join(fact_parts)}"
            else:
                values = [v for v in field_values.get(field, []) if str(v or "").strip()]
                line = f"{field}: {'; '.join(values) if values else 'None'}"
            lines.append(line)
        text = "\n".join(lines).strip()
        return self._clip_text_to_token_budget(text, SESSION_SUMMARY_ROLLING_MAX_CHARS)

    def _build_fixed_field_summary_from_history(self, rows: List[Dict[str, Any]], *, header: str) -> str:
        field_values: Dict[str, List[str]] = {field: [] for field in SESSION_SUMMARY_FIELD_NAMES}
        field_seen: Dict[str, Set[str]] = {field: set() for field in SESSION_SUMMARY_FIELD_NAMES}
        fact_values: Dict[str, List[str]] = {field: [] for field in SESSION_SUMMARY_FACT_SUBFIELDS}
        fact_seen: Dict[str, Set[str]] = {field: set() for field in SESSION_SUMMARY_FACT_SUBFIELDS}
        for msg in rows[-8:]:
            for field, fragment in self._summarize_recent_message_for_rolling(msg):
                if field in fact_values:
                    if len(fact_values[field]) >= SESSION_SUMMARY_FIELD_ITEM_LIMIT:
                        continue
                    fragment_key = fragment.lower()
                    if fragment_key in fact_seen[field]:
                        continue
                    fact_values[field].append(fragment)
                    fact_seen[field].add(fragment_key)
                    continue
                if field not in field_values or field == "Facts":
                    continue
                if len(field_values[field]) >= SESSION_SUMMARY_FIELD_ITEM_LIMIT:
                    continue
                fragment_key = fragment.lower()
                if fragment_key in field_seen[field]:
                    continue
                field_values[field].append(fragment)
                field_seen[field].add(fragment_key)
        return self._render_fixed_field_summary(field_values, fact_values, header=header)

    # Token-counter state lives on the shared TokenEstimator. These proxies
    # keep the historical service-level attribute names working (some tests
    # set them directly to exercise the non-blocking resolution contract).
    @property
    def _builtin_token_counter(self) -> Optional[Callable[[str], int]]:
        return self.token_estimator._builtin_token_counter

    @_builtin_token_counter.setter
    def _builtin_token_counter(self, value: Optional[Callable[[str], int]]) -> None:
        self.token_estimator._builtin_token_counter = value

    @property
    def _builtin_token_counter_init_done(self) -> bool:
        return self.token_estimator._builtin_token_counter_init_done

    @_builtin_token_counter_init_done.setter
    def _builtin_token_counter_init_done(self, value: bool) -> None:
        self.token_estimator._builtin_token_counter_init_done = value

    def _start_token_counter_warmup(self) -> None:
        self.token_estimator._start_token_counter_warmup()

    def _resolve_token_counter(self) -> Optional[Callable[[str], int]]:
        custom = getattr(self.agent, "token_estimator", None)
        if callable(custom):
            return custom
        if self._builtin_token_counter_init_done:
            return self._builtin_token_counter
        # Non-blocking: warmup continues in background; foreground falls back
        # to heuristic estimation until tokenizer is ready.
        self._start_token_counter_warmup()
        return None

    def append_chat_message(self, role: str, content: str, tool_calls: Any = None, _internal: bool = False, api_content: Optional[str] = None, context_suffix: Optional[str] = None, cache_stats: Optional[Dict[str, Any]] = None, clean_content: Optional[str] = None, output_tokens: Optional[int] = None, reasoning_tokens: Optional[int] = None, token_count_includes_reasoning: Optional[bool] = None, thinking: Optional[str] = None, thinking_from_content: Optional[bool] = None) -> None:
        r = str(role or "").strip().lower()
        if r not in ("user", "assistant", "tool"):
            return
        should_attach_suffix = False
        if r == "user" and isinstance(context_suffix, str) and context_suffix.strip():
            suffix_attached = False
            for m in reversed(self.agent.conversation_history):
                if (
                    isinstance(m, dict)
                    and str(m.get("role", "")).strip().lower() == "user"
                    and not m.get("_internal")
                ):
                    if "_context_suffix" in m:
                        suffix_attached = True
                        break
                    m["_context_suffix"] = str(context_suffix)
                    self.agent._sync_active_chat_messages()
                    return
            if not suffix_attached:
                should_attach_suffix = True
        message: Dict[str, Any] = {
            "role": r,
            "content": str(content or ""),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if should_attach_suffix:
            message["_context_suffix"] = str(context_suffix)
        if _internal:
            message["_internal"] = True
        if isinstance(clean_content, str) and clean_content != str(content or ""):
            message["_clean_content"] = clean_content
        if isinstance(api_content, str) and api_content:
            message["_api_content"] = api_content
        if isinstance(thinking, str) and thinking:
            message["_thinking"] = thinking
        if thinking_from_content:
            message["_thinking_from_content"] = True
        if isinstance(cache_stats, dict):
            message["_cache_stats"] = cache_stats
        if output_tokens is not None:
            message["_output_tokens"] = output_tokens
            message["_reasoning_tokens"] = reasoning_tokens or 0
            if token_count_includes_reasoning is not None:
                message["_token_count_includes_reasoning"] = token_count_includes_reasoning
        provider = str(getattr(self.agent, "provider", "") or "").strip()
        model_name = str(getattr(self.agent, "model_name", "") or "").strip()
        if provider and model_name:
            message["_model"] = f"{provider}/{model_name}"
        if r == "assistant" and self._is_internal_assistant_history_message(str(content or "")):
            pass
        elif output_tokens is not None:
            # API provided output tokens — _token_count is computed on the fly
            # from _output_tokens - _reasoning_tokens at read time; skip local
            # estimation and the _token_count field entirely.
            pass
        else:
            message["_token_count"] = self._estimate_message_tokens(r, str(content or ""))
        if r == "assistant":
            manager = getattr(self.agent, "_chat_state_manager", None)
            attach = getattr(manager, "attach_pending_plan_to_message", None)
            if callable(attach):
                try:
                    attach(message)
                except Exception:
                    pass
        if isinstance(tool_calls, list) and tool_calls:
            message["tool_calls"] = tool_calls
        # Defensive cleanup: _clean_content should never duplicate raw content.
        if "_clean_content" in message and str(message["_clean_content"] or "") == str(message.get("content") or ""):
            del message["_clean_content"]
        self.agent.conversation_history.append(message)
        self.agent._sync_active_chat_messages()
        if r == "user":
            self.agent._maybe_schedule_auto_chat_name()

    def build_context_compaction_summary_content(
        self,
        *,
        summary: str,
        mode: str,
        covered_message_count: int,
        output_tokens: Optional[int] = None,
        reasoning_tokens: Optional[int] = None,
    ) -> str:
        payload = {
            "kind": "context_compaction_summary",
            "summary": str(summary or "").strip(),
            "mode": str(mode or "").strip().lower() or "manual",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "covered_message_count": max(0, int(covered_message_count or 0)),
        }
        # Output-token accounting of the compaction model call. Recording it on
        # the summary lets post-compaction context-usage math count the summary
        # precisely (instead of estimating from its character length), since the
        # cache anchor that normally anchors the cumulative total is stale across
        # a compaction boundary.
        if isinstance(output_tokens, (int, float)) and int(output_tokens) > 0:
            payload["output_tokens"] = int(output_tokens)
        if isinstance(reasoning_tokens, (int, float)) and int(reasoning_tokens) > 0:
            payload["reasoning_tokens"] = int(reasoning_tokens)
        return CONTEXT_COMPACTION_SUMMARY_PREFIX + json.dumps(payload, ensure_ascii=False)

    def parse_context_compaction_summary_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(CONTEXT_COMPACTION_SUMMARY_PREFIX):
            return None
        body = text[len(CONTEXT_COMPACTION_SUMMARY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "context_compaction_summary":
            return None
        return payload

    def is_context_compaction_summary_message(self, msg: Any) -> bool:
        if not isinstance(msg, dict):
            return False
        if str(msg.get("role") or "").strip().lower() != "assistant":
            return False
        return isinstance(
            self.parse_context_compaction_summary_content(str(msg.get("content") or "")),
            dict,
        )

    def _context_compaction_summary_for_model(self, content: str) -> str:
        payload = self.parse_context_compaction_summary_content(content)
        if not isinstance(payload, dict):
            return ""
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            return ""
        created_at = str(payload.get("created_at") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        header = "[Context summary]"
        meta: List[str] = []
        if mode:
            meta.append(f"mode={mode}")
        if created_at:
            meta.append(f"created_at={created_at}")
        if meta:
            header += " " + "; ".join(meta)
        return header + "\n" + summary

    def _is_excluded_user_message_for_model_context(self, msg: Dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        role = str(msg.get("role") or "").strip().lower()
        if role != "user":
            return False
        return bool(msg.get("exclude_from_model_context", False))

    def _is_internal_assistant_history_message(self, content: str) -> bool:
        raw = str(content or "")
        if not raw:
            return False
        if self.parse_context_compaction_summary_content(raw) is not None:
            return True
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
        if callable(parse_slash_result):
            try:
                if isinstance(parse_slash_result(raw), dict):
                    return True
            except Exception:
                pass
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        if callable(parse_worked_summary):
            try:
                if isinstance(parse_worked_summary(raw), dict):
                    return True
            except Exception:
                pass
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        if callable(parse_direct_result):
            try:
                if isinstance(parse_direct_result(raw), dict):
                    return True
            except Exception:
                pass
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)
        if callable(parse_interrupted):
            try:
                if isinstance(parse_interrupted(raw), dict):
                    return True
            except Exception:
                pass
        return False

    def mark_latest_unanswered_user_message_for_cancel(self) -> int:
        """
        Fallback marker used when task-id-based marking cannot be applied.
        Marks the latest unanswered user message globally.
        """
        idx = self._latest_unanswered_user_message_index_global()
        if idx < 0:
            return 0
        try:
            msg_obj = self.agent.conversation_history[idx]
        except Exception:
            return 0
        if not isinstance(msg_obj, dict):
            return 0
        if str(msg_obj.get("role") or "").strip().lower() != "user":
            return 0
        if bool(msg_obj.get("exclude_from_model_context", False)):
            return 0
        msg_obj["exclude_from_model_context"] = True
        try:
            self.agent._sync_active_chat_messages()
        except Exception:
            pass
        return 1

    def _latest_unanswered_user_message_index_global(self) -> int:
        """
        Fallback selector: find the latest user message that has no later *real* assistant
        reply in history, ignoring slash/internal bookkeeping entries.
        """
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        if not hist:
            return -1
        seen_assistant_after = False
        for idx in range(len(hist) - 1, -1, -1):
            msg = hist[idx]
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role == "assistant":
                if self._is_internal_assistant_history_message(str(msg.get("content") or "")):
                    continue
                seen_assistant_after = True
                continue
            if role != "user":
                continue
            if self._is_excluded_user_message_for_model_context(msg):
                continue
            raw = str(msg.get("content") or "")
            if self._is_builtin_slash_user_message(role, raw):
                continue
            if seen_assistant_after:
                continue
            return idx
        return -1

    def _is_builtin_slash_user_message(self, role: str, content: str) -> bool:
        norm_role = str(role or "").strip().lower()
        if norm_role != "user":
            return False
        raw = str(content or "")
        parse_slash_user = getattr(self.agent, "_parse_internal_slash_user_history_content", None)
        cmd = ""
        if callable(parse_slash_user):
            try:
                cmd = str(parse_slash_user(raw) or "").strip()
            except Exception:
                cmd = ""
        text = cmd if cmd else raw
        return str(text or "").strip().startswith("/")

    def _context_eligible_history(self) -> List[Dict[str, Any]]:
        hist = list(getattr(self.agent, "conversation_history", None) or [])

        def _context_eligible_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                if role not in ("user", "assistant", "tool"):
                    continue
                if role == "user" and self._is_excluded_user_message_for_model_context(item):
                    continue
                if self._is_builtin_slash_user_message(role, str(item.get("content") or "")):
                    continue
                out.append(item)
            return out

        try:
            chat = self.agent._find_chat_by_id(getattr(self.agent, "active_chat_id", ""))
        except Exception:
            chat = None
        if not isinstance(chat, dict):
            return _context_eligible_messages(hist)
        chat_messages = chat.get("messages")
        if not isinstance(chat_messages, list) or not chat_messages:
            return _context_eligible_messages(hist)
        return _context_eligible_messages(chat_messages)

    def _context_eligible_history_with_indices(self) -> List[Tuple[int, Dict[str, Any]]]:
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        out: List[Tuple[int, Dict[str, Any]]] = []
        for idx, item in enumerate(hist):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            if role == "user" and self._is_excluded_user_message_for_model_context(item):
                continue
            if self._is_builtin_slash_user_message(role, str(item.get("content") or "")):
                continue
            out.append((idx, item))
        return out

    def latest_compaction_summary_index(self, history: Optional[List[Dict[str, Any]]] = None) -> int:
        rows = list(history if history is not None else self._context_eligible_history())
        for idx in range(len(rows) - 1, -1, -1):
            if self.is_context_compaction_summary_message(rows[idx]):
                return idx
        return -1

    def history_for_regular_context(self) -> List[Dict[str, Any]]:
        rows = self._context_eligible_history()
        idx = self.latest_compaction_summary_index(rows)
        if idx >= 0:
            return rows[idx:]
        return rows

    def _history_with_indices_for_regular_context(self) -> List[Tuple[int, Dict[str, Any]]]:
        rows = self._context_eligible_history_with_indices()
        for idx in range(len(rows) - 1, -1, -1):
            if self.is_context_compaction_summary_message(rows[idx][1]):
                return rows[idx:]
        return rows

    def _normalize_history_content_for_model(self, role: str, content: str, message: Optional[Dict[str, Any]] = None) -> str:
        text = str(content or "")
        norm_role = str(role or "").strip().lower()
        if not text:
            return ""
        # Restore stored memory context for user messages (injected on creation, persisted for cache prefix)
        if norm_role == "user" and isinstance(message, dict):
            mem_ctx = message.get("_memory_context", "")
            if mem_ctx and isinstance(mem_ctx, str) and mem_ctx.strip():
                text = mem_ctx.strip() + "\n" + text
        if norm_role == "assistant":
            compact_summary = self._context_compaction_summary_for_model(text)
            if compact_summary:
                return compact_summary
        parse_user_cmd = getattr(self.agent, "_parse_direct_shell_user_history_content", None)
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        is_direct_aborted = getattr(self.agent, "_is_direct_shell_result_aborted", None)
        normalize_aborted = getattr(self.agent, "_normalize_aborted_direct_shell_stdout_for_history", None)
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)

        if norm_role == "user" and callable(parse_user_cmd):
            try:
                cmd = str(parse_user_cmd(text) or "").strip()
            except Exception:
                cmd = ""
            if cmd:
                return f"[User direct command] !{cmd}"

        if norm_role == "assistant" and callable(parse_direct_result):
            try:
                payload = parse_direct_result(text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                executed = str(payload.get("executed_command") or "").strip()
                rc_raw = payload.get("return_code")
                try:
                    rc_num = int(rc_raw)
                    rc_text = str(rc_num)
                except Exception:
                    rc_num = None
                    rc_text = str(rc_raw)
                out = str(payload.get("stdout") or "")
                err = str(payload.get("stderr") or "")
                merged = out + err
                aborted = False
                if callable(is_direct_aborted):
                    try:
                        aborted = bool(is_direct_aborted(payload))
                    except Exception:
                        aborted = False
                if aborted and callable(normalize_aborted):
                    try:
                        merged = str(normalize_aborted(merged) or "")
                    except Exception:
                        merged = out + err
                success = False
                if not aborted:
                    if rc_num is None:
                        success = False
                    else:
                        success = rc_num == 0
                status = "interrupted_by_user=true" if aborted else "interrupted_by_user=false"
                success_text = "true" if success else "false"
                header = (
                    "[Command result] "
                    f"command={executed or '<empty>'}; return_code={rc_text}; "
                    f"executed_success={success_text}; {status}"
                )
                body = merged.strip("\r\n")
                return f"{header}\n{body}" if body else header

        if norm_role == "assistant" and callable(parse_interrupted):
            try:
                payload2 = parse_interrupted(text)
            except Exception:
                payload2 = None
            if isinstance(payload2, dict):
                interrupted_kind = str(payload2.get("interrupted_kind") or "task").strip()
                reason = str(payload2.get("reason") or "user_interrupt").strip()
                detail = str(payload2.get("detail") or "").strip()
                msg = (
                    f"[Session interruption event] kind={interrupted_kind}; reason={reason}; "
                    "The task was interrupted by the user. Do not auto-resume unless the user explicitly asks to continue."
                )
                if detail:
                    msg += f"\nInterrupted task: {detail}"
                return msg

        return text

    def _is_internal_slash_history_message(self, role: str, content: str) -> bool:
        norm_role = str(role or "").strip().lower()
        text = str(content or "")
        if norm_role == "user":
            parse_slash_user = getattr(self.agent, "_parse_internal_slash_user_history_content", None)
            if callable(parse_slash_user):
                try:
                    return bool(str(parse_slash_user(text) or "").strip())
                except Exception:
                    return False
            return False
        if norm_role == "assistant":
            if self.parse_context_compaction_summary_content(text) is not None:
                return True
            parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
            if callable(parse_slash_result):
                try:
                    payload = parse_slash_result(text)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    return True
            parse_model_tool_result = getattr(self.agent, "_parse_model_tool_result_history_content", None)
            if callable(parse_model_tool_result):
                try:
                    model_payload = parse_model_tool_result(text)
                except Exception:
                    model_payload = None
                if isinstance(model_payload, dict):
                    return True
            parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
            if callable(parse_worked_summary):
                try:
                    worked_payload = parse_worked_summary(text)
                except Exception:
                    worked_payload = None
                if isinstance(worked_payload, dict):
                    return True
            return False
        return False

    def _latest_interruption_context_line(
        self, source_history: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        rows = list(source_history if source_history is not None else self._context_eligible_history())
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        is_direct_aborted = getattr(self.agent, "_is_direct_shell_result_aborted", None)
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)
        for msg in reversed(rows):
            role = str(msg.get("role") or "").strip().lower()
            if role != "assistant":
                continue
            content = str(msg.get("content") or "")
            if callable(parse_interrupted):
                try:
                    evt = parse_interrupted(content)
                except Exception:
                    evt = None
                if isinstance(evt, dict):
                    detail = str(evt.get("detail") or "").strip()
                    line = "The most recent task execution was interrupted by the user (ESC). Do not auto-resume the interrupted task unless the user explicitly asks."
                    if detail:
                        line += f" Interrupted task: {detail}"
                    return line
            if callable(parse_direct_result):
                try:
                    dr = parse_direct_result(content)
                except Exception:
                    dr = None
                if isinstance(dr, dict):
                    aborted = False
                    if callable(is_direct_aborted):
                        try:
                            aborted = bool(is_direct_aborted(dr))
                        except Exception:
                            aborted = False
                    if aborted:
                        cmd = str(dr.get("executed_command") or "").strip()
                        rc = dr.get("return_code")
                        return (
                            "The most recent direct command execution was forcibly terminated by the user; "
                            f"command={cmd or '<empty>'}; return_code={rc}。"
                            "Do not treat that command as fully and successfully completed."
                        )
        return ""

    def update_session_summary_rolling(self) -> None:
        hist = list(self._context_eligible_history() or [])
        s = self._build_fixed_field_summary_from_history(hist, header="[Session excerpt]")
        self.agent._session_summary_rolling = s

    def session_summary_for_retrieval(self) -> str:
        llm = (self.agent._session_summary_llm or "").strip()
        if llm:
            cap = min(1200, SESSION_SUMMARY_LLM_MAX_CHARS)
            return f"[Session summary]\n{llm[:cap]}"
        roll = (self.agent._session_summary_rolling or "").strip()
        if roll:
            return f"[Session excerpt]\n{roll}"
        return ""

    def maybe_refresh_session_summary_llm(self) -> None:
        all_hist = list(getattr(self.agent, "conversation_history", None) or [])
        filtered_hist = [
            m for m in all_hist
            if not self._is_internal_slash_history_message(
                str(m.get("role") or "").strip().lower(),
                str(m.get("content") or ""),
            )
        ]
        pairs = len(filtered_hist) // 2
        if pairs < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
            return
        if self.agent._last_llm_summary_pair_count > 0:
            if pairs - self.agent._last_llm_summary_pair_count < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
                return
        hist = filtered_hist[-SESSION_SUMMARY_LLM_HISTORY_MSGS:]
        lines: List[str] = []
        for msg in hist:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            tag = "User" if role == "user" else "Assistant"
            c = str(msg.get("content") or "")[:500].replace("\n", " ")
            lines.append(f"{tag}: {c}")
        blob = "\n".join(lines)
        if not blob.strip():
            return
        try:
            raw = self.agent.call_ai(
                "Below is a recent excerpt from this session. Produce a dense six-field summary using Goals / Facts / Preferences / Decisions / Errors / Next steps, and make Facts use the three buckets Paths/Commands/Tool results, Environment/Workspace, and Errors/Fixes, then follow the system instructions.\n\n"
                + blob,
                context="",
                stream=False,
                session_summary_mode=True,
            )
        except Exception:
            return
        if not isinstance(raw, str):
            return
        text = raw.strip()
        if text.startswith("❌") or text.startswith("calling the model"):
            return
        text = text.replace("```", "").strip()
        if not text:
            return
        self.agent._session_summary_llm = text[:SESSION_SUMMARY_LLM_MAX_CHARS]
        self.agent._last_llm_summary_pair_count = pairs

    def build_memory_retrieval_query(self, user_input: str) -> str:
        def _clip(text: str, n: int) -> str:
            t = (text or "").strip()
            if not t:
                return ""
            if len(t) <= n:
                return t
            return t[: max(1, n - 1)] + "…"

        return _clip((user_input or "").strip(), MEMORY_RETRIEVAL_QUERY_MAX_CHARS)

    @staticmethod
    def memory_row_sort_key(r: Dict[str, Any]) -> Tuple[int, float, int]:
        mt = (r.get("memory_type") or "").lower()
        tier = (r.get("tier") or "").lower()
        cluster = 0 if mt in MEMORY_IDENTITY_CLUSTER_TYPES else 1
        tr = 0 if tier == "durable" else (1 if tier == "episodic" else 2)
        try:
            ca = float(r.get("created_at") or 0)
        except (TypeError, ValueError):
            ca = 0.0
        return (cluster, -ca, tr)

    @staticmethod
    def user_input_emphasizes_memory_or_identity(user_input: str) -> bool:
        s = (user_input or "").strip()
        if not s:
            return False
        needles = (
            "search memory", "use memory", "check memory", "your memory", "experiential memory", "do not remember", "named you before",
            "given name", "nickname", "form of address", "who am I", "who are you", "what is my name", "previously gave you", "agreed before", "do you remember",
        )
        return any(x in s for x in needles)

    def memory_dialogue_excerpt_for_expansion(self) -> str:
        per_msg = MEMORY_RETRIEVAL_MSG_MAX_CHARS
        rounds = MEMORY_RETRIEVAL_ROUNDS

        def _clip(text: str, n: int) -> str:
            t = (text or "").strip()
            if not t:
                return ""
            if len(t) <= n:
                return t
            return t[: max(1, n - 1)] + "…"

        hist = list(getattr(self.agent, "conversation_history", None) or [])
        want = rounds * 2
        tail = hist[-want:] if len(hist) >= want else hist[:]
        if tail and (tail[-1].get("role") or "") == "user":
            tail = tail[:-1]
        while tail and (tail[0].get("role") or "") != "user":
            tail = tail[1:]
        if len(tail) % 2 == 1:
            tail = tail[1:]
        lines: List[str] = []
        for msg in tail:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            raw_content = str(msg.get("content") or "")
            if self._is_internal_slash_history_message(role, raw_content):
                continue
            content = _clip(raw_content, per_msg)
            if not content:
                continue
            tag = "User" if role == "user" else "Assistant"
            lines.append(f"[{tag}] {content}")
        return "\n".join(lines).strip()

    def memory_expansion_reference_block(self) -> str:
        pref = self.session_summary_for_retrieval()
        dia = self.memory_dialogue_excerpt_for_expansion()
        parts: List[str] = []
        if pref:
            parts.append(pref)
        if dia:
            parts.append("[Recent dialogue excerpt]\n" + dia)
        return "\n\n".join(parts).strip()

    @staticmethod
    def parse_memory_expansion_json(text: str) -> Optional[Dict[str, Any]]:
        raw = (text or "").strip()
        if not raw or raw.startswith("❌") or raw.startswith("calling the model"):
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```\s*$", "", raw)
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            start = raw.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(raw[start : i + 1])
                            except Exception:
                                data = None
                            break
        if not isinstance(data, dict):
            return None
        keys = ("keywords", "aliases", "entities", "topics", "preferences_hint")
        out: Dict[str, Any] = {}
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                cleaned = [str(x).strip() for x in v if str(x).strip()][:12]
                out[k] = cleaned[:10]
            else:
                out[k] = []
        if not any(out.values()):
            return None
        return out

    def memory_expansion_keywords_query_string(self, expansion: Dict[str, Any]) -> str:
        chunks: List[str] = []
        for k in ("keywords", "aliases", "entities", "topics", "preferences_hint"):
            for s in expansion.get(k) or []:
                s = str(s).strip()[:80]
                if s:
                    chunks.append(s)
        joined = " ".join(chunks).strip()
        if len(joined) > MEMORY_EXPANSION_MAX_KEYWORD_CHARS:
            joined = joined[: MEMORY_EXPANSION_MAX_KEYWORD_CHARS]
        return joined

    def should_run_memory_query_expansion(
        self, rows_sem: List[Dict[str, Any]], rows_boost: List[Dict[str, Any]], identity_mode: bool
    ) -> bool:
        if not getattr(self.agent, "memory_fallback_expansion_enabled", True):
            return False
        if identity_mode and rows_boost:
            return False
        if not rows_sem:
            return True
        scores = [float(r.get("similarity") or 0) for r in rows_sem]
        if not scores:
            return True
        return max(scores) < MEMORY_FALLBACK_MIN_COSINE_SCORE

    def run_memory_expansion_llm(self, user_input: str) -> Optional[Dict[str, Any]]:
        ref = self.memory_expansion_reference_block()
        body = (user_input or "").strip()
        payload = ((ref + "\n\n---\n\n") if ref else "") + "[Current user question] (Use only to extract retrieval terms and synonymous entities. Do not answer the user directly.)\n" + body
        try:
            raw = self.agent.call_ai(payload, context="", stream=False, memory_query_expansion_mode=True)
        except Exception:
            get_logger().exception("Experiential memory: query expansion LLM call failed")
            return None
        if not isinstance(raw, str):
            return None
        return self.parse_memory_expansion_json(raw)

    def memory_rows_for_prompt(self, user_input: str) -> List[Dict[str, Any]]:
        raw_ui = (user_input or "").strip()
        q = self.build_memory_retrieval_query(user_input)
        if not q.strip():
            return []
        sk = self.agent._memory_scope_key()

        rows_boost: List[Dict[str, Any]] = []
        identity_mode = self.user_input_emphasizes_memory_or_identity(raw_ui)
        if identity_mode:
            for bq in (
                "user preference nickname name form-of-address assistant identity naming agreement",
                "preference nickname identity assistant name form of address",
            ):
                rows_boost.extend(self.agent.memory_service.search_memories(bq, top_k=5, scope_key=sk))
        seen_b: Set[str] = set()
        boost_uniq: List[Dict[str, Any]] = []
        for r in rows_boost:
            rid = str(r.get("id") or "").strip()
            if rid and rid not in seen_b:
                seen_b.add(rid)
                boost_uniq.append(r)
        rows_boost = boost_uniq

        rows_sem = self.agent.memory_service.search_memories(q, top_k=6, scope_key=sk)

        rows_exp: List[Dict[str, Any]] = []
        _mem_log = get_logger()
        if self.should_run_memory_query_expansion(rows_sem, rows_boost, identity_mode):
            max_sim = max((float(r.get("similarity") or 0) for r in rows_sem), default=0.0)
            if not rows_sem:
                _mem_log.info("Experiential memory: triggered query expansion fallback (primary retrieval had no hits)")
            else:
                _mem_log.info(
                    "Experiential memory: triggered query expansion fallback (primary retrieval weak max_sim=%.2f < %.2f)",
                    max_sim,
                    MEMORY_FALLBACK_MIN_COSINE_SCORE,
                )
            exp = self.run_memory_expansion_llm(raw_ui)
            if exp:
                kw = self.memory_expansion_keywords_query_string(exp)
                if kw:
                    q2 = (q.strip() + "\n\n[Expanded retrieval terms]\n" + kw).strip()
                    rows_exp = self.agent.memory_service.search_memories(q2, top_k=8, scope_key=sk)
                    _mem_log.info(
                        "Experiential memory: query expansion executed (about %d chars of expanded terms, %d secondary retrieval results)",
                        len(kw),
                        len(rows_exp),
                    )
                else:
                    _mem_log.info("Experiential memory: query expansion produced no usable keywords (all slots empty)")
            else:
                _mem_log.info("Experiential memory: query expansion did not take effect (model response could not be parsed or call failed)")

        # Filter semantic results: strong matches (>= 0.55) always pass; weak
        # matches (0.20-0.55) must share meaningful characters to avoid noise.
        def _passes_semantic_filter(r: Dict[str, Any]) -> bool:
            sim = float(r.get("similarity") or 0)
            if sim >= MEMORY_FALLBACK_MIN_COSINE_SCORE:
                return True
            if sim < MEMORY_SEMANTIC_MIN_COSINE_SCORE:
                return False
            # Weak match: require meaningful character overlap (entity sharing)
            q_raw = (user_input or "").lower()
            m_raw = ((r.get("title") or "") + " " + (r.get("content") or "")).lower()
            q_cn = {c for c in q_raw if '\u4e00' <= c <= '\u9fff'}
            m_cn = {c for c in m_raw if '\u4e00' <= c <= '\u9fff'}
            common = q_cn & m_cn
            common -= set("的了是在有不你我他她它你们这那什么怎么吧吗啊呢哈的和与或及")
            return len(common) >= 2
        rows_sem = [r for r in rows_sem if _passes_semantic_filter(r)]
        # Query expansion results are speculative (LLM-generated queries) and
        # more prone to drift, so use a higher threshold.
        rows_exp = [r for r in rows_exp if float(r.get("similarity") or 0) >= MEMORY_FALLBACK_MIN_COSINE_SCORE]

        seen: Set[str] = set()
        merged: List[Dict[str, Any]] = []

        def _add_rows(rs: List[Dict[str, Any]]) -> None:
            for r in rs:
                if len(merged) >= 12:
                    return
                rid = str(r.get("id") or "").strip()
                if not rid or rid in seen:
                    continue
                merged.append(r)
                seen.add(rid)

        _add_rows(rows_boost)
        _add_rows(rows_sem)
        _add_rows(rows_exp)

        def _from_recent_item(item: Dict[str, Any]) -> Dict[str, Any]:
            prev = item.get("preview") or ""
            ca = item.get("created_at")
            try:
                ca_f = float(ca) if ca is not None else 0.0
            except (TypeError, ValueError):
                ca_f = 0.0
            return {
                "id": item.get("id"),
                "title": item.get("title") or "",
                "content": (prev if isinstance(prev, str) else str(prev))[:600],
                "tier": item.get("tier") or "",
                "memory_type": item.get("memory_type") or "",
                "source": item.get("source") or "",
                "system_note": None,
                "created_at": ca_f,
            }

        # Only include recent memories when semantic retrieval found at least
        # some relevant results, and cap at 3 to avoid filling context with
        # unrelated recent activity.
        if len(merged) > 0:
            recent = self.agent.memory_service.list_recent(limit=10, scope_key=sk)
            recent_added = 0
            for item in recent:
                if len(merged) >= 12 or recent_added >= 3:
                    break
                rid = str(item.get("id") or "").strip()
                if not rid or rid in seen:
                    continue
                merged.append(_from_recent_item(item))
                seen.add(rid)
                recent_added += 1

        merged.sort(key=self.memory_row_sort_key)
        return merged[:12]

    def memory_context_for_user_message(self, user_input: str, max_chars: int = 2400) -> str:
        if not self.agent._ensure_memory_service():
            return ""
        try:
            rq = self.build_memory_retrieval_query(user_input)
            if not rq.strip():
                return ""
            rows = self.memory_rows_for_prompt(user_input)
            if not rows:
                return ""
            lines: List[str] = []
            total = 0
            for r in rows:
                mid = str(r.get("id", "") or "")
                mid_tag = f"(id={mid[:12]})" if mid else ""
                block = f"[Memory: {r.get('title', '')}]{mid_tag} {r.get('content', '')[:500]}"
                if r.get("system_note"):
                    block += f" [System note: {r['system_note'][:200]}]"
                ca = r.get("created_at")
                if ca is not None:
                    try:
                        ts = float(ca)
                        if ts > 0:
                            block += f" [Recorded at: {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}]"
                    except (TypeError, ValueError, OSError, OverflowError):
                        pass
                if total + 1 + len(block) > max_chars:
                    break
                lines.append(block)
                total += 1 + len(block)
                mid = str(r.get("id") or "").strip()
                if mid:
                    try:
                        self.agent.memory_service.touch_memory(mid)
                    except Exception:
                        pass
            return "\n".join(lines) if lines else ""
        except Exception:
            return ""

    def _estimate_text_tokens(self, text: str) -> int:
        return self.token_estimator.estimate_text_tokens(text)

    def _estimate_message_tokens(self, role: str, content: str) -> int:
        return self.token_estimator.estimate_message_tokens(role, content)

    def _store_context_usage_snapshot(self, context_window: int, total_input_tokens: int) -> None:
        self.llm_context_manager._store_context_usage_snapshot(context_window, total_input_tokens)

    def _persist_context_usage_snapshot(self) -> None:
        self.llm_context_manager._persist_context_usage_snapshot()

    def _clip_text_to_token_budget(self, text: str, max_tokens: int) -> str:
        return self.token_estimator.clip_text_to_token_budget(text, max_tokens)

    def _first_user_requirement(self, fallback: str) -> str:
        return self.llm_context_manager._first_user_requirement(fallback)

    def _context_token_budgets(self) -> Dict[str, int]:
        return self.llm_context_manager._context_token_budgets_impl()

    def _should_use_simple_chat_context(self, budgets: Dict[str, Any]) -> bool:
        return self.llm_context_manager._should_use_simple_chat_context(budgets)

    def _build_simple_chat_messages(
        self,
        user_input: str,
        budgets: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        return self.llm_context_manager._build_simple_chat_messages(user_input, budgets)

    def _software_development_prompt_append(self) -> str:
        return self.llm_context_manager._software_development_prompt_append()

    def _summarize_history_excerpt(self, rows: List[Dict[str, Any]], summary_budget: int) -> str:
        return self.llm_context_manager._summarize_history_excerpt(rows, summary_budget)

    def _build_history_messages_by_budget(
        self,
        history_budget: int,
        summary_budget: int,
        assistant_clip_tokens: int,
        source_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        return self.llm_context_manager._build_history_messages_by_budget(
            history_budget,
            summary_budget,
            assistant_clip_tokens,
            source_history=source_history,
        )

    def _message_cost_for_tail_budget(self, msg: Dict[str, Any]) -> int:
        return self.llm_context_manager._message_cost_for_tail_budget(msg)

    def _format_compaction_banner_line(self, text: str) -> str:
        return self.llm_context_manager._format_compaction_banner_line(text)

    def _compaction_output_stream(self) -> Any:
        return self.llm_context_manager._compaction_output_stream()

    def _terminal_columns_for_compaction_banner(self) -> int:
        return self.llm_context_manager._terminal_columns_for_compaction_banner()

    def _terminal_columns_from_compaction_streams(self, stream: Any) -> int:
        return self.llm_context_manager._terminal_columns_from_compaction_streams(stream)

    def _write_compaction_raw(self, text: str) -> None:
        self.llm_context_manager._write_compaction_raw(text)

    def _print_compaction_banner(self, text: str) -> int:
        return self.llm_context_manager._print_compaction_banner(text)

    def _clear_compaction_banner(self, rendered_lines: int) -> None:
        self.llm_context_manager._clear_compaction_banner(rendered_lines)

    def _auto_tail_count_within_budget(self, rows: List[Tuple[int, Dict[str, Any]]], max_tokens: int) -> int:
        return self.llm_context_manager._auto_tail_count_within_budget(rows, max_tokens)

    def _compaction_candidate_rows(self, mode: str) -> List[Tuple[int, Dict[str, Any]]]:
        return self.llm_context_manager._compaction_candidate_rows(mode)

    def build_compaction_user_input(self, mode: str) -> str:
        return self.llm_context_manager.build_compaction_user_input(mode)

    def compact_context(self, mode: str = "manual") -> bool:
        return self.llm_context_manager.compact_context(mode)

    def check_and_compact_if_needed(self, user_input_hint: str = "", context_hint: str = "") -> bool:
        return self.llm_context_manager.check_and_compact_if_needed(
            user_input_hint=user_input_hint, context_hint=context_hint
        )

    def maybe_auto_compact_before_user_message(self, user_input: str) -> bool:
        return self.llm_context_manager.maybe_auto_compact_before_user_message(user_input)

    def refresh_context_usage_snapshot(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
        expected_state_key: str = "",
    ) -> None:
        self.llm_context_manager._refresh_context_usage_snapshot_impl(
            user_input_hint=user_input_hint,
            context_hint=context_hint,
            expected_chat_id=expected_chat_id,
            expected_state_key=expected_state_key,
        )

    def schedule_context_usage_refresh_async(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
    ) -> bool:
        return self.llm_context_manager.schedule_context_usage_refresh_async(
            user_input_hint=user_input_hint,
            context_hint=context_hint,
            expected_chat_id=expected_chat_id,
        )

    def build_regular_task_messages(self, user_input: str, context: str = "") -> Tuple[List[Dict[str, Any]], bool]:
        return self.llm_context_manager.build_regular_task_messages(user_input, context)
