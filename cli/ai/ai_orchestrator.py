import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_logger_root
from ..core.logging.app_logging import get_logger
from .ai_provider_clients import (
    AICallContext,
    ModelCallError,
    AIResult,
    ProviderCallContext,
    _extract_api_error_message,
    _sanitize_assistant_text,
    call_ai_with_provider,
    prepare_image_input,
)
from .ai_special_mode_prompts import build_special_mode_messages


_AI_HISTORY_LOG = get_logger(f"{get_app_logger_root()}.ai_history")


def _build_tool_calls_plan_payload(message: Optional[Dict[str, Any]]) -> str:
    """Serialize standard-API tool_calls into a JSON plan string for chat history.

    Returns an empty string when ``message`` carries no usable tool_calls so
    callers can fall back to the original empty-content handling.
    """
    if not isinstance(message, dict):
        return ""
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    serialized: List[Dict[str, Any]] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        raw_args: Any = function.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except Exception:
                parsed_args = None
            if not isinstance(parsed_args, dict):
                parsed_args = {"_raw_arguments": raw_args}
        elif isinstance(raw_args, dict):
            parsed_args = raw_args
        else:
            parsed_args = {}
        serialized.append({
            "id": str(entry.get("id") or "").strip(),
            "type": str(entry.get("type") or "function"),
            "function": {
                "name": name,
                "arguments": json.dumps(parsed_args, ensure_ascii=False),
            },
        })
    if not serialized:
        return ""
    payload = {"tool_calls": serialized}
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return ""


def _coalesce_reply_events(
    events: List[Tuple[str, Any, str]],
) -> List[Dict[str, Any]]:
    """Coalesce the stream's natural-arrival events into reply nodes.

    Consecutive same-kind/same-source text events merge into one node; each
    tool_call event becomes its own node. "thinking" (extracted from content)
    maps to kind ``reasoning`` / source ``content_split``."""
    nodes: List[Dict[str, Any]] = []
    for kind, data, source in events:
        if kind == "thinking":
            nkind: str = "reasoning"
            nsrc: str = "content_split"
        else:
            nkind = str(kind or "").strip()
            nsrc = str(source or "").strip()
        if nkind == "tool_call":
            if not isinstance(data, dict):
                continue
            nodes.append({
                "kind": "tool_call",
                "tool_call_id": str(data.get("id") or "").strip(),
                "data": data,
                "from": nsrc,
            })
            continue
        if not isinstance(data, str) or not data:
            continue
        if nodes and nodes[-1].get("kind") == nkind and nodes[-1].get("from") == nsrc:
            nodes[-1]["data"] = str(nodes[-1].get("data") or "") + data
        else:
            nodes.append({"kind": nkind, "data": data, "from": nsrc})
    return nodes


