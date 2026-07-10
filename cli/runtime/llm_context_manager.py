"""Assembles the model-visible context (system + history + current turn).

This manager owns the "context packing" logic: token budgeting, regular vs.
simple-chat message assembly, aggressive compression, usage-snapshot
accounting, and context compaction. Token counting is delegated to a shared
:class:`~cli.runtime.token_estimator.TokenEstimator` so usage accounting here
and in the session-memory service stays consistent.

Memory/history/summary helpers continue to live on ``SessionMemoryService``;
this manager reaches them through ``self.session_memory`` (and, for any
attribute it does not define, via ``__getattr__`` proxying). The service keeps
thin same-named wrappers so existing call sites and tests are unchanged.
"""

from __future__ import annotations

import json
import platform
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .prompt_preprocessor import preprocess_prompt

from ..config.app_info import (
    get_app_global_config_dir,
    get_app_logger_root,
    get_app_prompt_name,
    get_app_prompt_slug_kebab,
    get_app_runtime_attr_name,
)
from ..core.config.model_providers import (
    DEFAULT_CONTEXT_WINDOW,
    SIMPLE_CHAT_SYSTEM_PROMPT_MIN_CONTEXT_WINDOW,
    parse_context_window,
)
from ..services import session_memory_service as _sms

# Reference the patchable module-level symbols through the session-memory
# service module so existing tests that patch
# ``cli.services.session_memory_service.get_logger`` / ``._ansi_gray`` continue
# to intercept logging and banner rendering after the logic moved here.
def get_logger():  # type: ignore[no-redef]
    return _sms.get_logger()


def _ansi_gray(text: str) -> str:  # type: ignore[no-redef]
    return _sms._ansi_gray(text)

CONTEXT_OUTPUT_RESERVE_RATIO = 0.20
CONTEXT_OUTPUT_RESERVE_MIN = 512
CONTEXT_OUTPUT_RESERVE_MAX = 8192
CONTEXT_SAFETY_MARGIN_RATIO = 0.10
CONTEXT_SAFETY_MARGIN_MIN = 256
SMALL_CTX_MAX = 16_000
MEDIUM_CTX_MAX = 64_000
AGGRESSIVE_COMPRESS_TRIGGER_PCT = 80
AGGRESSIVE_COMPRESS_TARGET_PCT = 20
AUTO_COMPACT_TRIGGER_PCT = 60
AUTO_COMPACT_TAIL_WINDOW_RATIO = 0.05


