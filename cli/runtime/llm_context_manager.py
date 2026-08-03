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
from contextlib import redirect_stderr, redirect_stdout
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


def _assistant_replay_mode_from_agent(agent: Any) -> str:
    """Whether reply blocks replay their ``_reply_records`` interleaved or merged.

    Chat Completions (incl. DeepSeek) return a single flattened assistant
    message (one ``reasoning_content``, one ``content``, one ``tool_calls``),
    so merged replay reproduces the original message boundary and best matches
    the provider's prompt-prefix cache. Responses / Anthropic preserve
    multi-segment reasoning in their response structure, so interleaved replay
    is more faithful there. The stored ``_reply_records`` is never changed;
    this only selects how the block is flattened during model-context assembly.
    """
    params = getattr(agent, "params", None)
    provider = str(getattr(agent, "provider", "") or "")
    try:
        from ..ai.ai_provider_clients import _base_url_suffix_hint, resolve_api_mode
        mode = resolve_api_mode(params=params, provider=provider)
    except Exception:
        mode = "auto"
    if mode == "responses":
        return "interleaved"
    if mode == "chat":
        return "merged"
    # auto (or unknown): follow the base_url suffix the request router would use.
    base_url = ""
    if isinstance(params, dict):
        base_url = str(params.get("base_url") or "")
    try:
        if _base_url_suffix_hint(base_url) == "responses":
            return "interleaved"
    except Exception:
        pass
    return "merged"

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
        if bool(getattr(self.agent, "_plan_mode_sticky", False)):
            user_text = (
                "<system-reminder>You are in Plan mode. Do NOT modify files — only "
                "explore and design. Treat user requests as planning requests, not "
                "execution commands.</system-reminder>\n" + user_text
            )
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
        use_interleaved_replay = _assistant_replay_mode_from_agent(self.agent) == "interleaved"
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        for idx, msg in enumerate(hist):
            role = str(msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant", "tool"):
                continue
            if role == "assistant" and _sms._is_reply_block(msg):
                if not use_interleaved_replay:
                    # Chat Completions (incl. DeepSeek): replay the block as a
                    # single flattened assistant message (reasoning_content +
                    # content + tool_calls), reproducing the original message
                    # boundary for prompt-prefix cache hits. Falls through to
                    # the single-entry path below via model_view.
                    effective = _sms._assistant_model_view(msg)
                    raw_content = str(effective.get("content") or "")
                    content = self._normalize_history_content_for_model("assistant", raw_content, message=effective)
                    content = self._clip_text_to_token_budget(content, assistant_clip_tokens)
                    entry: Dict[str, Any] = {"role": "assistant", "content": content}
                    tcs = effective.get("tool_calls")
                    if isinstance(tcs, list) and tcs:
                        entry["tool_calls"] = tcs
                    thinking = str(effective.get("_thinking") or "").strip()
                    if thinking:
                        entry["_thinking"] = thinking
                        if effective.get("_thinking_from_content"):
                            entry["_thinking_from_content"] = True
                    msg_model = str(msg.get("_model") or "").strip()
                    if msg_model:
                        entry["_model"] = msg_model
                    cs = msg.get("_cache_stats")
                    if isinstance(cs, dict) and cs:
                        entry["_cache_stats"] = cs
                    from ..services.session_memory_service import _message_effective_token_count
                    outer_tc = _message_effective_token_count(msg)
                    if outer_tc is not None:
                        entry["_token_count"] = outer_tc
                    elif idx >= last_cache_src_idx:
                        local = self._estimate_message_tokens("assistant", raw_content)
                        msg["_token_count"] = local
                        entry["_token_count"] = local
                    normalized.append(entry)
                    continue
                # Responses / Anthropic preserve multi-segment reasoning: emit
                # one provider assistant message per recorded node (reasoning
                # merged with its following content), never merged across
                # kinds. Split-out nodes are skipped (their data lives in the
                # raw node). Tool results follow the tool-calls message.
                from ..services.session_memory_service import _message_effective_token_count
                sub_messages = _sms._assistant_model_messages(msg)
                msg_model = str(msg.get("_model") or "").strip()
                outer_tc = _message_effective_token_count(msg)
                for _si, sub in enumerate(sub_messages):
                    sub_role = str(sub.get("role") or "assistant").strip().lower()
                    raw_content = str(sub.get("content") or "")
                    content = self._normalize_history_content_for_model(sub_role, raw_content, message=sub)
                    content = self._clip_text_to_token_budget(content, assistant_clip_tokens)
                    entry: Dict[str, Any] = {"role": sub_role, "content": content}
                    tcs = sub.get("tool_calls")
                    if isinstance(tcs, list) and tcs:
                        entry["tool_calls"] = tcs
                    thinking = str(sub.get("_thinking") or "").strip()
                    if thinking:
                        entry["_thinking"] = thinking
                        if sub.get("_thinking_from_content"):
                            entry["_thinking_from_content"] = True
                    if msg_model:
                        entry["_model"] = msg_model
                    if _si == len(sub_messages) - 1:
                        cs = msg.get("_cache_stats")
                        if isinstance(cs, dict) and cs:
                            entry["_cache_stats"] = cs
                        if outer_tc is not None:
                            entry["_token_count"] = outer_tc
                        elif idx >= last_cache_src_idx:
                            local = self._estimate_message_tokens(sub_role, raw_content)
                            msg["_token_count"] = local
                            entry["_token_count"] = local
                    normalized.append(entry)
                continue
            # Reply blocks keep content/reasoning/tool_calls inside
            # ``_reply_records``; flatten them via the model view so the
            # provider payload reproduces the original call exactly (split
            # blocks use the raw node's uncleaned content for cache fidelity).
            effective = _sms._assistant_model_view(msg) if role == "assistant" else msg
            raw_content = str(effective.get("content") or "")
            # When the message carries ``_api_content`` (the exact text that was
            # sent to the provider), use it for the model context so replayed
            # history prefixes match upstream cache units.
            api_content = effective.get("_api_content")
            if isinstance(api_content, str) and api_content.strip():
                raw_content = api_content
            # ``_context_suffix`` carries auto-injected content (evidence block,
            # local time, etc.) that was previously stored as a separate _internal
            # user message. Append it to the message content so the model still
            # receives it during history replay.
            context_suffix = effective.get("_context_suffix")
            if role == "user" and isinstance(context_suffix, str) and context_suffix.strip():
                if raw_content.strip():
                    raw_content = raw_content + "\n\n" + context_suffix.strip()
                else:
                    raw_content = context_suffix.strip()
            if role == "user" and self._is_excluded_user_message_for_model_context(effective):
                continue
            if role == "user" and self._is_builtin_slash_user_message(role, raw_content):
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
            content = self._normalize_history_content_for_model(role, raw_content, message=effective)
            if role == "assistant":
                before = content
                content = self._clip_text_to_token_budget(content, assistant_clip_tokens)
                if content != before:
                    assistant_trimmed += 1
            entry: Dict[str, Any] = {"role": role, "content": content}
            if role == "tool":
                tid = str(msg.get("tool_call_id") or "").strip()
                if tid:
                    entry["tool_call_id"] = tid
                tname = str(msg.get("name") or "").strip()
                if tname:
                    entry["name"] = tname
            if role == "assistant":
                tcs = effective.get("tool_calls")
                if isinstance(tcs, list) and tcs:
                    entry["tool_calls"] = tcs
                msg_model = str(effective.get("_model") or "").strip()
                if msg_model:
                    entry["_model"] = msg_model
                cs = effective.get("_cache_stats")
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
        # Sanitize: remove tool_calls from assistant messages whose
        # tool_call_ids don't all have corresponding tool responses in the
        # final message list (e.g. user cancelled mid-batch, leaving an
        # orphaned tool_call_id).  Without this the provider returns 400.
        # The pairing must be symmetric: a ``role: tool`` message whose
        # ``tool_call_id`` matches no assistant ``tool_calls`` entry is an
        # orphan and is dropped (otherwise the provider rejects the batch
        # with "Messages with role 'tool' must be a response to a preceding
        # message with 'tool_calls'"), and assistant ``tool_calls`` are
        # trimmed to the calls that actually have a response so no dangling
        # call id is left behind.
        assistant_tcids: Set[str] = set()
        tool_tcids: Set[str] = set()
        for _m in working:
            _role = str(_m.get("role") or "").strip().lower()
            if _role == "assistant":
                _tcs = _m.get("tool_calls")
                if isinstance(_tcs, list):
                    for _c in _tcs:
                        if isinstance(_c, dict):
                            _cid = str(_c.get("id") or "").strip()
                            if _cid:
                                assistant_tcids.add(_cid)
            elif _role == "tool":
                _tid = str(_m.get("tool_call_id") or "").strip()
                if _tid:
                    tool_tcids.add(_tid)
        filtered: List[Dict[str, Any]] = []
        for _m in working:
            if str(_m.get("role") or "").strip().lower() == "tool":
                _tid = str(_m.get("tool_call_id") or "").strip()
                if _tid and _tid not in assistant_tcids:
                    continue
            filtered.append(_m)
        working = filtered
        for _m in working:
            if str(_m.get("role") or "").strip().lower() == "assistant":
                _tcs = _m.get("tool_calls")
                if isinstance(_tcs, list) and _tcs:
                    _kept = [
                        _c
                        for _c in _tcs
                        if isinstance(_c, dict)
                        and str(_c.get("id") or "").strip() in tool_tcids
                    ]
                    if _kept:
                        _m["tool_calls"] = _kept
                    else:
                        del _m["tool_calls"]
        return working, stats

    def _message_cost_for_tail_budget(self, msg: Dict[str, Any]) -> int:
        from ..services.session_memory_service import _message_effective_token_count
        tc = _message_effective_token_count(msg)
        if tc is not None:
            return tc
        role = str(msg.get("role") or "").strip().lower()
        effective = _sms._assistant_model_view(msg) if role == "assistant" else msg
        content = self._normalize_history_content_for_model(role, str(effective.get("content") or ""), message=effective)
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
            effective = _sms._assistant_model_view(msg) if role == "assistant" else msg
            content = self._normalize_history_content_for_model(role, str(effective.get("content") or ""), message=effective)
            return self._estimate_message_tokens(role, content)

        total = 0
        start_idx = 0
        # Locate the most recent compaction summary. It marks a hard restart of
        # the model context: everything before it is replaced by the summary
        # and must never be counted again (neither via a stale cache anchor nor
        # directly). This is the authoritative lower bound for the real context.
        latest_compaction_idx = -1
        for i, m in enumerate(messages):
            if self.is_context_compaction_summary_message(m):
                latest_compaction_idx = i

        # Use the cache anchor only when it sits at/after the latest compaction
        # summary. If a compaction happened after the last cache anchor, that
        # anchor's input_tokens still describe the *pre-compaction* context, so
        # trusting it would double-count everything. In that case we start from
        # the compaction summary and count forward instead.
        use_cache_anchor = (
            0 <= last_cache_idx < len(messages)
            and last_cache_idx >= latest_compaction_idx
        )
        if use_cache_anchor:
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
        elif latest_compaction_idx >= 0:
            # No (valid) cache anchor — or the anchor predates compaction.
            # Start from the most recent compaction summary so compacted
            # messages before it are not counted a second time.
            start_idx = latest_compaction_idx

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
        candidates = list(rows)
        has_new_dialogue = any(
            not self.is_context_compaction_summary_message(m)
            for _idx, m in candidates
        )
        if not has_new_dialogue:
            return []
        return candidates

    # --- Compaction ----------------------------------------------------------
    def build_compaction_user_input(self, mode: str) -> str:
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "compact_prompt.md"
        raw = prompt_path.read_text(encoding="utf-8").strip()
        compact_prompt_text = preprocess_prompt(raw, {"os": platform.system()})
        normalized_mode = str(mode or "").strip().lower() or "manual"
        return (
            f"compact_mode={normalized_mode}\n"
            "You are compacting the active conversation context using the normal main-session history.\n\n"
            f"{compact_prompt_text}\n\n"
            "Generate a concise checkpoint handoff summary that can replace the already-covered context."
        )

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

    def _print_compaction_notice(self, title: str, body: str = "") -> int:
        rendered_lines = self._print_compaction_banner(title)
        body_text = str(body or "").strip()
        if body_text:
            self._write_compaction_raw(body_text + "\n\n")
            rendered_lines += body_text.count("\n") + 2
            try:
                self.agent._terminal_cursor_at_line_start = True
            except Exception:
                pass
        return rendered_lines

    def _emit_gui_compaction_notice(self, phase: str, mode: str, title: str, body: str = "") -> None:
        try:
            cb = getattr(self.agent, "_gui_compaction_notice", None)
            if callable(cb):
                clean_title = str(title or "").strip()
                clean_body = str(body or "").strip()
                cb(
                    str(phase or ""),
                    str(mode or ""),
                    clean_title,
                    clean_body,
                    clean_title if not clean_body else f"{clean_title}\n\n{clean_body}",
                )
        except Exception:
            pass

    def _compaction_tui_finalize_via_reload(self) -> bool:
        remember_tail = getattr(self.agent, "_remember_active_chat_history_tail_anchor", None)
        if callable(remember_tail):
            try:
                remember_tail()
            except Exception:
                pass
        reload_fn = getattr(self.agent, "_reload_chat_history_from_anchor_on_resize", None)
        if not callable(reload_fn):
            return False
        real_stdout = self._compaction_output_stream()
        real_stderr = sys.stderr
        seen: Set[int] = set()
        while real_stderr is not None:
            sid = id(real_stderr)
            if sid in seen:
                break
            seen.add(sid)
            nxt = getattr(real_stderr, "_primary", None)
            if nxt is None:
                nxt = getattr(real_stderr, "_base_stream", None)
            if nxt is None:
                break
            real_stderr = nxt
        try:
            with redirect_stdout(real_stdout), redirect_stderr(real_stderr or sys.stderr):
                reload_fn(include_startup_overview=False)
            return True
        except Exception:
            return False

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
        self._emit_gui_compaction_notice("start", mode, start_text)
        start_banner_lines = self._print_compaction_notice(start_text)
        source_history = [m for _idx, m in candidates_with_idx]
        compaction_user_input = self.build_compaction_user_input(mode)
        stream_summary = True
        gui_notice_enabled = callable(getattr(self.agent, "_gui_compaction_notice", None))
        stream_to_terminal = not gui_notice_enabled
        streamed_summary_parts: List[str] = []
        try:
            raw = self.agent.call_ai(
                compaction_user_input,
                context="",
                stream=stream_summary,
                return_message=True,
                record_history_override=False,
            )
        except Exception as e:
            get_logger().exception("context compact: model call failed")
            if mode == "manual":
                print(self._t("compaction.failed", error=e))
            return False
        summary = ""
        compaction_reply_message = None
        if isinstance(raw, dict):
            # Non-stream path (return_message=True returns the assistant message).
            summary = str(raw.get("content") or "").strip()
            compaction_reply_message = raw
        elif isinstance(raw, str):
            summary = raw.strip()
        elif raw is not None and stream_to_terminal:
            from .runtime_loop import (
                _consume_streaming_ai_response,
                _take_pending_stream_history_reload_request,
            )

            consumed, _streamed = _consume_streaming_ai_response(self.agent, raw)
            _take_pending_stream_history_reload_request(self.agent)
            summary = str(consumed or "").strip()
            compaction_reply_message = getattr(raw, "final_message", None)
        elif raw is not None:
            close_fn = getattr(raw, "close", None)
            try:
                for chunk in raw:
                    piece = str(chunk or "")
                    if not piece:
                        continue
                    streamed_summary_parts.append(piece)
                    current_stream_text = "".join(streamed_summary_parts)
                    if gui_notice_enabled:
                        self._emit_gui_compaction_notice(
                            "stream",
                            mode,
                            start_text,
                            current_stream_text,
                        )
                    if stream_to_terminal:
                        self._write_compaction_raw(piece)
            finally:
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
            streamed_raw_text = "".join(streamed_summary_parts)
            summary = streamed_raw_text.strip()
            compaction_reply_message = getattr(raw, "final_message", None)
        if summary.startswith("❌") or summary.startswith("Error calling LLM API") or summary.startswith("❌ API error:") or not summary:
            if mode == "manual":
                print(summary or self._t("compaction.failed_empty_summary"))
            return False
        summary = summary.replace("```", "").strip()
        compaction_output_tokens = None
        compaction_reasoning_tokens = None
        compaction_token_count_includes_reasoning = None
        if isinstance(compaction_reply_message, dict):
            compaction_output_tokens = compaction_reply_message.get("_output_tokens")
            compaction_reasoning_tokens = compaction_reply_message.get("_reasoning_tokens")
            compaction_token_count_includes_reasoning = compaction_reply_message.get(
                "_token_count_includes_reasoning"
            )
        content = self.build_context_compaction_summary_content(
            summary=summary,
            mode=mode,
            covered_message_count=len(source_history),
            output_tokens=compaction_output_tokens,
            reasoning_tokens=compaction_reasoning_tokens,
        )
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = {
            "role": "assistant",
            "content": content,
            "created_at": created_at,
        }
        # Record the compaction call's output-token accounting on the summary
        # message so post-compaction context-usage math counts the summary
        # precisely. We deliberately do NOT copy ``_cache_stats`` here (that
        # anchor carries the *pre-compaction* input tokens). We mirror the
        # provider's ``_token_count_includes_reasoning`` flag verbatim so
        # ``_message_effective_token_count`` keeps applying the right formula
        # per provider: ``_output_tokens`` (DeepSeek-style, reasoning folded
        # in) vs ``_output_tokens - _reasoning_tokens`` (others). Recording
        # ``_reasoning_tokens`` even when zero is what makes the latter
        # computable for providers that fold reasoning into ``_output_tokens``.
        if isinstance(compaction_output_tokens, (int, float)) and int(compaction_output_tokens) > 0:
            msg["_output_tokens"] = int(compaction_output_tokens)
        if isinstance(compaction_reasoning_tokens, (int, float)):
            msg["_reasoning_tokens"] = int(compaction_reasoning_tokens)
        if compaction_token_count_includes_reasoning is True:
            msg["_token_count_includes_reasoning"] = True
        elif compaction_token_count_includes_reasoning is False:
            msg["_token_count_includes_reasoning"] = False
        try:
            self.agent.conversation_history.append(msg)
            self.agent._sync_active_chat_messages()
            self.refresh_context_usage_snapshot(context_hint="context compacted")
        except Exception:
            get_logger().exception("context compact: failed to persist summary")
            if mode == "manual":
                print(self._t("compaction.failed_saving_summary"))
            return False
        if stream_to_terminal:
            if self._compaction_tui_finalize_via_reload():
                return True
        self._clear_compaction_banner(start_banner_lines)
        compact_display = self.build_context_compaction_display_payload(content)
        self._print_compaction_notice(compact_display["title"], compact_display["body"])
        self._emit_gui_compaction_notice("done", mode, compact_display["title"], compact_display["body"])
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
        injected_suffix_parts: list[str] = []
        if bool(getattr(self.agent, "_plan_mode_sticky", False)):
            plan_reminder = (
                "<system-reminder>You are in Plan mode. Do NOT modify files — only "
                "explore and design. Treat user requests as planning requests, not "
                "execution commands.</system-reminder>"
            )
            current_input = plan_reminder + "\n" + current_input
            injected_suffix_parts.append(plan_reminder)
        if mem_context:
            current_input = mem_context.strip() + "\n" + current_input
            injected_suffix_parts.append(mem_context.strip())
        if self.agent.operation_results:
            pass
        if context:
            ctx_line = f"Operation context: {context}\n"
            current_input += self._clip_text_to_token_budget(ctx_line, op_context_budget)
            injected_suffix_parts.append(ctx_line.strip())
        if interruption_line:
            interruption_text = f"Most recent interruption status: {interruption_line}\n"
            current_input += interruption_text
            injected_suffix_parts.append(interruption_text.strip())
        time_ref = f"Local time reference: {date_time}"
        current_input += time_ref
        injected_suffix_parts.append(time_ref)
        current_user_msg = {"role": "user", "content": current_input}
        if injected_suffix_parts:
            current_user_msg["_injected_suffix"] = "\n\n".join(injected_suffix_parts)
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