def _synthesize_reply_nodes(message: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Synthesize reply nodes from a final message dict in canonical order
    (used when no natural-arrival events were captured, e.g. non-stream)."""
    if not isinstance(message, dict):
        return []
    content = str(message.get("content") or "")
    try:
        clean = _sanitize_assistant_text(content)
    except Exception:
        clean = content
    thinking = str(message.get("_thinking") or "").strip()
    thinking_from_content = bool(message.get("_thinking_from_content"))
    tool_calls = message.get("tool_calls")
    nodes: List[Dict[str, Any]] = []
    if thinking and thinking_from_content:
        nodes.append({"kind": "reasoning", "data": thinking, "from": "content_split"})
        nodes.append({"kind": "content", "data": clean, "from": "content_split"})
    elif thinking:
        nodes.append({"kind": "reasoning", "data": thinking, "from": "native"})
        nodes.append({"kind": "content", "data": content, "from": "native"})
    elif content or (clean != content):
        nodes.append({"kind": "content", "data": clean, "from": "native"})
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            nodes.append({
                "kind": "tool_call",
                "tool_call_id": str(tc.get("id") or "").strip(),
                "data": tc,
                "from": "native",
            })
    return nodes


def _replace_tool_call_nodes(
    nodes: List[Dict[str, Any]],
    deduped_tool_calls: Any,
) -> List[Dict[str, Any]]:
    """Replace tool-call node data with the deduplicated call set actually
    used for execution, keeping arrival order."""
    if not isinstance(deduped_tool_calls, list):
        return nodes
    calls = [c for c in deduped_tool_calls if isinstance(c, dict)]
    out: List[Dict[str, Any]] = []
    tc_index = 0
    for node in nodes:
        if node.get("kind") == "tool_call":
            if tc_index >= len(calls):
                continue
            call = calls[tc_index]
            tc_index += 1
            node = dict(node)
            node["data"] = call
            node["tool_call_id"] = str(call.get("id") or "").strip()
        out.append(node)
    return out


_PSEUDO_TOOL_CALL_ENVELOPE_RE = re.compile(
    r"<tool_calls\b[^>]*>(.*?)</tool_calls>",
    re.IGNORECASE | re.DOTALL,
)
_PSEUDO_TOOL_CALL_FENCE_RE = re.compile(
    r"```json\s*(.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)


def _pseudo_call_to_openai(obj: Any) -> Optional[Dict[str, Any]]:
    """Convert a parsed pseudo tool-call payload into an OpenAI-shaped call
    dict, or None when it is not a usable call (incompatible -> discarded)."""
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
        if not name:
            return None
        args = fn.get("arguments")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except Exception:
                args = "{}"
        return {
            "id": str(obj.get("id") or "").strip(),
            "type": "function",
            "function": {"name": name, "arguments": args or "{}"},
        }
    name = str(obj.get("tool") or obj.get("name") or "").strip()
    if not name:
        return None
    args = obj.get("args")
    if not isinstance(args, dict):
        args = {}
    try:
        args_str = json.dumps(args, ensure_ascii=False)
    except Exception:
        args_str = "{}"
    return {
        "id": str(obj.get("id") or "").strip(),
        "type": "function",
        "function": {"name": name, "arguments": args_str or "{}"},
    }


def _extract_pseudo_tool_calls_from_text(text: str) -> List[Dict[str, Any]]:
    """Extract compatible pseudo tool calls embedded in assistant content text
    (``<tool_calls>...</tool_calls>`` envelopes and fenced JSON tool payloads).

    Compatible payloads become OpenAI-shaped call dicts; unparseable /
    incompatible ones are dropped so they are never recorded as nodes."""
    if not isinstance(text, str) or not text:
        return []
    calls: List[Dict[str, Any]] = []

    def _push(payload: Any) -> None:
        if isinstance(payload, dict):
            items = payload.get("tool_calls")
            if isinstance(items, list):
                for item in items:
                    parsed = _pseudo_call_to_openai(item)
                    if parsed is not None:
                        calls.append(parsed)
            else:
                parsed = _pseudo_call_to_openai(payload)
                if parsed is not None:
                    calls.append(parsed)
        elif isinstance(payload, list):
            for item in payload:
                parsed = _pseudo_call_to_openai(item)
                if parsed is not None:
                    calls.append(parsed)

    for matcher in (_PSEUDO_TOOL_CALL_ENVELOPE_RE, _PSEUDO_TOOL_CALL_FENCE_RE):
        for match in matcher.finditer(text):
            body = match.group(1).strip()
            if not body:
                continue
            try:
                payload = json.loads(body)
            except Exception:
                continue
            _push(payload)
    return calls


def _reply_requires_split(
    message: Optional[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
) -> bool:
    """Whether content cleaning happened (content text carried embedded
    thinking / pseudo tool calls), which requires the raw node to be kept."""
    if isinstance(message, dict):
        content = str(message.get("content") or "")
        try:
            if _sanitize_assistant_text(content) != content:
                return True
        except Exception:
            pass
        if message.get("_thinking_from_content"):
            return True
    for node in nodes:
        if str(node.get("from") or "").strip().lower() == "content_split":
            return True
    return False


def _build_reply_records(
    message: Optional[Dict[str, Any]],
    reply_events: Optional[List[Tuple[str, Any, str]]],
    deduped_tool_calls: Any,
) -> List[Dict[str, Any]]:
    """Build the ``_reply_records`` list for one model reply.

    Natural-arrival events (when available) define node order; otherwise nodes
    are synthesized in canonical order. When the reply required cleaning, the
    block is ordered as: native nodes (reasoning from ``reasoning_content`` and
    ``tool_calls``) in arrival order, then the ``raw`` node preserving the
    original uncleaned content, then the nodes split out of that raw text
    (cleaned ``content`` pieces, extracted thinking, pseudo tool calls) in
    their split order. Split-out nodes carry ``from: "content_split"``; native
    nodes keep ``from: "native"`` (they are not part of the raw text)."""
    if reply_events:
        nodes = _coalesce_reply_events(reply_events)
    else:
        nodes = _synthesize_reply_nodes(message)
    if not nodes:
        return []
    if isinstance(deduped_tool_calls, list):
        nodes = _replace_tool_call_nodes(nodes, deduped_tool_calls)
    # Content-embedded pseudo tool calls (compatible ones) become tool_call
    # content_split nodes; incompatible/unparseable blocks are discarded.
    if isinstance(message, dict):
        raw_text = str(message.get("content") or "")
        if raw_text:
            for pseudo_call in _extract_pseudo_tool_calls_from_text(raw_text):
                nodes.append({
                    "kind": "tool_call",
                    "tool_call_id": str(pseudo_call.get("id") or "").strip(),
                    "data": pseudo_call,
                    "from": "content_split",
                })
    if _reply_requires_split(message, nodes):
        raw_content = str(message.get("content") or "") if isinstance(message, dict) else ""
        # Order reflects the natural arrival of the reply's data streams: the
        # raw node sits at the position where the content stream arrived, so
        # native reasoning that preceded it stays above it and native tool
        # calls (which typically follow the content) sit below it. The nodes
        # split out of the raw (cleaned content, extracted thinking, pseudo
        # tool calls) come right after the raw and are marked
        # ``from: "content_split"`` (the single "derived from raw" marker);
        # native reasoning/tool_calls keep ``from: "native"``.
        before_nodes: List[Dict[str, Any]] = []
        after_nodes: List[Dict[str, Any]] = []
        seen_content_stream = False
        for node in nodes:
            kind = str(node.get("kind") or "").strip().lower()
            src = str(node.get("from") or "").strip().lower()
            if kind == "content":
                # Cleaned content of a split reply is derived from the raw text.
                node["from"] = "content_split"
                src = "content_split"
            is_content_stream = kind == "content" or src == "content_split"
            if is_content_stream:
                seen_content_stream = True
                after_nodes.append(node)
            elif seen_content_stream:
                after_nodes.append(node)
            else:
                before_nodes.append(node)
        nodes = before_nodes + [
            {"kind": "raw", "_split_source": True, "content": raw_content}
        ] + after_nodes
    return nodes


@dataclass
class AgentAIContext:
    provider: str
    model_name: str
    model_params: Optional[Dict[str, Any]]
    openai_conf: Optional[Dict[str, Any]]
    history_writer: Callable[..., None]
    regular_message_builder: Callable[[str, str], Tuple[List[Dict[str, Any]], bool]]
    ollama_importer: Callable[[], Any]
    workspace_root: str = ""
    self_repo_root: str = ""
    display_language: str = "en"
    # Optional sink for multi-attempt model-call error messages that should be
    # displayed on screen and survive terminal-resize redraws but must NOT be
    # persisted to chat history. Receives a single pre-formatted string.
    ephemeral_notice_writer: Optional[Callable[[str], None]] = None
    # Optional sink for model-call error messages that should be persisted
    # to chat history so the GUI can render a centered error banner (but
    # excluded from the model context so it never reaches the AI).
    # Receives a single human-readable error message string.
    model_error_history_writer: Optional[Callable[[str], None]] = None


class AIOrchestrator:
    def __init__(self, context: AgentAIContext) -> None:
        self.context = context

    def call(self, *, call_ctx: AICallContext) -> AIResult:
        # Prefer the per-call resolved snapshot when the caller supplied one
        # (see AICallContext). The shared ``self.context`` is mutated by
        # concurrent chat activations, so reading it mid-call can pair one
        # chat's model name with another chat's server config.
        provider = str(call_ctx.provider or self.context.provider or "")
        model_name = str(call_ctx.model_name or self.context.model_name or "")
        try:
            if call_ctx.messages_override is not None:
                messages = list(call_ctx.messages_override)
                record_history = bool(call_ctx.record_history_override)
            else:
                special_messages, special_record_history, special_error = build_special_mode_messages(
                    user_input=call_ctx.user_input,
                    stream=call_ctx.stream,
                    minimal_classifier=call_ctx.minimal_classifier,
                    freedom_combined_review=call_ctx.freedom_combined_review,
                    session_summary_mode=call_ctx.session_summary_mode,
                    memory_query_expansion_mode=call_ctx.memory_query_expansion_mode,
                    workspace_root=self.context.workspace_root,
                    self_repo_root=self.context.self_repo_root,
                )
                if special_error:
                    return AIResult(text="", error_code="API_ERROR")
                if special_messages is not None:
                    messages = special_messages
                    record_history = special_record_history
                else:
                    messages, record_history = self.context.regular_message_builder(
                        call_ctx.user_input, call_ctx.context
                    )
                if call_ctx.record_history_override is not None:
                    record_history = bool(call_ctx.record_history_override)

            if not provider or not model_name:
                return AIResult(text="", error_code="API_ERROR")

            internal_mode = any(
                (
                    call_ctx.freedom_combined_review,
                    call_ctx.minimal_classifier,
                    call_ctx.session_summary_mode,
                    call_ctx.memory_query_expansion_mode,
                )
            )
            image_data, image_user_idx, image_user_text, image_error = prepare_image_input(
                image_path=call_ctx.image_path,
                messages=messages,
                internal_mode=internal_mode,
            )
            if image_error:
                return AIResult(text="", error_code="API_ERROR")

            def _append_history(
                ai_response: str,
                message: Optional[Dict[str, Any]] = None,
            ) -> None:
                if not record_history:
                    return
                # Record user messages (including tool-result intermediates) so
                # replayed history prefixes match cache units.  Tool-result user
                # messages are flagged ``_internal`` so the display can hide them.
                #
                # ``content``  = clean display text (e.g. the raw user question)
                # ``_api_content`` = exact text sent in the API payload (for cache
                #                    prefix matching during history replay).
                _api_content = ""
                _injected_suffix = ""
                for _m in reversed(messages):
                    if isinstance(_m, dict) and str(_m.get("role", "")).strip().lower() == "user":
                        _api_content = str(_m.get("content", ""))
                        _injected_suffix = str(_m.get("_injected_suffix", ""))
                        break
                if call_ctx.history_skip_user:
                    if _injected_suffix:
                        self.context.history_writer("user", _api_content, _internal=True, context_suffix=_injected_suffix)
                else:
                    _clean = (
                        call_ctx.history_user_input
                        if call_ctx.history_user_input is not None
                        else call_ctx.user_input
                    )
                    if _api_content and _api_content != _clean:
                        self.context.history_writer("user", _clean, api_content=_api_content)
                    else:
                        self.context.history_writer("user", _clean)
                assistant_text = str(ai_response or "")
                tool_calls_data: Any = None
                cache_stats: Any = None
                output_tokens: Optional[int] = None
                reasoning_tokens: Optional[int] = None
                token_count_includes_reasoning: Optional[bool] = None
                reply_events: Optional[List[Tuple[str, Any, str]]] = None
                if isinstance(message, dict):
                    tool_calls_data = message.get("tool_calls")
                    # Deduplicate tool_calls with identical function content
                    if isinstance(tool_calls_data, list) and len(tool_calls_data) > 1:
                        _seen_tc: Set[str] = set()
                        _deduped_tc: List[Dict[str, Any]] = []
                        for _tc in tool_calls_data:
                            if not isinstance(_tc, dict):
                                _deduped_tc.append(_tc)
                                continue
                            _fn = _tc.get("function", {})
                            _key = json.dumps(
                                {"name": _fn.get("name"), "arguments": _fn.get("arguments")},
                                sort_keys=True,
                                ensure_ascii=False,
                            )
                            if _key not in _seen_tc:
                                _seen_tc.add(_key)
                                _deduped_tc.append(_tc)
                        if len(_deduped_tc) != len(tool_calls_data):
                            _AI_HISTORY_LOG.debug(
                                "Deduplicated tool_calls: original=%d -> kept=%d",
                                len(tool_calls_data),
                                len(_deduped_tc),
                            )
                            tool_calls_data = _deduped_tc
                            message["tool_calls"] = _deduped_tc
                    cache_stats = message.get("_cache_stats")
                    output_tokens = message.get("_output_tokens")
                    reasoning_tokens = message.get("_reasoning_tokens")
                    token_count_includes_reasoning = message.get("_token_count_includes_reasoning")
                    reply_events = message.pop("_reply_events", None)
                if not assistant_text.strip() and not tool_calls_data and not reply_events:
                    _AI_HISTORY_LOG.warning(
                        "llm-history empty-assistant skipped provider=%s model=%s stream=%s return_message=%s history_skip_user=%s",
                        provider,
                        model_name,
                        bool(call_ctx.stream),
                        bool(call_ctx.return_message),
                        bool(call_ctx.history_skip_user),
                    )
                    return
                reply_records = _build_reply_records(message, reply_events, tool_calls_data)
                if not reply_records:
                    _AI_HISTORY_LOG.warning(
                        "llm-history no-reply-records skipped provider=%s model=%s stream=%s",
                        provider,
                        model_name,
                        bool(call_ctx.stream),
                    )
                    return
                self.context.history_writer(
                    "assistant",
                    "",
                    cache_stats=cache_stats,
                    output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens,
                    token_count_includes_reasoning=token_count_includes_reasoning,
                    reply_records=reply_records,
                )

            provider_ctx = ProviderCallContext(
                provider=provider,
                model_name=model_name,
                model_params=call_ctx.model_params or self.context.model_params,
                openai_conf=call_ctx.openai_conf or self.context.openai_conf,
                messages=messages,
                stream=call_ctx.stream,
                return_message=call_ctx.return_message,
                image_data=image_data,
                image_user_idx=image_user_idx,
                image_user_text=image_user_text,
                session_summary_mode=call_ctx.session_summary_mode,
                memory_query_expansion_mode=call_ctx.memory_query_expansion_mode,
                tool_schemas=call_ctx.tool_schemas,
                tool_choice=call_ctx.tool_choice,
                display_language=self.context.display_language,
            )
            raw = call_ai_with_provider(
                context=provider_ctx,
                append_history=_append_history,
                ollama_importer=self.context.ollama_importer,
            )
            if raw is None:
                return AIResult(text="")
            if isinstance(raw, dict):
                return AIResult(text=str(raw.get("content", "")))
            if isinstance(raw, str):
                return AIResult(text=raw)
            return raw
        except ModelCallError as e:
            clean_msg = _extract_clean_api_error(e) or str(e)
            sink = self.context.ephemeral_notice_writer
            if callable(sink):
                try:
                    sink(clean_msg)
                except Exception:
                    pass
            # Persist to chat history for GUI rendering.
            write_hist = self.context.model_error_history_writer
            if callable(write_hist):
                try:
                    display_msg = clean_msg if clean_msg else str(e)
                    write_hist(display_msg)
                except Exception:
                    pass
            return AIResult(text="", error_code="API_ERROR")
        except Exception as e:
            return AIResult(text="", error_code="API_ERROR")


_API_ERROR_PREFIX = "❌ API error: "


def _extract_clean_api_error(error: ModelCallError) -> str:
    """Extract the human-readable API error message from a failed model call.

    Walks the captured attempt errors looking for an ``OpenAIRequestError``
    whose response body carries a JSON ``error.message`` field. Falls back
    to the exception string when no structured message is available.
    """
    for attempt in (error.attempt_errors or []):
        err_text = str(attempt.get("error") or "").strip()
        if "response_body=" in err_text:
            idx = err_text.find("response_body=")
            body = err_text[idx + len("response_body="):]
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        err_obj = parsed.get("error")
                        if isinstance(err_obj, dict):
                            msg = str(err_obj.get("message") or "").strip()
                            if msg:
                                return msg
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
    for attempt in (error.attempt_errors or []):
        err_text = str(attempt.get("error") or "").strip()
        if "response_body=" not in err_text and err_text:
            return err_text
    return str(error)