class LLMContextManager:
    """Builds and budgets the LLM request context for a turn."""

    def __init__(self, agent: Any, session_memory: Any, token_estimator: Any) -> None:
        self.agent = agent
        self.session_memory = session_memory
        self.token_estimator = token_estimator
        self._context_usage_refresh_lock = threading.Lock()
        self._context_usage_refresh_inflight = False
        self._context_usage_refresh_pending: Optional[Dict[str, str]] = None
        self._context_compaction_lock = threading.Lock()
        self._software_development_prompt_cache: Optional[str] = None

    def __getattr__(self, name: str) -> Any:
        # Proxy any helper not defined here (memory/history/summary utilities,
        # localized text, path helpers, etc.) to the session-memory service.
        # __getattr__ only fires for attributes missing on this instance, so it
        # never shadows the methods this manager defines.
        session_memory = self.__dict__.get("session_memory")
        if session_memory is None:
            raise AttributeError(name)
        return getattr(session_memory, name)

    # --- Token estimation (shared logic) -------------------------------------
    # These delegate to the service's same-named wrappers so test monkeypatches
    # on the service instance remain authoritative.
    def _estimate_text_tokens(self, text: str) -> int:
        return self.session_memory._estimate_text_tokens(text)

    def _estimate_message_tokens(self, role: str, content: str) -> int:
        return self.session_memory._estimate_message_tokens(role, content)

    def _clip_text_to_token_budget(self, text: str, max_tokens: int) -> str:
        return self.session_memory._clip_text_to_token_budget(text, max_tokens)

    # --- Budgeting -----------------------------------------------------------
    def _context_token_budgets_impl(self) -> Dict[str, int]:
        ctx_window = parse_context_window(
            ((getattr(self.agent, "params", None) or {}).get("context_window")),
            default_value=DEFAULT_CONTEXT_WINDOW,
        )
        if ctx_window <= SMALL_CTX_MAX:
            profile = "small"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.50, 0.26, 0.14, 0.10
            memory_share_ratio = 0.42
            assistant_clip_tokens = 8000
        elif ctx_window <= MEDIUM_CTX_MAX:
            profile = "medium"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.45, 0.35, 0.12, 0.08
            memory_share_ratio = 0.45
            assistant_clip_tokens = 16000
        else:
            profile = "large"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.38, 0.48, 0.10, 0.06
            memory_share_ratio = 0.55
            assistant_clip_tokens = 32000

        output_reserve = int(ctx_window * CONTEXT_OUTPUT_RESERVE_RATIO)
        output_reserve = max(CONTEXT_OUTPUT_RESERVE_MIN, min(output_reserve, CONTEXT_OUTPUT_RESERVE_MAX))
        safety_margin = max(CONTEXT_SAFETY_MARGIN_MIN, int(ctx_window * CONTEXT_SAFETY_MARGIN_RATIO))
        input_budget = max(512, ctx_window - output_reserve - safety_margin)
        system_budget = max(200, int(input_budget * system_ratio))
        history_budget = max(120, int(input_budget * history_ratio))
        op_context_budget = max(80, int(input_budget * op_ratio))
        history_summary_budget = max(80, int(input_budget * summary_ratio))
        return {
            "profile": profile,
            "context_window": ctx_window,
            "input_budget": input_budget,
            "system_budget": system_budget,
            "history_budget": history_budget,
            "op_context_budget": op_context_budget,
            "history_summary_budget": history_summary_budget,
            "memory_share_ratio": int(memory_share_ratio * 100),
            "assistant_clip_tokens": assistant_clip_tokens,
        }

    def _should_use_simple_chat_context(self, budgets: Dict[str, Any]) -> bool:
        try:
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        except Exception:
            ctx_window = DEFAULT_CONTEXT_WINDOW
        return ctx_window < SIMPLE_CHAT_SYSTEM_PROMPT_MIN_CONTEXT_WINDOW

    def _estimate_tool_schemas_tokens(self) -> int:
        """Estimate the token overhead of the tool schemas sent via API."""
        tool_specs = list(getattr(self.agent, "tool_specs", []) or [])
        if not tool_specs:
            return 0
        try:
            raw = json.dumps(tool_specs, ensure_ascii=False, sort_keys=True)
            return self._estimate_message_tokens("system", raw)
        except Exception:
            return 0

    def _build_simple_chat_messages(
        self,
        user_input: str,
        budgets: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        user_text = str(user_input or "")
        user_tokens = self._estimate_message_tokens("user", user_text)
        tool_schemas_tokens = self._estimate_tool_schemas_tokens()
        input_budget = int(budgets.get("input_budget") or 1024)
        # Reserve tokens for the small-model system prompt.
        system_budget = int(budgets.get("system_budget") or 0)
        if system_budget > 0:
            sys_prompt = self._build_small_model_system_prompt()
            sys_tokens = self._estimate_message_tokens("system", sys_prompt)
            if sys_tokens > system_budget:
                sys_prompt = self._clip_text_to_token_budget(sys_prompt, system_budget)
                sys_tokens = self._estimate_message_tokens("system", sys_prompt)
            if sys_tokens > 0:
                input_budget = max(120, input_budget - sys_tokens)
        else:
            sys_prompt = ""
            sys_tokens = 0
        history_budget = max(0, input_budget - user_tokens)
        history_messages, history_stats = self._build_history_messages_by_budget(
            history_budget,
            int(budgets.get("history_summary_budget") or 80),
            int(budgets.get("assistant_clip_tokens") or 180),
            source_history=self.history_for_regular_context(),
        )
        messages: List[Dict[str, Any]] = list(history_messages)
        if sys_prompt:
            messages.insert(0, {"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": user_text})

        try:
            history_tokens = sum(
                self._estimate_message_tokens(str(m.get("role") or ""), str(m.get("content") or ""))
                for m in history_messages
            )
            total_input_tokens = int(sys_tokens + history_tokens + user_tokens + tool_schemas_tokens)
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
            usage_pct = max(0, min(999, int(round((total_input_tokens * 100.0) / max(1, ctx_window)))))
            self.agent._last_context_usage_percent_precompression = usage_pct
            self.agent._last_context_aggressive_compression_applied = False
            self._store_context_usage_snapshot(ctx_window, total_input_tokens)
            if bool(getattr(self.agent, "_force_current_input_as_requirement_once", False)):
                self.agent._force_current_input_as_requirement_once = False
            get_logger().info(
                "context-pack profile=simple-chat ctx_window=%s input_budget=%s system=%s history=%s user=%s "
                "history_trimmed_assistant=%s history_summary_messages=%s history_dropped=%s",
                budgets.get("context_window"),
                budgets.get("input_budget"),
                sys_tokens,
                history_tokens,
                user_tokens,
                history_stats.get("assistant_trimmed", 0),
                history_stats.get("summary_messages", 0),
                history_stats.get("dropped_messages", 0),
            )
        except Exception:
            pass
        return messages, True

    def _build_small_model_system_prompt(self) -> str:
        """Build a compact system prompt for small-context models (< 64k).

        Combines the simplified base system prompt, simplified domain prompt,
        simplified tools catalog, and basic runtime metadata.

        Always loads the simplified prompt template directly rather than
        relying on the cached ``_base_system_prompt`` (which may have been
        set during bootstrap with a different model profile).
        """
        parts: List[str] = []
        try:
            from .context.base_system_prompt import _prompts_root
            prompt_path = _prompts_root() / "small" / "system_prompt.md"
            raw = prompt_path.read_text(encoding="utf-8").strip()
            base = preprocess_prompt(raw, {"os": platform.system()})
            base = (base
                .replace("{{APP_NAME}}", get_app_prompt_name())
                .replace("{{APP_SLUG_KEBAB}}", get_app_prompt_slug_kebab())
            )
        except Exception:
            base = ""
        if base:
            parts.append(base)
        domain = self._software_development_prompt_append().strip()
        if domain:
            parts.append(domain)
        # Inject the simplified tools catalog so the model knows available
        # tools even in simple-chat mode.
        try:
            from .prompt_composer import build_tools_prompt_append
            tools_text = build_tools_prompt_append(self.agent).strip()
            if tools_text:
                parts.append(tools_text)
        except Exception:
            pass
        workspace_root = self._model_visible_workspace_directory_text()
        if workspace_root:
            parts.append(f"Current workspace root: {workspace_root}")
        return "\n\n".join(parts)

    def _software_development_prompt_append(self) -> str:
        cached = getattr(self, "_software_development_prompt_cache", None)
        if isinstance(cached, str):
            return cached
        small_model = bool(getattr(self.agent, "_small_model", False))
        if small_model:
            prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "small" / "domain_software_development.md"
        else:
            prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "domain_software_development.md"
        try:
            raw = prompt_path.read_text(encoding="utf-8").strip()
            text = preprocess_prompt(raw, {"os": platform.system()})
        except Exception:
            text = ""
        if text:
            text = "\n\n" + text + "\n"
        self._software_development_prompt_cache = text
        return text

    def _summarize_history_excerpt(self, rows: List[Dict[str, Any]], summary_budget: int) -> str:
        if not rows or summary_budget <= 0:
            return ""
        lines: List[str] = []
        for item in rows:
            role = "U" if str(item.get("role") or "").strip().lower() == "user" else "A"
            c = str(item.get("content") or "").replace("\n", " ").strip()
            if not c:
                continue
            lines.append(f"{role}:{c[:180]}")
            if len(lines) >= 12:
                break
        if not lines:
            return ""
        summary = "[History summary]\n" + " | ".join(lines)
        return self._clip_text_to_token_budget(summary, summary_budget)

    def _build_history_messages_by_budget(
        self,
        history_budget: int,
        summary_budget: int,
        assistant_clip_tokens: int,
        source_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        hist = list(source_history if source_history is not None else self._context_eligible_history())
        if not hist or history_budget <= 0:
            return [], {"assistant_trimmed": 0, "summary_messages": 0, "dropped_messages": 0}

        # Only messages at or after the last cache-stats message contribute
        # to the token count — earlier ones are covered by the cumulative
        # input tokens from the cache-stats anchor.  Pre-scan to find that
        # position so we only compute _token_count for messages that matter.
        last_cache_src_idx = -1
        for _i, _msg in enumerate(hist):
            if isinstance(_msg, dict) and isinstance(_msg.get("_cache_stats"), dict):
                last_cache_src_idx = _i

        normalized: List[Dict[str, Any]] = []
        assistant_trimmed = 0
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        for idx, msg in enumerate(hist):
            role = str(msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            raw_content = str(msg.get("content") or "")
            # When the message carries ``_api_content`` (the exact text that was
            # sent to the provider), use it for the model context so replayed
            # history prefixes match upstream cache units.
            api_content = msg.get("_api_content")
            if isinstance(api_content, str) and api_content.strip():
                raw_content = api_content
            # ``_context_suffix`` carries auto-injected content (evidence block,
            # local time, etc.) that was previously stored as a separate _internal
            # user message. Append it to the message content so the model still
            # receives it during history replay.
            context_suffix = msg.get("_context_suffix")
            if role == "user" and isinstance(context_suffix, str) and context_suffix.strip():
                if raw_content.strip():
                    raw_content = raw_content + "\n\n" + context_suffix.strip()
                else:
                    raw_content = context_suffix.strip()
            if role == "user" and self._is_excluded_user_message_for_model_context(msg):
                continue
            if role == "user" and self._is_builtin_slash_user_message(role, raw_content):
                continue
            if role == "assistant" and self.parse_context_compaction_notice_content(raw_content) is not None:
                continue
            if role == "assistant" and callable(parse_slash_result):
                try:
                    slash_payload = parse_slash_result(raw_content)
                except Exception:
                    slash_payload = None
                if isinstance(slash_payload, dict):
                    continue
            if role == "assistant" and callable(parse_worked_summary):
                try:
                    worked_payload = parse_worked_summary(raw_content)
                except Exception:
                    worked_payload = None
                if isinstance(worked_payload, dict):
                    continue
            content = self._normalize_history_content_for_model(role, raw_content, message=msg)
            if role == "assistant":
                before = content
                content = self._clip_text_to_token_budget(content, assistant_clip_tokens)
                if content != before:
                    assistant_trimmed += 1
            entry: Dict[str, Any] = {"role": role, "content": content}
            if role == "assistant":
                msg_model = str(msg.get("_model") or "").strip()
                if msg_model:
                    entry["_model"] = msg_model
                cs = msg.get("_cache_stats")
                if isinstance(cs, dict) and cs:
                    entry["_cache_stats"] = cs
            from ..services.session_memory_service import _message_effective_token_count
            tc = _message_effective_token_count(msg)
            if tc is not None:
                entry["_token_count"] = tc
            elif idx >= last_cache_src_idx:
                local = self._estimate_message_tokens(role, raw_content)
                msg["_token_count"] = local
                entry["_token_count"] = local
            normalized.append(entry)

        if not normalized:
            return [], {"assistant_trimmed": assistant_trimmed, "summary_messages": 0, "dropped_messages": 0}

        def _total_cost(items: List[Dict[str, Any]]) -> int:
            return sum(self._message_cost_for_tail_budget(i) for i in items)

        working = list(normalized)
        dropped_for_summary: List[Dict[str, Any]] = []
        summary_message: Optional[Dict[str, Any]] = None

        # Stage 2: compress older dialogue into one summary message before dropping whole messages.
        target_without_summary = max(24, history_budget - max(40, summary_budget))
        while len(working) > 2 and _total_cost(working) > target_without_summary:
            dropped_for_summary.append(working.pop(0))
        if dropped_for_summary:
            summary_text = self._summarize_history_excerpt(dropped_for_summary, summary_budget)
            if summary_text:
                summary_message = {"role": "assistant", "content": summary_text}
                working.insert(0, summary_message)

        # Stage 3: still too big -> drop whole oldest messages.
        dropped_messages = 0
        while len(working) > 1 and _total_cost(working) > history_budget:
            if summary_message is not None and len(working) > 2:
                working.pop(1)
            else:
                working.pop(0)
            dropped_messages += 1

        if summary_message is not None and working and working[0] is summary_message and _total_cost(working) > history_budget:
            other_cost = _total_cost(working[1:])
            allowed = max(16, history_budget - other_cost - 6)
            clipped_summary = self._clip_text_to_token_budget(str(summary_message.get("content") or ""), allowed)
            if clipped_summary:
                summary_message["content"] = clipped_summary
            else:
                working.pop(0)

        if not working and normalized:
            # keep one latest message as last resort
            last = normalized[-1]
            max_content_tokens = max(16, history_budget - 8)
            working = [
                {
                    "role": str(last.get("role") or "assistant"),
                    "content": self._clip_text_to_token_budget(str(last.get("content") or ""), max_content_tokens),
                }
            ]

        stats = {
            "assistant_trimmed": assistant_trimmed,
            "summary_messages": 1 if summary_message else 0,
            "dropped_messages": dropped_messages,
        }
        return working, stats

    def _message_cost_for_tail_budget(self, msg: Dict[str, Any]) -> int:
        from ..services.session_memory_service import _message_effective_token_count
        tc = _message_effective_token_count(msg)
        if tc is not None:
            return tc
        role = str(msg.get("role") or "").strip().lower()
        content = self._normalize_history_content_for_model(role, str(msg.get("content") or ""), message=msg)
        return self._estimate_message_tokens(role, content)

    def _history_tokens_cumulative(self, messages: List[Dict[str, Any]]) -> int:
        """Compute total history tokens using the cumulative formula.

        The last message with ``_cache_stats`` provides a cumulative anchor
        for everything before that assistant response. Its ``input_tokens``
        (or cache hit/miss sum) covers the prompt side, and its own response
        contributes ``_output_tokens - _reasoning_tokens`` when available.
        Messages after that anchor are counted individually.

        Internal-bookkeeping messages (task-worked summaries, compaction
        notices, slash results, etc.) are skipped — they are never sent to
        the model and should not inflate the usage display.
        """
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)

        def _is_internal_assistant(msg: Dict[str, Any]) -> bool:
            role = str(msg.get("role") or "").strip().lower()
            if role != "assistant":
                return False
            raw = str(msg.get("content") or "")
            if not raw:
                return False
            if self.parse_context_compaction_notice_content(raw) is not None:
                return True
            if callable(parse_slash_result):
                try:
                    if isinstance(parse_slash_result(raw), dict):
                        return True
                except Exception:
                    pass
            if callable(parse_worked_summary):
                try:
                    if isinstance(parse_worked_summary(raw), dict):
                        return True
                except Exception:
                    pass
            return False

        last_cache_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m.get("_cache_stats"), dict):
                last_cache_idx = i

        from ..services.session_memory_service import _message_effective_token_count

        def _message_cost(msg: Dict[str, Any]) -> int:
            tc = _message_effective_token_count(msg)
            if tc is not None:
                return int(tc)
            role = str(msg.get("role") or "").strip().lower()
            content = self._normalize_history_content_for_model(role, str(msg.get("content") or ""), message=msg)
            return self._estimate_message_tokens(role, content)

        total = 0
        start_idx = 0
        if 0 <= last_cache_idx < len(messages):
            anchor = messages[last_cache_idx]
            cs = anchor["_cache_stats"]
            if "input_tokens" in cs:
                total += int(cs["input_tokens"] or 0)
            else:
                total += int(cs.get("prompt_cache_hit_tokens") or 0) + int(cs.get("prompt_cache_miss_tokens") or 0)

            # Anchor input tokens already cover prior prompt context. Add the
            # anchor response exactly once, preferring provider usage data.
            total += _message_cost(anchor)
            start_idx = last_cache_idx + 1
        else:
            # No cache-stats anchor from the API — use the most recent
            # compaction summary as the starting point so compacted messages
            # before it are not counted a second time.
            for i, m in enumerate(messages):
                if self.is_context_compaction_summary_message(m):
                    start_idx = i

        for i, m in enumerate(messages):
            if _is_internal_assistant(m):
                continue
            if i < start_idx:
                continue
            total += _message_cost(m)
        return total

    def _context_usage_from_chat_record(self) -> int:
        """Compute cumulative history tokens from the active chat record.

        Works on any thread (no session binding required) because it reads
        from the persisted chat record, not conversation_history.
        Falls back to conversation_history when the chat record is unavailable.
        """
        cid = str(getattr(self.agent, "active_chat_id", "") or "").strip()
        chat = self.agent._find_chat_by_id(cid) if cid else None
        if not isinstance(chat, dict):
            hist = list(getattr(self.agent, "conversation_history", None) or [])
            return self._history_tokens_cumulative(hist)
        msgs = list(chat.get("messages") or [])
        return self._history_tokens_cumulative(msgs)

    def _auto_tail_count_within_budget(self, rows: List[Tuple[int, Dict[str, Any]]], max_tokens: int) -> int:
        if not rows or max_tokens <= 0:
            return 0
        total = 0
        tail_count = 0
        pos = len(rows) - 1
        while pos >= 0:
            if self.is_context_compaction_summary_message(rows[pos][1]):
                break
            group_start = pos
            role = str(rows[pos][1].get("role") or "").strip().lower()
            if role == "assistant" and pos - 1 >= 0:
                prev = rows[pos - 1][1]
                if (
                    str(prev.get("role") or "").strip().lower() == "user"
                    and not self.is_context_compaction_summary_message(prev)
                ):
                    group_start = pos - 1
            group = rows[group_start:pos + 1]
            if any(self.is_context_compaction_summary_message(m) for _idx, m in group):
                break
            cost = sum(self._message_cost_for_tail_budget(m) for _idx, m in group)
            if cost <= 0:
                break
            if total + cost > max_tokens:
                break
            total += cost
            tail_count += len(group)
            pos = group_start - 1
        return tail_count

    def _compaction_candidate_rows(self, mode: str) -> List[Tuple[int, Dict[str, Any]]]:
        rows = self._history_with_indices_for_regular_context()
        if not rows:
            return []
        normalized_mode = str(mode or "").strip().lower()
        _ = normalized_mode
        compact_until_pos = len(rows) - 1
        budgets = self._context_token_budgets()
        ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        tail_budget = max(1, int(ctx_window * AUTO_COMPACT_TAIL_WINDOW_RATIO))
        tail_count = self._auto_tail_count_within_budget(rows, tail_budget)
        compact_until_pos = len(rows) - tail_count - 1
        if compact_until_pos < 0:
            return []
        candidates = rows[:compact_until_pos + 1]
        has_new_dialogue = any(
            not self.is_context_compaction_summary_message(m)
            and not self.is_context_compaction_notice_message(m)
            for _idx, m in candidates
        )
        if normalized_mode == "manual" and not has_new_dialogue and tail_count > 0:
            last_group_size = 0
            pos = len(rows) - 1
            while pos >= 0:
                if self.is_context_compaction_summary_message(rows[pos][1]):
                    break
                group_start = pos
                role = str(rows[pos][1].get("role") or "").strip().lower()
                if role == "assistant" and pos - 1 >= 0:
                    prev = rows[pos - 1][1]
                    if (
                        str(prev.get("role") or "").strip().lower() == "user"
                        and not self.is_context_compaction_summary_message(prev)
                    ):
                        group_start = pos - 1
                group = rows[group_start:pos + 1]
                if any(self.is_context_compaction_summary_message(m) for _idx, m in group):
                    break
                last_group_size = len(group)
                break
            if last_group_size > 0 and tail_count > last_group_size:
                compact_until_pos = len(rows) - last_group_size - 1
                if compact_until_pos >= 0:
                    candidates = rows[:compact_until_pos + 1]
                    has_new_dialogue = any(
                        not self.is_context_compaction_summary_message(m)
                        and not self.is_context_compaction_notice_message(m)
                        for _idx, m in candidates
                    )
        if not has_new_dialogue:
            if normalized_mode == "manual" and compact_until_pos < len(rows) - 1:
                return candidates
            return []
        return candidates

    # --- Compaction ----------------------------------------------------------
    def build_compaction_messages(
        self,
        mode: str,
        source_history: List[Dict[str, Any]],
        compact_until_index: int,
    ) -> List[Dict[str, Any]]:
        import os

        _ = compact_until_index
        self.agent._reload_skills()
        try:
            system_prompt = self.agent._compose_system_prompt_snapshot(include_tools=True)
        except Exception:
            system_prompt = str(getattr(self.agent, "system_prompt", "") or "")
        budgets = self._context_token_budgets()
        history_budget = max(160, int(int(budgets.get("input_budget") or 1024) * 0.72))
        summary_budget = max(80, int(history_budget * 0.10))
        assistant_clip = max(120, int(budgets.get("assistant_clip_tokens") or 260))
        history_messages, _stats = self._build_history_messages_by_budget(
            history_budget,
            summary_budget,
            assistant_clip,
            source_history=source_history,
        )
        os_info = os.uname() if hasattr(os, "uname") else os.name
        workspace_root_text = self._model_visible_workspace_directory_text()
        workspace_data_dir_text = self._model_visible_path_text(
            getattr(self.agent, "workspace_config_dir", None)
        )
        workspace_skills_dir = (Path(self.agent.workspace_config_dir) / "skills").resolve()
        default_install_skills_dir = (get_app_global_config_dir() / "skills").resolve()
        runtime_tail_raw = (
            f"Current OS info: {os_info}\n"
            f"Current workspace name: {self.agent.workspace_name}\n"
            f"Current chat name (weak hint, session label only, not this turn's task goal): {self.agent.active_chat_name}\n"
            f"Current workspace root (absolute path): {workspace_root_text}\n"
            f"Current workspace data directory (absolute path): {workspace_data_dir_text}\n"
            f"Default skill install path (absolute path): {default_install_skills_dir}\n"
            f"Current workspace skills directory (absolute path): {workspace_skills_dir}\n"
            "When installing a third-party skill: if the user does not specify an install location, you must use the Default skill install path (absolute path); "
            "use the Current workspace skills directory (absolute path) only when the user explicitly asks to install into the workspace.\n"
        )
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "compact_prompt.md"
        raw = prompt_path.read_text(encoding="utf-8").strip()
        compact_prompt_text = preprocess_prompt(raw, {"os": platform.system()})
        sys_content = (
            f"{str(getattr(self.agent, '_skills_routing_prefix', '') or '')}"
            f"{system_prompt}\n"
            f"{self._software_development_prompt_append()}"
            f"{runtime_tail_raw}"
            f"{compact_prompt_text}\n"
        )
        user_content = (
            f"compact_mode={str(mode or '').strip().lower() or 'manual'}\n"
            "Based on the history above, generate a concise checkpoint handoff summary that can replace those messages."
        )
        return [{"role": "system", "content": sys_content}] + history_messages + [{"role": "user", "content": user_content}]

    def _format_compaction_banner_line(self, text: str) -> str:
        label = f" {str(text or '').strip()} "
        width = self._terminal_columns_for_compaction_banner()
        label_width = self._text_display_width(label)
        if label_width >= width:
            return self._truncate_text_to_display_width(label.strip(), width)
        pad = width - label_width
        left = pad // 2
        right = pad - left
        return ("─" * left) + label + ("─" * right)

    def _compaction_output_stream(self) -> Any:
        stream = sys.stdout
        seen: Set[int] = set()
        while stream is not None:
            sid = id(stream)
            if sid in seen:
                break
            seen.add(sid)
            nxt = getattr(stream, "_primary", None)
            if nxt is None:
                nxt = getattr(stream, "_base_stream", None)
            if nxt is None:
                break
            stream = nxt
        return stream or sys.stdout

    def _terminal_columns_for_compaction_banner(self) -> int:
        stream = self._compaction_output_stream()
        fn_prompt = getattr(self.agent, "_terminal_columns_for_prompt_separator", None)
        if callable(fn_prompt):
            try:
                width0 = int(fn_prompt(default=80) or 0)
                if width0 > 0:
                    return max(1, width0)
            except Exception:
                pass
        if stream is not sys.stdout:
            width_raw = self._terminal_columns_from_compaction_streams(stream)
            if width_raw > 0:
                return max(1, width_raw - 1)
        width_raw = self._terminal_columns_from_compaction_streams(stream)
        if width_raw > 0:
            return width_raw
        return 80

    def _terminal_columns_from_compaction_streams(self, stream: Any) -> int:
        candidates: List[int] = []
        terminal_columns_attr = get_app_runtime_attr_name("terminal_columns")
        for obj in (sys.stdout, stream, sys.__stdout__):
            try:
                fn = getattr(obj, terminal_columns_attr, None)
                if callable(fn):
                    candidates.append(int(fn() or 0))
            except Exception:
                pass
        for obj in (stream, sys.__stdout__, sys.stdout):
            try:
                if hasattr(obj, "fileno"):
                    candidates.append(int(__import__("os").get_terminal_size(obj.fileno()).columns or 0))
            except Exception:
                pass
        for name in ("_terminal_columns_for_line_estimate",):
            fn2 = getattr(self.agent, name, None)
            if callable(fn2):
                try:
                    candidates.append(int(fn2() or 0))
                except Exception:
                    pass
        width = max([c for c in candidates if c > 0], default=80)
        return max(1, int(width))

    def _write_compaction_raw(self, text: str) -> None:
        stream = self._compaction_output_stream()
        try:
            stream.write(str(text or ""))
            stream.flush()
        except Exception:
            try:
                sys.stdout.write(str(text or ""))
                sys.stdout.flush()
            except Exception:
                pass

    def _print_compaction_banner(self, text: str) -> int:
        line = self._format_compaction_banner_line(text)
        self._write_compaction_raw("\n" + _ansi_gray(line) + "\n\n")
        try:
            self.agent._terminal_cursor_at_line_start = True
        except Exception:
            pass
        return 3

    def _clear_compaction_banner(self, rendered_lines: int) -> None:
        rows = max(0, int(rendered_lines or 0))
        if rows <= 0:
            return
        stream = self._compaction_output_stream()
        try:
            if not (hasattr(stream, "isatty") and stream.isatty()):
                return
        except Exception:
            return
        try:
            for _ in range(min(rows, 20)):
                stream.write("\x1b[1A\r\x1b[2K")
            stream.flush()
        except Exception:
            pass

    def compact_context(self, mode: str = "manual") -> bool:
        normalized_mode = str(mode or "").strip().lower() or "manual"
        if normalized_mode not in {"auto", "manual"}:
            normalized_mode = "manual"
        if not self._context_compaction_lock.acquire(blocking=False):
            if normalized_mode == "manual":
                print(self._t("compaction.already_running"))
            return False
        try:
            return self._compact_context_locked(normalized_mode)
        finally:
            self._context_compaction_lock.release()

    def _compact_context_locked(self, mode: str) -> bool:
        candidates_with_idx = self._compaction_candidate_rows(mode)
        if not candidates_with_idx:
            if mode == "manual":
                print(self._t("compaction.no_context"))
            return False
        start_text = self._t("compaction.start.auto") if mode == "auto" else self._t("compaction.start.manual")
        start_banner_lines = self._print_compaction_banner(start_text)
        source_history = [m for _idx, m in candidates_with_idx]
        insert_after_idx = int(candidates_with_idx[-1][0])
        messages = self.build_compaction_messages(mode, source_history, insert_after_idx)
        try:
            raw = self.agent.call_ai(
                "Generate context compaction summary.",
                context="",
                stream=False,
                return_message=False,
                messages_override=messages,
                record_history_override=False,
            )
        except Exception as e:
            get_logger().exception("context compact: model call failed")
            if mode == "manual":
                print(self._t("compaction.failed", error=e))
            return False
        summary = str(raw or "").strip() if isinstance(raw, str) else ""
        if summary.startswith("❌") or summary.startswith("Error calling LLM API") or not summary:
            if mode == "manual":
                print(summary or self._t("compaction.failed_empty_summary"))
            return False
        summary = summary.replace("```", "").strip()
        content = self.build_context_compaction_summary_content(
            summary=summary,
            mode=mode,
            covered_message_count=len(source_history),
        )
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = {
            "role": "assistant",
            "content": content,
            "created_at": created_at,
        }
        notice_msg = {
            "role": "assistant",
            "content": self.build_context_compaction_notice_content(mode=mode),
            "created_at": created_at,
        }
        try:
            self.agent.conversation_history.insert(insert_after_idx + 1, msg)
            self.agent.conversation_history.append(notice_msg)
            self.agent._sync_active_chat_messages()
            self.refresh_context_usage_snapshot(context_hint="context compacted")
        except Exception:
            get_logger().exception("context compact: failed to persist summary")
            if mode == "manual":
                print(self._t("compaction.failed_saving_summary"))
            return False
        self._clear_compaction_banner(start_banner_lines)
        self._print_compaction_banner(self._default_context_compaction_notice_message(mode))
        return True

    def check_and_compact_if_needed(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
    ) -> bool:
        """
        Check current context usage and auto-compact if usage exceeds the
        trigger threshold.  Intended to be called **during** task execution
        (e.g. after a tool-call result is appended to history) so the context
        never silently grows past the trigger limit mid-turn.

        Returns ``True`` if compaction was triggered, ``False`` otherwise.
        This method is a no-op (and returns ``False``) when compaction is
        already running on another thread.
        """
        try:
            self.refresh_context_usage_snapshot(
                user_input_hint=str(user_input_hint or ""),
                context_hint=str(context_hint or ""),
            )
            usage_pct = int(getattr(self.agent, "_last_context_usage_percent", 0) or 0)
            try:
                trigger_pct = int(
                    getattr(self.agent, "auto_compact_trigger_percent", AUTO_COMPACT_TRIGGER_PCT)
                    or AUTO_COMPACT_TRIGGER_PCT
                )
            except Exception:
                trigger_pct = AUTO_COMPACT_TRIGGER_PCT
            trigger_pct = max(1, min(100, trigger_pct))
            if usage_pct < trigger_pct:
                return False
            if not self._compaction_candidate_rows("auto"):
                return False
            return self.compact_context("auto")
        except Exception:
            return False

    def maybe_auto_compact_before_user_message(self, user_input: str) -> bool:
        if not self._context_compaction_lock.acquire(blocking=False):
            return False
        try:
            self.refresh_context_usage_snapshot(user_input_hint=str(user_input or ""))
            usage_pct = int(getattr(self.agent, "_last_context_usage_percent", 0) or 0)
            try:
                trigger_pct = int(getattr(self.agent, "auto_compact_trigger_percent", AUTO_COMPACT_TRIGGER_PCT) or AUTO_COMPACT_TRIGGER_PCT)
            except Exception:
                trigger_pct = AUTO_COMPACT_TRIGGER_PCT
            trigger_pct = max(1, min(100, trigger_pct))
            if usage_pct < trigger_pct:
                return False
            if not self._compaction_candidate_rows("auto"):
                return False
            return self._compact_context_locked("auto")
        finally:
            self._context_compaction_lock.release()

    # --- Usage snapshot ------------------------------------------------------
    def _store_context_usage_snapshot(self, context_window: int, total_input_tokens: int) -> None:
        ctx_window = max(1, int(context_window or DEFAULT_CONTEXT_WINDOW))
        total = max(0, int(total_input_tokens or 0))
        usage_pct = max(0, min(999, int(round((total * 100.0) / ctx_window))))
        self.agent._last_context_window = ctx_window
        self.agent._last_context_input_tokens = total
        self.agent._last_context_usage_percent = usage_pct

    def _persist_context_usage_snapshot(self) -> None:
        persisted = False
        try:
            persist_fn = getattr(self.agent, "_persist_active_chat_usage_snapshot", None)
            if callable(persist_fn):
                persist_fn()
                persisted = True
        except Exception:
            persisted = False
        try:
            sync_fn = getattr(self.agent, "_sync_active_chat_messages", None)
            if callable(sync_fn) and not persisted:
                sync_fn()
        except Exception:
            pass

    def _first_user_requirement(self, fallback: str) -> str:
        hist = self.history_for_regular_context()
        for msg in hist:
            if str(msg.get("role") or "").strip().lower() != "user":
                continue
            if self._is_excluded_user_message_for_model_context(msg):
                continue
            c = str(msg.get("content") or "").strip()
            if self._is_builtin_slash_user_message("user", c):
                continue
            if c:
                return c
        return str(fallback or "").strip()

    def _refresh_context_usage_snapshot_impl(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
        expected_state_key: str = "",
    ) -> None:
        """
        Refresh status-bar usage percentage from current chat/model state
        without invoking memory retrieval or LLM calls.
        """
        try:
            expected = str(expected_chat_id or "").strip()
            if expected:
                current = str(getattr(self.agent, "active_chat_id", "") or "").strip()
                if current != expected:
                    return
            expected_key = str(expected_state_key or "").strip()
            observed_key = self._context_usage_state_key()
            if expected_key and observed_key != expected_key:
                return
            budgets = self._context_token_budgets()
            if self._should_use_simple_chat_context(budgets):
                user_text = str(user_input_hint or "")
                source_history = self.history_for_regular_context()
                history_messages, _stats = self._build_history_messages_by_budget(
                    int(budgets["history_budget"]),
                    int(budgets["history_summary_budget"]),
                    int(budgets["assistant_clip_tokens"]),
                    source_history=source_history,
                )
                history_tokens = self._context_usage_from_chat_record()
                user_tokens = self._estimate_message_tokens("user", user_text)
                # When a cache anchor exists (_cache_stats on a prior assistant
                # message), history_tokens already includes the system prompt
                # and tool schemas from the previous API call.  Adding them
                # again would double-count.
                has_cache_anchor = any(
                    isinstance(m.get("_cache_stats"), dict)
                    for m in source_history
                )
                if not has_cache_anchor:
                    sys_prompt = self._build_small_model_system_prompt()
                    sys_tokens = self._estimate_message_tokens("system", sys_prompt)
                    tool_schemas_tokens = self._estimate_tool_schemas_tokens()
                    total_input_tokens = int(history_tokens + user_tokens + sys_tokens + tool_schemas_tokens)
                else:
                    total_input_tokens = int(history_tokens + user_tokens)
                if expected:
                    current = str(getattr(self.agent, "active_chat_id", "") or "").strip()
                    if current != expected:
                        return
                if self._context_usage_state_key() != observed_key:
                    return
                self._store_context_usage_snapshot(
                    int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW),
                    total_input_tokens,
                )
                self._persist_context_usage_snapshot()
                return

            filtered_history = self.history_for_regular_context()
            history_messages, _stats = self._build_history_messages_by_budget(
                int(budgets["history_budget"]),
                int(budgets["history_summary_budget"]),
                int(budgets["assistant_clip_tokens"]),
                source_history=filtered_history,
            )
            history_tokens = self._context_usage_from_chat_record()
            compose_prompt = getattr(self.agent, "_compose_system_prompt_snapshot", None)
            if callable(compose_prompt):
                try:
                    system_prompt_snapshot = str(compose_prompt(include_tools=True) or "")
                except Exception:
                    system_prompt_snapshot = str(getattr(self.agent, "system_prompt", "") or "")
            else:
                system_prompt_snapshot = str(getattr(self.agent, "system_prompt", "") or "")
            sys_text = (
                f"{str(getattr(self.agent, '_skills_routing_prefix', '') or '')}"
                f"{system_prompt_snapshot}\n"
                f"{self._software_development_prompt_append()}"
                f"Current workspace name: {str(getattr(self.agent, 'workspace_name', '') or '')}\n"
                f"Current chat name: {str(getattr(self.agent, 'active_chat_name', '') or '')}\n"
            )
            system_tokens = self._estimate_message_tokens("system", sys_text)
            force_new_requirement = bool(
                getattr(self.agent, "_force_current_input_as_requirement_once", False)
            )
            requirement = (
                str(user_input_hint or "").strip()
                if force_new_requirement
                else self._first_user_requirement(str(user_input_hint or "").strip())
            )
            user_anchor = (
                f"User input: {str(user_input_hint or '').strip()}\n"
            )
            if context_hint:
                user_anchor += f"Operation context: {str(context_hint)}\n"
            user_tokens = self._estimate_message_tokens("user", user_anchor)
            # When history has _cache_stats, prompt_cache_hit_tokens +
            # prompt_cache_miss_tokens already include the system prompt.
            # Adding system_tokens separately would double-count it.
            has_cache_anchor = any(
                isinstance(m.get("_cache_stats"), dict)
                for m in filtered_history
            )
            if has_cache_anchor:
                total_input_tokens = int(history_tokens)
            else:
                tool_schemas_tokens = self._estimate_tool_schemas_tokens()
                total_input_tokens = int(system_tokens + history_tokens + tool_schemas_tokens)
            if expected:
                current = str(getattr(self.agent, "active_chat_id", "") or "").strip()
                if current != expected:
                    return
            if self._context_usage_state_key() != observed_key:
                return
            self._store_context_usage_snapshot(
                int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW),
                total_input_tokens,
            )
            self._persist_context_usage_snapshot()
        except Exception:
            # Keep previous snapshot on refresh failure.
            pass

    def schedule_context_usage_refresh_async(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
    ) -> bool:
        """
        Recompute context usage in background to avoid blocking UI/input loop.
        """
        target_chat_id = str(expected_chat_id or "").strip()
        if not target_chat_id:
            target_chat_id = str(getattr(self.agent, "active_chat_id", "") or "").strip()
        request_payload = {
            "user_input_hint": str(user_input_hint or ""),
            "context_hint": str(context_hint or ""),
            "expected_chat_id": target_chat_id,
            "expected_state_key": self._context_usage_state_key(),
        }
        with self._context_usage_refresh_lock:
            if self._context_usage_refresh_inflight:
                # Coalesce requests while one refresh is in flight; keep only the latest snapshot intent.
                self._context_usage_refresh_pending = dict(request_payload)
                return False
            self._context_usage_refresh_inflight = True

        def _run(initial_payload: Dict[str, str]) -> None:
            payload = dict(initial_payload)
            try:
                while True:
                    self.refresh_context_usage_snapshot(
                        user_input_hint=str(payload.get("user_input_hint") or ""),
                        context_hint=str(payload.get("context_hint") or ""),
                        expected_chat_id=str(payload.get("expected_chat_id") or ""),
                        expected_state_key=str(payload.get("expected_state_key") or ""),
                    )
                    with self._context_usage_refresh_lock:
                        pending = self._context_usage_refresh_pending
                        self._context_usage_refresh_pending = None
                        if not pending:
                            self._context_usage_refresh_inflight = False
                            break
                        payload = dict(pending)
            finally:
                with self._context_usage_refresh_lock:
                    self._context_usage_refresh_inflight = False
                    self._context_usage_refresh_pending = None

        threading.Thread(
            target=_run,
            args=(request_payload,),
            daemon=True,
            name=f"{get_app_logger_root()}-context-usage-refresh",
        ).start()
        return True

    # --- Regular task message assembly ---------------------------------------
    def build_regular_task_messages(self, user_input: str, context: str = "") -> Tuple[List[Dict[str, Any]], bool]:
        budgets = self._context_token_budgets()
        if self._should_use_simple_chat_context(budgets):
            return self._build_simple_chat_messages(user_input, budgets)

        import os

        os_info = os.uname() if hasattr(os, "uname") else os.name
        date_time = datetime.now().strftime("%Y-%m-%d %A %H:%M:%S")

        self.update_session_summary_rolling()
        self.maybe_refresh_session_summary_llm()
        self.agent._reload_skills()
        self.agent.system_prompt = self.agent._compose_system_prompt_snapshot(include_tools=True)
        op_context_budget = int(budgets["op_context_budget"])
        immutable_system_core = (
            f"{self.agent._skills_routing_prefix}{self.agent.system_prompt}\n"
            f"{self._software_development_prompt_append()}"
        )
        # Key runtime metadata is intentionally non-clippable.
        workspace_root_text = self._model_visible_workspace_directory_text()
        workspace_data_dir_text = self._model_visible_path_text(
            getattr(self.agent, "workspace_config_dir", None)
        )
        workspace_skills_dir = (Path(self.agent.workspace_config_dir) / "skills").resolve()
        default_install_skills_dir = (get_app_global_config_dir() / "skills").resolve()
        runtime_tail_raw = (
            f"Current OS info: {os_info}\n"
            f"Current workspace name: {self.agent.workspace_name}\n"
            f"Current chat name (weak hint, session label only, not this turn's task goal): {self.agent.active_chat_name}\n"
            f"Current workspace root (absolute path): {workspace_root_text}\n"
            f"Current workspace data directory (absolute path): {workspace_data_dir_text}\n"
            f"Default skill install path (absolute path): {default_install_skills_dir}\n"
            f"Current workspace skills directory (absolute path): {workspace_skills_dir}\n"
            "When installing a third-party skill: if the user does not specify an install location, you must use the Default skill install path (absolute path); "
            "use the Current workspace skills directory (absolute path) only when the user explicitly asks to install into the workspace.\n"
        )
        tail_context = immutable_system_core + runtime_tail_raw
        sys_prefix = tail_context
        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prefix}]
        filtered_history = self.history_for_regular_context()
        history_messages, history_stats = self._build_history_messages_by_budget(
            int(budgets["history_budget"]),
            int(budgets["history_summary_budget"]),
            int(budgets["assistant_clip_tokens"]),
            source_history=filtered_history,
        )
        interruption_line = self._latest_interruption_context_line(filtered_history)
        # The raw user input was already recorded to history by
        # ``_try_record_user_task_message`` (crash safety).  Before
        # appending the context-injected version below, drop the
        # matching raw user message from history to avoid duplication.
        _raw_input = user_input.strip().lower() if user_input else ""
        if _raw_input and history_messages and history_messages[-1].get("role") == "user":
            _last_user = str(history_messages[-1].get("content", "") or "").strip().lower()
            if _last_user == _raw_input:
                history_messages = history_messages[:-1]
        for msg in history_messages:
            messages.append(msg)

        force_new_requirement = bool(
            getattr(self.agent, "_force_current_input_as_requirement_once", False)
        )
        workspace_directory = self._model_visible_workspace_directory_text()
        original_requirement = (
            str(user_input or "").strip()
            if force_new_requirement
            else self._first_user_requirement(user_input)
        )

        # Retrieve memory context and inject into user message.
        # Only inject on the first round of a task (active user message), not on
        # auto-generated continuation rounds (tool results, retries, etc.).
        if not getattr(self.agent, '_memory_injected_this_task', False):
            mem_context = self.memory_context_for_user_message(user_input)
            self.agent._memory_injected_this_task = True
        else:
            mem_context = ""
        current_input = str(user_input or "").strip() + "\n"
        if mem_context:
            current_input = mem_context.strip() + "\n" + current_input
        if force_new_requirement:
            last_cancelled_task = str(getattr(self.agent, "_last_cancelled_task", "") or "").strip()
            current_input += (
                "[Cancelled task] The previous task was cancelled by the user. If this turn is a new task, do not proactively resume or redo the cancelled task "
                "unless the user explicitly asks to continue.\n\n"
            )
            if last_cancelled_task:
                current_input += f"Recently cancelled task: {last_cancelled_task}\n"
        if self.agent.operation_results:
            pass
        if context:
            ctx_line = f"Operation context: {context}\n"
            current_input += self._clip_text_to_token_budget(ctx_line, op_context_budget)
        if interruption_line:
            current_input += f"Most recent interruption status: {interruption_line}\n"
        current_input += f"Local time reference: {date_time}"
        current_user_msg = {"role": "user", "content": current_input}
        if mem_context:
            current_user_msg["_memory_context"] = mem_context.strip()
        messages.append(current_user_msg)

        system_tokens = 0
        history_tokens = 0
        user_tokens = 0
        tool_schemas_tokens = 0
        try:
            system_tokens = self._estimate_message_tokens("system", sys_prefix)
            history_tokens = self._history_tokens_cumulative(history_messages)
            user_tokens = self._estimate_message_tokens("user", current_input)
            tool_schemas_tokens = self._estimate_tool_schemas_tokens()
            has_cache_anchor = any(
                isinstance(m.get("_cache_stats"), dict)
                for m in history_messages
            )
            total_input_tokens = int(system_tokens + history_tokens + user_tokens + tool_schemas_tokens)
            snapshot_input_tokens = int(history_tokens + tool_schemas_tokens) if has_cache_anchor else int(total_input_tokens)
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
            usage_pct = max(0, min(999, int(round((total_input_tokens * 100.0) / max(1, ctx_window)))))
            self.agent._last_context_usage_percent_precompression = usage_pct
            self.agent._last_context_aggressive_compression_applied = False
            if usage_pct > AGGRESSIVE_COMPRESS_TRIGGER_PCT:
                target_tokens = max(256, int((ctx_window * AGGRESSIVE_COMPRESS_TARGET_PCT) / 100))
                aggressive_user_budget = max(120, int(target_tokens * 0.45))
                aggressive_system_budget = max(80, int(target_tokens * 0.35))
                aggressive_history_budget = max(40, int(target_tokens * 0.20))
                aggressive_history_summary_budget = max(30, int(aggressive_history_budget * 0.55))
                aggressive_assistant_clip = max(48, int(int(budgets.get("assistant_clip_tokens") or 180) * 0.35))
                aggressive_op_context_budget = max(24, int(op_context_budget * 0.35))

                tail_context2 = immutable_system_core + runtime_tail_raw
                sys_prefix2 = tail_context2

                history_messages2, history_stats2 = self._build_history_messages_by_budget(
                    aggressive_history_budget,
                    aggressive_history_summary_budget,
                    aggressive_assistant_clip,
                    source_history=filtered_history,
                )
                current_input2 = str(user_input or "").strip() + "\n"
                mem_context2 = mem_context
                if mem_context2:
                    current_input2 = mem_context2.strip() + "\n" + current_input2
                if force_new_requirement:
                    last_cancelled_task = str(getattr(self.agent, "_last_cancelled_task", "") or "").strip()
                    current_input2 += (
                        "4) The previous task was cancelled by the user. If this turn is a new task, do not proactively resume or redo the cancelled task "
                        "unless the user explicitly asks to continue.\n\n"
                    )
                    if last_cancelled_task:
                        current_input2 += f"Recently cancelled task: {last_cancelled_task}\n"
                if interruption_line:
                    current_input2 += f"Most recent interruption status: {interruption_line}\n"
                if self.agent.operation_results:
                    pass
                if context:
                    ctx_line2 = f"Operation context: {context}\n"
                    current_input2 += self._clip_text_to_token_budget(ctx_line2, aggressive_op_context_budget)
                # Hard anchors: never clip current input and time.
                current_input2 += f"Local time reference: {date_time}"

                system_tokens2 = self._estimate_message_tokens("system", sys_prefix2)
                history_tokens2 = sum(
                    self._message_cost_for_tail_budget(m)
                    for m in history_messages2
                )
                user_tokens2 = self._estimate_message_tokens("user", current_input2)
                total_input_tokens2 = int(system_tokens2 + history_tokens2 + user_tokens2)
                has_cache_anchor2 = any(
                    isinstance(m.get("_cache_stats"), dict)
                    for m in history_messages2
                )
                snapshot_input_tokens2 = int(history_tokens2) if has_cache_anchor2 else int(total_input_tokens2)

                if total_input_tokens2 < total_input_tokens:
                    messages = [{"role": "system", "content": sys_prefix2}]
                    messages += list(history_messages2)
                    user_msg2 = {"role": "user", "content": current_input2}
                    if mem_context2:
                        user_msg2["_memory_context"] = mem_context2.strip()
                    messages.append(user_msg2)
                    sys_prefix = sys_prefix2
                    history_messages = history_messages2
                    current_input = current_input2
                    history_stats = history_stats2
                    system_tokens = system_tokens2
                    history_tokens = history_tokens2
                    user_tokens = user_tokens2
                    total_input_tokens = total_input_tokens2
                    snapshot_input_tokens = snapshot_input_tokens2
                    self.agent._last_context_aggressive_compression_applied = True
                    get_logger().info(
                        "context-pack aggressive-compress triggered pre_pct=%s target_pct=%s post_pct=%s",
                        usage_pct,
                        AGGRESSIVE_COMPRESS_TARGET_PCT,
                        int(round((total_input_tokens2 * 100.0) / max(1, ctx_window))),
                    )

            self._store_context_usage_snapshot(ctx_window, snapshot_input_tokens)
            if force_new_requirement:
                self.agent._force_current_input_as_requirement_once = False
            get_logger().info(
                "context-pack profile=%s ctx_window=%s input_budget=%s system=%s history=%s user=%s "
                "history_trimmed_assistant=%s history_summary_messages=%s history_dropped=%s",
                budgets.get("profile"),
                budgets.get("context_window"),
                budgets.get("input_budget"),
                system_tokens,
                history_tokens,
                user_tokens,
                history_stats.get("assistant_trimmed", 0),
                history_stats.get("summary_messages", 0),
                history_stats.get("dropped_messages", 0),
            )
        except Exception:
            pass
        return messages, True
