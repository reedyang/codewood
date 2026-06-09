import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.app_info import get_app_config_dirname, get_app_logger_root
from ..core.config.model_providers import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_OLLAMA_PORT,
    parse_context_window,
    parse_extra_headers,
    parse_port,
)
from ..core.logging.app_logging import get_logger


_OPENAI_API_ROUTE_CACHE_FILE = "openai_api_route_cache.json"
_OPENAI_API_ROUTE_CACHE_LOCK = threading.Lock()
_OPENAI_API_ROUTE_CACHE_LOADED = False
_OPENAI_API_ROUTE_CACHE: Dict[str, Any] = {"prefer_no_suffix": {}}
_OPENAI_ROUTE_LOG = get_logger(f"{get_app_logger_root()}.openai_route")
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_CHANNEL_THOUGHT_RE = re.compile(
    r"<\|channel\>\s*thought[\s\S]*?<channel\|>", flags=re.IGNORECASE
)
# Orphan markers: when the provider strips the matching half (e.g., reasoning
# content is hidden upstream), the dangling sentinel literal would otherwise
# leak to the terminal. These are always sentinel tokens — they have no
# legitimate use in user-facing assistant text — so it is safe to remove them
# unconditionally after the paired-block regexes have run.
_ORPHAN_HIDDEN_MARKER_RE = re.compile(
    r"<\|channel\>\s*thought|<channel\|>|</?think\s*>",
    flags=re.IGNORECASE,
)

# Stream-aware sanitizer entry per hidden block:
#   - opener_re: matches the literal that introduces the hidden block.
#   - closer_re: matches the corresponding terminator.
#   - opener_prefix_re: matches any *prefix* of opener_re anchored at end-of-text.
#     Used while streaming to decide whether a trailing fragment could still grow
#     into a real opener and therefore must be withheld from the user-visible
#     stream until more bytes arrive.
# All patterns are case-insensitive because models occasionally emit different
# casings; opener_re/closer_re are also DOTALL-ready by virtue of using [\s\S]
# in the consumer logic, while these literals are simple.
_STREAM_HIDDEN_BLOCKS: List[Tuple[re.Pattern, re.Pattern, re.Pattern]] = [
    (
        re.compile(r"<think>", re.IGNORECASE),
        re.compile(r"</think>", re.IGNORECASE),
        # Any non-empty prefix of "<think>" anchored at end of text.
        re.compile(r"<(?:t(?:h(?:i(?:n(?:k>?)?)?)?)?)?\Z", re.IGNORECASE),
    ),
    (
        re.compile(r"<\|channel\>\s*thought", re.IGNORECASE),
        re.compile(r"<channel\|>", re.IGNORECASE),
        # Any non-empty prefix of "<|channel>" optionally followed by whitespace
        # and an optional prefix of "thought", anchored at end of text. We must
        # also withhold a partial closer "<channel|>" (prefix at end of text)
        # because the closer of one block looks similar to the opener.
        re.compile(
            r"(?:<(?:\|(?:c(?:h(?:a(?:n(?:n(?:e(?:l(?:>(?:\s*t(?:h(?:o(?:u(?:g(?:h(?:t)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)\Z",
            re.IGNORECASE,
        ),
    ),
]

# Additional prefix matchers for closer literals. While inside a hidden block
# we discard everything anyway, so closer-prefix lookahead is only relevant
# at top level (we should not flush a partial closer like ``<channel`` because
# in some streams the closer of a hidden block could appear without a clearly
# matched opener due to provider re-segmentation; flushing it would leak it).
_STREAM_CLOSER_PREFIX_RE: List[re.Pattern] = [
    re.compile(r"</(?:t(?:h(?:i(?:n(?:k>?)?)?)?)?)?\Z", re.IGNORECASE),
    re.compile(
        r"<(?:c(?:h(?:a(?:n(?:n(?:e(?:l(?:\|(?:>)?)?)?)?)?)?)?)?)?\Z",
        re.IGNORECASE,
    ),
]


def _sanitize_assistant_text(text: Any) -> str:
    if not isinstance(text, str) or not text:
        return ""
    text = _THINK_TAG_RE.sub("", text)
    text = _CHANNEL_THOUGHT_RE.sub("", text)
    # After paired-block removal, kill any dangling sentinel literals so an
    # unbalanced opener or closer (provider-side filtering, model error, etc.)
    # never reaches the user.
    text = _ORPHAN_HIDDEN_MARKER_RE.sub("", text)
    return text


class _StreamingSanitizer:
    """Stateful sanitizer that strips hidden ``<think>...</think>`` and
    ``<|channel>thought ... <channel|>`` blocks from a streamed assistant
    text even when the open/close markers are split across chunk boundaries.

    Per-delta sanitization with a stateless regex was insufficient because a
    chunk could carry only the opener while the closer arrives several chunks
    later, leaving the marker visible to the user (e.g., a leaked
    ``<|channel>thought\\n<channel|>`` showing up in the terminal).

    The sanitizer holds back a small tail of recent text whenever it could
    still become part of a known opener (or partial closer at top level),
    and consumes everything between an opener and its closer once both have
    been observed.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._closer: Optional[re.Pattern] = None

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str) or not delta:
            return ""
        self._pending += delta
        return self._consume()

    def flush(self) -> str:
        # End-of-stream: drop any unterminated hidden block. Otherwise emit
        # the remaining buffer after scrubbing it through the stateless
        # sanitizer (which kills complete orphan markers). Finally, also
        # drop any trailing fragment that is clearly an incomplete sentinel
        # prefix (length >= 2) — at end-of-stream those can never grow into
        # a legitimate marker and emitting them would leak sentinel text.
        # A bare ``<`` is preserved because it is more likely a real
        # character (math, code, comparisons) than an aborted sentinel.
        if self._closer is not None:
            self._pending = ""
            self._closer = None
            return ""
        scrubbed = _sanitize_assistant_text(self._pending)
        self._pending = ""
        keep_suffix = self._suspect_suffix_length(scrubbed)
        if keep_suffix >= 2:
            scrubbed = scrubbed[: len(scrubbed) - keep_suffix]
        return scrubbed

    def _consume(self) -> str:
        out_parts: List[str] = []
        while True:
            if self._closer is not None:
                m = self._closer.search(self._pending)
                if m is None:
                    return "".join(out_parts)
                self._pending = self._pending[m.end():]
                self._closer = None
                continue

            earliest_match: Optional[re.Match] = None
            earliest_closer: Optional[re.Pattern] = None
            earliest_kind: str = ""
            for opener_re, closer_re, _prefix_re in _STREAM_HIDDEN_BLOCKS:
                m = opener_re.search(self._pending)
                if m is None:
                    continue
                if earliest_match is None or m.start() < earliest_match.start():
                    earliest_match = m
                    earliest_closer = closer_re
                    earliest_kind = "opener"
            # Also detect orphan closer literals at top level. If a closer
            # appears with no matching opener in our pending buffer, the
            # provider must have stripped the opener (e.g., reasoning content
            # was hidden upstream). The sentinel is never legitimate visible
            # text, so we drop just the closer literal and keep the surrounding
            # text. We pick the earliest such hit so it competes with opener
            # matches above and we always make progress on the leftmost
            # marker first.
            for _opener_re, closer_re, _prefix_re in _STREAM_HIDDEN_BLOCKS:
                m = closer_re.search(self._pending)
                if m is None:
                    continue
                if earliest_match is None or m.start() < earliest_match.start():
                    earliest_match = m
                    earliest_closer = closer_re
                    earliest_kind = "orphan_closer"

            if earliest_match is not None:
                out_parts.append(self._pending[: earliest_match.start()])
                self._pending = self._pending[earliest_match.end():]
                if earliest_kind == "opener":
                    self._closer = earliest_closer
                # For orphan_closer we just dropped the literal and stay at
                # top level.
                continue

            keep = self._suspect_suffix_length(self._pending)
            if keep > 0:
                emit_to = len(self._pending) - keep
                out_parts.append(self._pending[:emit_to])
                self._pending = self._pending[emit_to:]
            else:
                out_parts.append(self._pending)
                self._pending = ""
            return "".join(out_parts)

    @staticmethod
    def _suspect_suffix_length(text: str) -> int:
        """Return how many trailing chars in ``text`` could still grow into a
        known opener (or a stray closer prefix at top level). The matchers are
        anchored with ``\\Z`` so they only match suffixes.
        """
        if not text:
            return 0
        best = 0
        for _opener_re, _closer_re, prefix_re in _STREAM_HIDDEN_BLOCKS:
            m = prefix_re.search(text)
            if m is not None:
                length = m.end() - m.start()
                if length > best:
                    best = length
        for prefix_re in _STREAM_CLOSER_PREFIX_RE:
            m = prefix_re.search(text)
            if m is not None:
                length = m.end() - m.start()
                if length > best:
                    best = length
        return best


def _make_stream_sanitizer() -> _StreamingSanitizer:
    return _StreamingSanitizer()


@dataclass(frozen=True)
class AICallContext:
    user_input: str
    context: str = ""
    stream: bool = False
    minimal_classifier: bool = False
    freedom_combined_review: bool = False
    return_message: bool = False
    reflection_mode: bool = False
    session_summary_mode: bool = False
    memory_query_expansion_mode: bool = False
    image_path: Optional[str] = None
    history_user_input: Optional[str] = None
    history_skip_user: bool = False
    tool_schemas: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = None
    messages_override: Optional[List[Dict[str, Any]]] = None
    record_history_override: Optional[bool] = None


@dataclass(frozen=True)
class ProviderCallContext:
    provider: str
    model_name: str
    model_params: Optional[Dict[str, Any]]
    openai_conf: Optional[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    stream: bool
    return_message: bool
    image_data: Optional[str]
    image_user_idx: Optional[int]
    image_user_text: str
    session_summary_mode: bool
    memory_query_expansion_mode: bool
    tool_schemas: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = None


class OpenAIRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_body: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.url = url


class ModelCallError(RuntimeError):
    """Raised when every retry strategy for a model call has been exhausted.

    Carries the full per-attempt error trail so the UI can show every
    failed attempt instead of just the last one. Each entry in
    ``attempt_errors`` is a dict with at least ``label`` and ``error``
    keys; optional fields like ``url`` are kept verbatim for display.
    """

    def __init__(
        self,
        message: str,
        *,
        attempt_errors: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        super().__init__(message)
        self.attempt_errors: List[Dict[str, str]] = list(attempt_errors or [])


def _openai_api_route_cache_path() -> Path:
    return (Path.home() / get_app_config_dirname() / _OPENAI_API_ROUTE_CACHE_FILE).resolve()


def _load_openai_api_route_cache_locked() -> None:
    global _OPENAI_API_ROUTE_CACHE_LOADED, _OPENAI_API_ROUTE_CACHE
    if _OPENAI_API_ROUTE_CACHE_LOADED:
        return
    _OPENAI_API_ROUTE_CACHE_LOADED = True
    path = _openai_api_route_cache_path()
    try:
        if not path.exists():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            prefer_no_suffix = raw.get("prefer_no_suffix")
            if isinstance(prefer_no_suffix, dict):
                _OPENAI_API_ROUTE_CACHE["prefer_no_suffix"] = {
                    str(k): bool(v) for k, v in prefer_no_suffix.items()
                }
    except Exception:
        pass


def _save_openai_api_route_cache_locked() -> None:
    path = _openai_api_route_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "prefer_no_suffix": dict(_OPENAI_API_ROUTE_CACHE.get("prefer_no_suffix") or {}),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _openai_route_cache_key(base_url: str, model_name: str, api_kind: str) -> str:
    base_norm = str(base_url or "").strip().rstrip("/").casefold()
    model_norm = str(model_name or "").strip().casefold()
    api_norm = str(api_kind or "").strip().casefold()
    return f"{api_norm}|{model_norm}|{base_norm}"


def _openai_get_prefer_no_suffix(base_url: str, model_name: str, api_kind: str) -> bool:
    key = _openai_route_cache_key(base_url=base_url, model_name=model_name, api_kind=api_kind)
    with _OPENAI_API_ROUTE_CACHE_LOCK:
        _load_openai_api_route_cache_locked()
        prefer = bool((_OPENAI_API_ROUTE_CACHE.get("prefer_no_suffix") or {}).get(key, False))
    _OPENAI_ROUTE_LOG.info(
        "openai-route cache-lookup model=%s api_kind=%s prefer_no_suffix=%s base_url=%s",
        model_name,
        api_kind,
        prefer,
        str(base_url or "").rstrip("/"),
    )
    return prefer


def _openai_set_prefer_no_suffix(
    base_url: str,
    model_name: str,
    api_kind: str,
    *,
    prefer_no_suffix: bool,
) -> None:
    key = _openai_route_cache_key(base_url=base_url, model_name=model_name, api_kind=api_kind)
    with _OPENAI_API_ROUTE_CACHE_LOCK:
        _load_openai_api_route_cache_locked()
        mapping = _OPENAI_API_ROUTE_CACHE.setdefault("prefer_no_suffix", {})
        if not isinstance(mapping, dict):
            mapping = {}
            _OPENAI_API_ROUTE_CACHE["prefer_no_suffix"] = mapping
        prev = bool(mapping.get(key, False))
        if prefer_no_suffix:
            mapping[key] = True
        else:
            mapping.pop(key, None)
        if prev != bool(mapping.get(key, False)):
            _save_openai_api_route_cache_locked()
            _OPENAI_ROUTE_LOG.info(
                "openai-route cache-update model=%s api_kind=%s prefer_no_suffix=%s base_url=%s",
                model_name,
                api_kind,
                bool(mapping.get(key, False)),
                str(base_url or "").rstrip("/"),
            )


def prepare_image_input(
    image_path: Optional[str],
    messages: List[Dict[str, Any]],
    internal_mode: bool,
) -> Tuple[Optional[str], Optional[int], str, Optional[str]]:
    if image_path is None:
        return None, None, "", None
    if internal_mode:
        return None, None, "", "❌ Error: image input is not supported in the current internal mode."

    import base64

    with open(str(image_path), "rb") as image_file:
        image_data = base64.b64encode(image_file.read()).decode("utf-8")
    image_user_idx = None
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            image_user_idx = idx
            break
    if image_user_idx is None:
        return None, None, "", "❌ Error: failed to build multimodal message because no user message was found."
    image_user_text = str(messages[image_user_idx].get("content", "") or "")
    return image_data, image_user_idx, image_user_text, None


def _stringify_tool_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return ""


def _stream_tool_call_state_key(item: Dict[str, Any], fallback_index: int = 0) -> str:
    idx = item.get("index")
    if isinstance(idx, int) and idx >= 0:
        return f"idx:{idx}"
    output_index = item.get("output_index")
    if isinstance(output_index, int) and output_index >= 0:
        return f"idx:{output_index}"
    for key in ("call_id", "id", "item_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return f"id:{value}"
    return f"idx:{max(0, int(fallback_index or 0))}"


def _ensure_stream_tool_call_state(
    states: Dict[str, Dict[str, Any]],
    order: List[str],
    key: str,
) -> Dict[str, Any]:
    state = states.get(key)
    if state is None:
        state = {"id": "", "name": "", "arguments": "", "type": "function"}
        states[key] = state
        order.append(key)
    return state


def _update_stream_tool_call_state(
    state: Dict[str, Any],
    *,
    item: Dict[str, Any],
    append_arguments: bool,
) -> None:
    call_id = str(item.get("call_id") or item.get("id") or item.get("item_id") or "").strip()
    if call_id:
        state["id"] = call_id
    fn = item.get("function")
    fn_name = ""
    fn_args: Any = None
    if isinstance(fn, dict):
        fn_name = str(fn.get("name") or "").strip()
        fn_args = fn.get("arguments")
    name = str(item.get("name") or fn_name or "").strip()
    if name:
        state["name"] = name
    raw_args = fn_args if fn_args is not None else item.get("arguments")
    args_text = _stringify_tool_arguments(raw_args)
    if not args_text:
        return
    if append_arguments:
        state["arguments"] = str(state.get("arguments") or "") + args_text
    else:
        state["arguments"] = args_text


def _collect_stream_tool_calls_from_payload(
    payload: Any,
    *,
    states: Dict[str, Dict[str, Any]],
    order: List[str],
) -> None:
    if not isinstance(payload, dict):
        return

    try:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                delta_obj = first.get("delta")
                if isinstance(delta_obj, dict):
                    tool_calls = delta_obj.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for idx, item in enumerate(tool_calls):
                            if not isinstance(item, dict):
                                continue
                            key = _stream_tool_call_state_key(item, fallback_index=idx)
                            state = _ensure_stream_tool_call_state(states, order, key)
                            _update_stream_tool_call_state(
                                state,
                                item=item,
                                append_arguments=True,
                            )
    except Exception:
        pass

    event_type = str(payload.get("type") or "").strip().lower()
    if "function_call_arguments.delta" in event_type:
        key = _stream_tool_call_state_key(payload)
        state = _ensure_stream_tool_call_state(states, order, key)
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            state["arguments"] = str(state.get("arguments") or "") + delta
        for key_name in ("name",):
            value = str(payload.get(key_name) or "").strip()
            if value:
                state[key_name] = value
    if "function_call_arguments.done" in event_type:
        key = _stream_tool_call_state_key(payload)
        state = _ensure_stream_tool_call_state(states, order, key)
        _update_stream_tool_call_state(state, item=payload, append_arguments=False)

    candidate_items: List[Dict[str, Any]] = []
    for field in ("item", "output_item"):
        value = payload.get(field)
        if isinstance(value, dict):
            candidate_items.append(value)
    if str(payload.get("type") or "").strip().lower() == "function_call":
        candidate_items.append(payload)

    for idx, item in enumerate(candidate_items):
        item_type = str(item.get("type") or "").strip().lower()
        if item_type != "function_call":
            continue
        key = _stream_tool_call_state_key(item, fallback_index=idx)
        state = _ensure_stream_tool_call_state(states, order, key)
        # Responses API item snapshots are authoritative; prefer overwrite.
        _update_stream_tool_call_state(state, item=item, append_arguments=False)


def _extract_stream_snapshot_message(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    def _text_from_part(value: Any) -> str:
        if isinstance(value, str):
            return _sanitize_assistant_text(value)
        if isinstance(value, list):
            return _extract_text_from_response_content(value)
        if not isinstance(value, dict):
            return ""
        item_type = str(value.get("type") or "").strip().lower()
        if "reasoning" in item_type:
            return ""
        for key in ("text", "output_text", "content"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return _sanitize_assistant_text(text)
            if isinstance(text, list):
                extracted = _extract_text_from_response_content(text)
                if extracted:
                    return extracted
        return ""

    def _non_empty_message(message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        has_text = isinstance(content, str) and bool(content)
        has_tools = bool(message.get("tool_calls"))
        if has_text or has_tools:
            return message
        return None

    for key in ("response", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            try:
                message = _extract_message_from_openai_response_data(nested)
            except Exception:
                message = None
            found = _non_empty_message(message)
            if found:
                return found

    if any(key in payload for key in ("choices", "output", "output_text")):
        try:
            message = _extract_message_from_openai_response_data(payload)
        except Exception:
            message = None
        found = _non_empty_message(message)
        if found:
            return found

    message = payload.get("message")
    if isinstance(message, dict):
        role = str(message.get("role") or "assistant")
        content_text = _text_from_part(message.get("content"))
        if content_text:
            return {"role": role, "content": content_text}

    if str(payload.get("type") or "").strip().lower() == "message":
        role = str(payload.get("role") or "assistant")
        content_text = _text_from_part(payload.get("content"))
        if content_text:
            return {"role": role, "content": content_text}

    for field in ("item", "output_item"):
        item = payload.get(field)
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "message":
            role = str(item.get("role") or "assistant")
            content_text = _text_from_part(item.get("content"))
            if content_text:
                return {"role": role, "content": content_text}
        if item_type == "function_call":
            try:
                message = _extract_message_from_openai_response_data({"output": [item]})
            except Exception:
                message = None
            found = _non_empty_message(message)
            if found:
                return found

    event_type = str(payload.get("type") or "").strip().lower()
    for key in ("part", "content_part"):
        content_part = payload.get(key)
        content_text = _text_from_part(content_part)
        if content_text:
            return {"role": "assistant", "content": content_text}

    if any(token in event_type for token in ("output_text", "content_part", "message")) and "delta" not in event_type:
        for key in ("text", "output_text"):
            text = payload.get(key)
            if isinstance(text, str) and text:
                return {"role": "assistant", "content": _sanitize_assistant_text(text)}

    content_text = _text_from_part(payload.get("content"))
    if content_text:
        return {"role": "assistant", "content": content_text}

    return None


def _build_stream_tool_calls_message(
    *,
    content: str,
    states: Dict[str, Dict[str, Any]],
    order: List[str],
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": str(content or "")}
    tool_calls: List[Dict[str, Any]] = []
    for idx, key in enumerate(order, start=1):
        state = states.get(key) or {}
        name = str(state.get("name") or "").strip()
        if not name:
            continue
        call_id = str(state.get("id") or "").strip() or f"call_{idx}"
        arguments = str(state.get("arguments") or "")
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments if arguments else "{}",
                },
            }
        )
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _stream_openai_like_response(
    resp: Any,
    append_history: Callable[..., None],
):
    def _extract_stream_text_delta(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        event_type = str(payload.get("type") or "").strip().lower()
        if "reasoning" in event_type:
            return ""
        try:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    delta_obj = first.get("delta")
                    if isinstance(delta_obj, dict):
                        content = delta_obj.get("content")
                        if isinstance(content, str):
                            return content
                        if isinstance(content, list):
                            pieces: List[str] = []
                            for item in content:
                                if not isinstance(item, dict):
                                    continue
                                item_type = str(item.get("type") or "").strip().lower()
                                if "reasoning" in item_type:
                                    continue
                                text = item.get("text")
                                if isinstance(text, str):
                                    pieces.append(text)
                            if pieces:
                                return "".join(pieces)
        except Exception:
            pass

        if "output_text.delta" in event_type:
            d = payload.get("delta")
            if isinstance(d, str):
                return d
        if event_type.endswith(".delta") and "output_text" in event_type:
            d = payload.get("delta")
            if isinstance(d, str):
                return d
        out = payload.get("output_text")
        if isinstance(out, str):
            return out
        return ""

    class _OpenAIStreamResult:
        def __init__(self) -> None:
            self.final_message: Optional[Dict[str, Any]] = None

        def __iter__(self):
            buffer = ""
            first_chunk = True
            snapshot_message: Optional[Dict[str, Any]] = None
            seen_event_types: List[str] = []
            seen_payload_keys: List[str] = []
            tool_call_states: Dict[str, Dict[str, Any]] = {}
            tool_call_order: List[str] = []
            sanitizer = _make_stream_sanitizer()

            def _emit(raw: str, *, first: bool) -> Tuple[str, bool]:
                """Run raw chunk through the streaming sanitizer; lstrip the
                first non-empty visible chunk to keep prior leading-trim
                behavior intact."""
                produced = sanitizer.feed(raw)
                if not produced:
                    return "", first
                if first:
                    produced = produced.lstrip()
                    if produced:
                        first = False
                return produced, first

            for line in resp.iter_lines():
                if not line:
                    continue
                if not isinstance(line, (bytes, bytearray)) or not line.startswith(b"data: "):
                    continue
                data = line[6:]
                if data.strip() == b"[DONE]":
                    break
                data_str = data.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(data_str)
                except Exception:
                    continue
                event_type = str(payload.get("type") or "").strip()
                if event_type and event_type not in seen_event_types:
                    seen_event_types.append(event_type)
                for key in payload.keys():
                    key_text = str(key)
                    if key_text not in seen_payload_keys:
                        seen_payload_keys.append(key_text)
                _collect_stream_tool_calls_from_payload(
                    payload,
                    states=tool_call_states,
                    order=tool_call_order,
                )
                current_snapshot = _extract_stream_snapshot_message(payload)
                if current_snapshot:
                    snapshot_message = current_snapshot
                raw_delta = _extract_stream_text_delta(payload)
                if raw_delta:
                    delta, first_chunk = _emit(raw_delta, first=first_chunk)
                    if delta:
                        buffer += delta
                        yield delta
            tail = sanitizer.flush()
            if tail:
                if first_chunk:
                    tail = tail.lstrip()
                    if tail:
                        first_chunk = False
                if tail:
                    buffer += tail
                    yield tail
            if snapshot_message:
                snapshot_text = _sanitize_assistant_text(
                    snapshot_message.get("content", "") or ""
                )
                if snapshot_text:
                    if not buffer:
                        snapshot_delta = snapshot_text.lstrip() if first_chunk else snapshot_text
                        if snapshot_delta:
                            buffer += snapshot_delta
                            yield snapshot_delta
                    elif snapshot_text.startswith(buffer):
                        snapshot_delta = snapshot_text[len(buffer) :]
                        if snapshot_delta:
                            buffer += snapshot_delta
                            yield snapshot_delta
                    else:
                        buffer = snapshot_text
            self.final_message = _build_stream_tool_calls_message(
                content=buffer,
                states=tool_call_states,
                order=tool_call_order,
            )
            if snapshot_message:
                snapshot_tools = snapshot_message.get("tool_calls")
                if snapshot_tools and not self.final_message.get("tool_calls"):
                    self.final_message["tool_calls"] = snapshot_tools
            if not buffer:
                _OPENAI_ROUTE_LOG.warning(
                    "openai-stream empty-output event_types=%s payload_keys=%s snapshot_seen=%s has_tool_calls=%s",
                    ",".join(seen_event_types[-12:]),
                    ",".join(seen_payload_keys[-20:]),
                    bool(snapshot_message),
                    bool(self.final_message.get("tool_calls")),
                )
            append_history(buffer, self.final_message)

    return _OpenAIStreamResult()


def _extract_text_from_response_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in ("output_text", "input_text", "text"):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(_sanitize_assistant_text(text))
    return "".join(parts)


def _normalize_tool_call_arguments(raw_args: Any) -> str:
    if isinstance(raw_args, str):
        return raw_args or "{}"
    if raw_args is None:
        return "{}"
    try:
        return json.dumps(raw_args, ensure_ascii=False)
    except Exception:
        return "{}"


def _normalize_ollama_tool_calls(raw_tool_calls: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_tool_calls, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_tool_calls, start=1):
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if isinstance(fn, dict):
            fn_name = str(fn.get("name") or item.get("name") or "").strip()
            raw_args = fn.get("arguments")
            if raw_args is None:
                raw_args = item.get("arguments")
        else:
            fn_name = str(item.get("name") or "").strip()
            raw_args = item.get("arguments")
        if not fn_name:
            continue
        call_id = str(item.get("id") or item.get("call_id") or "").strip() or f"call_{idx}"
        out.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": _normalize_tool_call_arguments(raw_args),
                },
            }
        )
    return out


def _extract_message_from_ollama_response_data(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Ollama response JSON root must be an object.")
    message = data.get("message")
    if not isinstance(message, dict):
        message = {}
    ai_response = _sanitize_assistant_text(message.get("content", "") or "")
    out: Dict[str, Any] = {
        "role": str(message.get("role") or "assistant"),
        "content": ai_response,
    }
    tool_calls = _normalize_ollama_tool_calls(message.get("tool_calls"))
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _normalize_openai_message_for_request(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip() or "user"
    normalized = dict(message)
    normalized["role"] = role
    content = normalized.get("content", "")
    if isinstance(content, list):
        has_media_parts = any(
            isinstance(item, dict)
            and str(item.get("type") or "") in ("image_url", "input_image", "input_file", "input_audio")
            for item in content
        )
        if not has_media_parts:
            normalized["content"] = _extract_text_from_response_content(content)
    elif content is None:
        normalized["content"] = ""
    elif isinstance(content, (dict, tuple, set)):
        normalized["content"] = json.dumps(content, ensure_ascii=False)
    else:
        normalized["content"] = str(content)
    return normalized


def _normalize_openai_messages_for_request(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for message in messages:
        item = _normalize_openai_message_for_request(message)
        if item is None:
            continue
        normalized.append(item)
    return normalized


def _normalize_openai_api_mode(raw: Any) -> str:
    """Normalize the ``api_mode`` field to one of: ``auto``, ``chat``,
    ``responses``, or ``ollama``.

    ``ollama`` selects the local Ollama HTTP API path; the other
    three select the OpenAI-compatible HTTP path (``auto`` lets the
    client probe ``/chat/completions`` and ``/responses`` based on
    the configured ``base_url`` suffix).
    """
    text = str(raw or "").strip().lower()
    if text in ("", "auto"):
        return "auto"
    if text in ("chat", "chat_completions", "chat/completions", "completions"):
        return "chat"
    if text in ("response", "responses"):
        return "responses"
    if text == "ollama":
        return "ollama"
    return "auto"


def resolve_api_mode(*, params: Any, provider: Any = "") -> str:
    """Pick the effective ``api_mode`` for a model.

    ``api_mode`` is the SOLE switch that selects the API call method
    (OpenAI-compatible vs Ollama-native). ``provider`` is now just a
    label/prefix used by the model selector; it does NOT participate
    in dispatch.

    To keep older configs (``provider: "ollama"`` without an
    ``api_mode`` field) working, this helper falls back to inferring
    ``ollama`` from the provider name. New configs should set
    ``api_mode: "ollama"`` explicitly.
    """
    raw_mode = ""
    if isinstance(params, dict):
        raw_mode = str(params.get("api_mode") or "").strip()
    if raw_mode:
        return _normalize_openai_api_mode(raw_mode)
    if str(provider or "").strip().lower() == "ollama":
        return "ollama"
    return "auto"


def _normalize_additional_drop_params(raw: Any) -> List[str]:
    values: List[str] = []
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
    else:
        return []
    out: List[str] = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_openai_tool_schemas(raw_tools: Any, api_kind: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "function").strip().lower()
        if item_type != "function":
            continue
        fn = item.get("function")
        fn_obj = fn if isinstance(fn, dict) else item
        fn_name = str(fn_obj.get("name") or "").strip()
        if not fn_name:
            continue
        fn_desc = str(fn_obj.get("description") or "").strip()
        fn_params = fn_obj.get("parameters")
        if not isinstance(fn_params, dict):
            fn_params = {"type": "object", "properties": {}}
        if api_kind == "responses":
            normalized: Dict[str, Any] = {
                "type": "function",
                "name": fn_name,
                "parameters": fn_params,
            }
            if fn_desc:
                normalized["description"] = fn_desc
            out.append(normalized)
            continue
        # chat/completions
        fn_payload: Dict[str, Any] = {
            "name": fn_name,
            "parameters": fn_params,
        }
        if fn_desc:
            fn_payload["description"] = fn_desc
        out.append({"type": "function", "function": fn_payload})
    return out


def _normalize_openai_tool_choice(raw_tool_choice: Any, api_kind: str) -> Any:
    if raw_tool_choice is None:
        return None
    if isinstance(raw_tool_choice, str):
        return raw_tool_choice.strip() or None
    if not isinstance(raw_tool_choice, dict):
        return None

    choice_type = str(raw_tool_choice.get("type") or "").strip().lower()
    if choice_type != "function":
        return raw_tool_choice

    if api_kind == "responses":
        fn_name = ""
        fn_node = raw_tool_choice.get("function")
        if isinstance(fn_node, dict):
            fn_name = str(fn_node.get("name") or "").strip()
        if not fn_name:
            fn_name = str(raw_tool_choice.get("name") or "").strip()
        if not fn_name:
            return None
        return {"type": "function", "name": fn_name}

    # chat/completions
    fn_node = raw_tool_choice.get("function")
    if isinstance(fn_node, dict):
        fn_name = str(fn_node.get("name") or "").strip()
        if fn_name:
            return {"type": "function", "function": {"name": fn_name}}
    fn_name = str(raw_tool_choice.get("name") or "").strip()
    if fn_name:
        return {"type": "function", "function": {"name": fn_name}}
    return None


def _merge_default_drop_params(
    payload: Dict[str, Any], configured_drop_params: List[str]
) -> List[str]:
    out: List[str] = []
    seen = set()
    tools_value = payload.get("tools")
    has_non_empty_tools = isinstance(tools_value, list) and len(tools_value) > 0

    for item in configured_drop_params:
        key = str(item).strip()
        if not key or key in seen:
            continue
        if has_non_empty_tools and key == "tools":
            continue
        seen.add(key)
        out.append(key)
    if not has_non_empty_tools and "tools" not in seen:
        out.append("tools")
    return out


def _base_url_suffix_hint(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/").casefold()
    if normalized.endswith("/responses"):
        return "responses"
    if normalized.endswith("/chat/completions"):
        return "chat"
    return ""


def _openai_api_order_for_mode(api_mode: str, base_url: str) -> List[str]:
    mode = _normalize_openai_api_mode(api_mode)
    if mode in ("chat", "responses"):
        return [mode]
    # ``ollama`` is dispatched on a different code path; if it
    # somehow reaches this OpenAI-compatible router, treat it the
    # same as ``auto`` so the call still has a chance to succeed
    # against a base_url that happens to expose chat/completions.
    hint = _base_url_suffix_hint(base_url)
    if hint == "responses":
        return ["responses", "chat"]
    if hint == "chat":
        return ["chat", "responses"]
    return ["chat", "responses"]


def _openai_url_suffix_for_kind(api_kind: str) -> str:
    if api_kind == "responses":
        return "/responses"
    return "/chat/completions"


def _build_openai_request_url(base_url: str, api_kind: str, append_suffix: bool) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not append_suffix:
        return base
    suffix = _openai_url_suffix_for_kind(api_kind)
    base_low = base.casefold()
    if base_low.endswith(suffix):
        return base
    return base + suffix


def _should_disable_thinking_for_openai_compatible(
    *,
    model_name: str,
    base_url: str,
) -> bool:
    model = str(model_name or "").strip().casefold()
    base = str(base_url or "").strip().casefold()
    if model.startswith("deepseek-"):
        return True
    return "deepseek" in base


def _build_openai_responses_input_messages(
    messages: List[Dict[str, Any]],
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content", "")
        if image_data is not None and image_user_idx is not None and idx == image_user_idx:
            parts = [
                {"type": "input_text", "text": str(image_user_text or "")},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{image_data}",
                },
            ]
            out.append({"type": "message", "role": role, "content": parts})
            continue
        if isinstance(content, list):
            parts: List[Dict[str, Any]] = []
            text_buffer = ""
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type in ("text", "input_text", "output_text", "reasoning_text"):
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        text_buffer += text
                elif item_type == "image_url":
                    image_block = item.get("image_url")
                    if isinstance(image_block, dict):
                        image_url = image_block.get("url")
                    else:
                        image_url = image_block
                    if isinstance(image_url, str) and image_url:
                        parts.append({"type": "input_image", "image_url": image_url})
            if text_buffer:
                parts.insert(0, {"type": "input_text", "text": text_buffer})
            if not parts:
                parts = [{"type": "input_text", "text": ""}]
            out.append({"type": "message", "role": role, "content": parts})
            continue
        if isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            text = str(content)
        out.append({"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]})
    return out


def _build_openai_payload(
    *,
    api_kind: str,
    model_name: str,
    messages: List[Dict[str, Any]],
    stream: bool,
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
    session_summary_mode: bool,
    memory_query_expansion_mode: bool,
    additional_drop_params: List[str],
    tool_schemas: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    force_disable_thinking: bool,
) -> Dict[str, Any]:
    tools_payload = _normalize_openai_tool_schemas(tool_schemas, api_kind=api_kind)
    tool_choice_payload = _normalize_openai_tool_choice(tool_choice, api_kind=api_kind)

    if api_kind == "responses":
        payload: Dict[str, Any] = {
            "model": model_name,
            "input": _build_openai_responses_input_messages(
                messages=messages,
                image_data=image_data,
                image_user_idx=image_user_idx,
                image_user_text=image_user_text,
            ),
            "stream": stream,
        }
        if tools_payload:
            payload["tools"] = tools_payload
            if tool_choice_payload is not None:
                payload["tool_choice"] = tool_choice_payload
        if session_summary_mode or memory_query_expansion_mode:
            payload["max_output_tokens"] = 512
        if memory_query_expansion_mode:
            payload["temperature"] = 0.2
        if force_disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if isinstance(payload.get("tools"), list) and not payload.get("tools"):
            payload.pop("tools", None)
        effective_drop_params = _merge_default_drop_params(
            payload=payload, configured_drop_params=additional_drop_params
        )
        if effective_drop_params:
            payload["additional_drop_params"] = effective_drop_params
        return payload

    payload = {"model": model_name, "messages": messages, "stream": stream}
    if tools_payload:
        payload["tools"] = tools_payload
        if tool_choice_payload is not None:
            payload["tool_choice"] = tool_choice_payload
    if session_summary_mode or memory_query_expansion_mode:
        payload["max_tokens"] = 512
    if memory_query_expansion_mode:
        payload["temperature"] = 0.2
    if force_disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    if isinstance(payload.get("tools"), list) and not payload.get("tools"):
        payload.pop("tools", None)
    effective_drop_params = _merge_default_drop_params(
        payload=payload, configured_drop_params=additional_drop_params
    )
    if effective_drop_params:
        payload["additional_drop_params"] = effective_drop_params
    return payload


def _truncate_error_body(raw: str, limit: int = 1200) -> str:
    text = str(raw or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _post_openai_request(
    *,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    stream: bool,
) -> Any:
    import requests

    resp = requests.post(url, headers=headers, json=payload, verify=False, timeout=120, stream=stream)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        status_code: Optional[int] = None
        try:
            status_code = int(resp.status_code)
        except Exception:
            status_code = None
        try:
            body = _truncate_error_body(resp.text or "")
        except Exception:
            body = ""
        msg = str(e)
        if body:
            msg = f"{msg}; response_body={body}"
        raise OpenAIRequestError(msg, status_code=status_code, response_body=body, url=url) from e
    return resp


def _call_openai_once(
    *,
    model_name: str,
    api_kind: str,
    url: str,
    headers: Dict[str, str],
    messages: List[Dict[str, Any]],
    stream: bool,
    return_message: bool,
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
    session_summary_mode: bool,
    memory_query_expansion_mode: bool,
    additional_drop_params: List[str],
    tool_schemas: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    force_disable_thinking: bool,
    append_history: Callable[..., None],
):
    payload = _build_openai_payload(
        api_kind=api_kind,
        model_name=model_name,
        messages=messages,
        stream=stream,
        image_data=image_data,
        image_user_idx=image_user_idx,
        image_user_text=image_user_text,
        session_summary_mode=session_summary_mode,
        memory_query_expansion_mode=memory_query_expansion_mode,
        additional_drop_params=additional_drop_params,
        tool_schemas=tool_schemas,
        tool_choice=tool_choice,
        force_disable_thinking=force_disable_thinking,
    )
    resp = _post_openai_request(url=url, headers=headers, payload=payload, stream=stream)
    if stream:
        return _stream_openai_like_response(
            resp=resp,
            append_history=append_history,
        )

    data = resp.json()
    message = _extract_message_from_openai_response_data(data)
    raw_content = message.get("content", "")
    if isinstance(raw_content, list):
        ai_response = _extract_text_from_response_content(raw_content)
    else:
        ai_response = _sanitize_assistant_text(raw_content or "")
    message = dict(message)
    message["content"] = ai_response
    if not ai_response:
        _OPENAI_ROUTE_LOG.warning(
            "openai-response empty-output api_kind=%s data_keys=%s message_keys=%s has_tool_calls=%s",
            api_kind,
            ",".join(sorted([str(k) for k in data.keys()])),
            ",".join(sorted([str(k) for k in message.keys()])),
            bool(message.get("tool_calls")),
        )
    append_history(ai_response, message)
    return message if return_message else ai_response


def _call_openai_with_suffix_strategy(
    *,
    model_name: str,
    api_kind: str,
    base_url: str,
    headers: Dict[str, str],
    messages: List[Dict[str, Any]],
    stream: bool,
    return_message: bool,
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
    session_summary_mode: bool,
    memory_query_expansion_mode: bool,
    additional_drop_params: List[str],
    tool_schemas: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    append_history: Callable[..., None],
):
    force_disable_thinking = _should_disable_thinking_for_openai_compatible(
        model_name=model_name,
        base_url=base_url,
    )
    prefer_no_suffix = _openai_get_prefer_no_suffix(
        base_url=base_url, model_name=model_name, api_kind=api_kind
    )
    primary_append = not prefer_no_suffix
    secondary_append = not primary_append

    primary_url = _build_openai_request_url(
        base_url=base_url, api_kind=api_kind, append_suffix=primary_append
    )
    secondary_url = _build_openai_request_url(
        base_url=base_url, api_kind=api_kind, append_suffix=secondary_append
    )

    first_error: Optional[Exception] = None
    _OPENAI_ROUTE_LOG.info(
        "openai-route try-primary model=%s api_kind=%s append_suffix=%s url=%s",
        model_name,
        api_kind,
        primary_append,
        primary_url,
    )
    try:
        return _call_openai_once(
            model_name=model_name,
            api_kind=api_kind,
            url=primary_url,
            headers=headers,
            messages=messages,
            stream=stream,
            return_message=return_message,
            image_data=image_data,
            image_user_idx=image_user_idx,
            image_user_text=image_user_text,
            session_summary_mode=session_summary_mode,
            memory_query_expansion_mode=memory_query_expansion_mode,
            additional_drop_params=additional_drop_params,
            tool_schemas=tool_schemas,
            tool_choice=tool_choice,
            force_disable_thinking=force_disable_thinking,
            append_history=append_history,
        )
    except Exception as e:
        first_error = e
        _OPENAI_ROUTE_LOG.warning(
            "openai-route primary-failed model=%s api_kind=%s append_suffix=%s url=%s error=%s",
            model_name,
            api_kind,
            primary_append,
            primary_url,
            str(e),
        )

    if secondary_url == primary_url:
        attempts: List[Dict[str, str]] = [
            {
                "label": f"{api_kind} {'with-suffix' if primary_append else 'no-suffix'}",
                "url": primary_url,
                "error": str(first_error) if first_error is not None else "",
            }
        ]
        if first_error is not None:
            raise ModelCallError(str(first_error), attempt_errors=attempts) from first_error
        raise ModelCallError(
            "OpenAI request failed and no alternate URL strategy is available.",
            attempt_errors=attempts,
        )

    try:
        _OPENAI_ROUTE_LOG.info(
            "openai-route try-secondary model=%s api_kind=%s append_suffix=%s url=%s",
            model_name,
            api_kind,
            secondary_append,
            secondary_url,
        )
        result = _call_openai_once(
            model_name=model_name,
            api_kind=api_kind,
            url=secondary_url,
            headers=headers,
            messages=messages,
            stream=stream,
            return_message=return_message,
            image_data=image_data,
            image_user_idx=image_user_idx,
            image_user_text=image_user_text,
            session_summary_mode=session_summary_mode,
            memory_query_expansion_mode=memory_query_expansion_mode,
            additional_drop_params=additional_drop_params,
            tool_schemas=tool_schemas,
            tool_choice=tool_choice,
            force_disable_thinking=force_disable_thinking,
            append_history=append_history,
        )
        if primary_append and not secondary_append:
            _openai_set_prefer_no_suffix(
                base_url=base_url,
                model_name=model_name,
                api_kind=api_kind,
                prefer_no_suffix=True,
            )
            _OPENAI_ROUTE_LOG.info(
                "openai-route switched-to-no-suffix model=%s api_kind=%s base_url=%s",
                model_name,
                api_kind,
                str(base_url or "").rstrip("/"),
            )
        elif not primary_append and secondary_append:
            _openai_set_prefer_no_suffix(
                base_url=base_url,
                model_name=model_name,
                api_kind=api_kind,
                prefer_no_suffix=False,
            )
            _OPENAI_ROUTE_LOG.info(
                "openai-route restored-default-suffix model=%s api_kind=%s base_url=%s",
                model_name,
                api_kind,
                str(base_url or "").rstrip("/"),
            )
        return result
    except Exception as second_error:
        _OPENAI_ROUTE_LOG.warning(
            "openai-route secondary-failed model=%s api_kind=%s append_suffix=%s url=%s error=%s",
            model_name,
            api_kind,
            secondary_append,
            secondary_url,
            str(second_error),
        )
        attempts: List[Dict[str, str]] = []
        if first_error is not None:
            attempts.append({
                "label": f"{api_kind} {'with-suffix' if primary_append else 'no-suffix'}",
                "url": primary_url,
                "error": str(first_error),
            })
        attempts.append({
            "label": f"{api_kind} {'with-suffix' if secondary_append else 'no-suffix'}",
            "url": secondary_url,
            "error": str(second_error),
        })
        if first_error is not None:
            raise ModelCallError(str(second_error), attempt_errors=attempts) from first_error
        raise ModelCallError(str(second_error), attempt_errors=attempts) from second_error


def _extract_message_from_openai_response_data(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("OpenAI response JSON root must be an object.")

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                return message

    output = data.get("output")
    if isinstance(output, list):
        content_text = ""
        role = "assistant"
        tool_calls: List[Dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type == "message":
                role = str(item.get("role") or role)
                piece = _extract_text_from_response_content(item.get("content"))
                if piece:
                    if content_text:
                        content_text += "\n"
                    content_text += piece
                continue
            if item_type == "function_call":
                fn_name = str(item.get("name") or "").strip()
                if not fn_name:
                    continue
                raw_args = item.get("arguments")
                if isinstance(raw_args, str):
                    fn_args = raw_args
                elif raw_args is None:
                    fn_args = "{}"
                else:
                    try:
                        fn_args = json.dumps(raw_args, ensure_ascii=False)
                    except Exception:
                        fn_args = "{}"
                call_id = str(item.get("call_id") or item.get("id") or "").strip()
                if not call_id:
                    call_id = f"call_{len(tool_calls) + 1}"
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        },
                    }
                )
        if content_text or tool_calls:
            message: Dict[str, Any] = {"role": role, "content": content_text}
            if tool_calls:
                message["tool_calls"] = tool_calls
            return message

    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return {"role": "assistant", "content": output_text}
    if isinstance(output_text, list):
        merged = "".join([part for part in output_text if isinstance(part, str)])
        if merged:
            return {"role": "assistant", "content": merged}

    keys = ", ".join(sorted([str(k) for k in data.keys()]))
    raise ValueError(
        "Unsupported OpenAI response format: expected 'choices' or Responses API 'output'. "
        f"Top-level keys: [{keys}]"
    )


def _call_with_openai_compatible(
    *,
    model_name: str,
    conf: Dict[str, Any],
    messages: List[Dict[str, Any]],
    stream: bool,
    return_message: bool,
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
    session_summary_mode: bool,
    memory_query_expansion_mode: bool,
    tool_schemas: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    append_history: Callable[..., None],
    api_key_error_msg: str,
    default_base_url: str,
):
    api_key = conf.get("api_key")
    base_url = conf.get("base_url", default_base_url)
    if not api_key:
        return api_key_error_msg

    provider_messages = _normalize_openai_messages_for_request(messages)
    if image_data is not None and image_user_idx is not None:
        provider_messages = [dict(m) for m in provider_messages]
        provider_messages[image_user_idx] = {
            **provider_messages[image_user_idx],
            "content": [
                {"type": "text", "text": image_user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }

    api_mode = _normalize_openai_api_mode(conf.get("api_mode"))
    additional_drop_params = _normalize_additional_drop_params(
        conf.get("additional_drop_params")
    )
    api_kinds = _openai_api_order_for_mode(api_mode, str(base_url or ""))
    _OPENAI_ROUTE_LOG.info(
        "openai-route dispatch model=%s api_mode=%s api_order=%s base_url=%s",
        model_name,
        api_mode,
        ",".join(api_kinds),
        str(base_url or "").rstrip("/"),
    )

    context_window = parse_context_window(
        conf.get("context_window"), default_value=DEFAULT_CONTEXT_WINDOW
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Context-Window": str(context_window),
    }
    extra_headers = parse_extra_headers(conf.get("extra_headers"))
    if extra_headers:
        existing_keys = {str(key).casefold() for key in headers.keys()}
        for key, value in extra_headers.items():
            normalized_key = str(key or "").strip()
            if not normalized_key:
                continue
            if normalized_key.casefold() in existing_keys:
                continue
            headers[normalized_key] = str(value)
            existing_keys.add(normalized_key.casefold())
    last_error: Optional[Exception] = None
    aggregated_attempts: List[Dict[str, str]] = []
    for api_kind in api_kinds:
        try:
            _OPENAI_ROUTE_LOG.info(
                "openai-route enter-kind model=%s api_kind=%s",
                model_name,
                api_kind,
            )
            return _call_openai_with_suffix_strategy(
                model_name=model_name,
                api_kind=api_kind,
                base_url=str(base_url),
                headers=headers,
                messages=provider_messages,
                stream=stream,
                return_message=return_message,
                image_data=image_data,
                image_user_idx=image_user_idx,
                image_user_text=image_user_text,
                session_summary_mode=session_summary_mode,
                memory_query_expansion_mode=memory_query_expansion_mode,
                additional_drop_params=additional_drop_params,
                tool_schemas=tool_schemas,
                tool_choice=tool_choice,
                append_history=append_history,
            )
        except ModelCallError as e:
            last_error = e
            aggregated_attempts.extend(e.attempt_errors)
            _OPENAI_ROUTE_LOG.warning(
                "openai-route kind-failed model=%s api_kind=%s error=%s",
                model_name,
                api_kind,
                str(e),
            )
            continue
        except Exception as e:
            last_error = e
            aggregated_attempts.append({
                "label": api_kind,
                "url": str(base_url or ""),
                "error": str(e),
            })
            _OPENAI_ROUTE_LOG.warning(
                "openai-route kind-failed model=%s api_kind=%s error=%s",
                model_name,
                api_kind,
                str(e),
            )
            continue

    if last_error is not None:
        raise ModelCallError(str(last_error), attempt_errors=aggregated_attempts) from last_error
    raise ModelCallError(
        "OpenAI request failed: no API mode candidates were available.",
        attempt_errors=aggregated_attempts,
    )


def _format_ollama_http_error(url: str, error: Exception, response: Any) -> str:
    detail = ""
    if response is not None:
        body = ""
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            body = text.strip()
        else:
            try:
                body = json.dumps(response.json(), ensure_ascii=False)
            except Exception:
                body = ""
        if body:
            if len(body) > 1000:
                body = f"{body[:1000]}..."
            detail = f" Response body: {body}"
    return f"❌ Error calling Ollama HTTP API at {url}: {str(error)}.{detail}"


def _call_with_ollama(
    *,
    model_name: str,
    messages: List[Dict[str, Any]],
    stream: bool,
    return_message: bool,
    image_data: Optional[str],
    image_user_idx: Optional[int],
    image_user_text: str,
    session_summary_mode: bool,
    memory_query_expansion_mode: bool,
    tool_schemas: Optional[List[Dict[str, Any]]],
    tool_choice: Any,
    append_history: Callable[..., None],
    ollama_importer: Callable[[], Any],
    context_window: int,
    model_params: Optional[Dict[str, Any]],
):
    if image_data is not None and image_user_idx is not None:
        provider_messages = [dict(m) for m in messages]
        provider_messages[image_user_idx] = {
            **provider_messages[image_user_idx],
            "content": image_user_text,
            "images": [image_data],
        }
    else:
        provider_messages = messages

    ollama_options: Dict[str, Any] = {"num_ctx": int(context_window)}
    ollama_tools = _normalize_openai_tool_schemas(tool_schemas, api_kind="chat")
    ollama_tool_choice = _normalize_openai_tool_choice(tool_choice, api_kind="chat")
    if session_summary_mode:
        ollama_options.update({"num_predict": 512, "temperature": 0.3})
    elif memory_query_expansion_mode:
        ollama_options.update({"num_predict": 512, "temperature": 0.2})

    params_for_port = model_params if isinstance(model_params, dict) else {}
    port = parse_port(params_for_port.get("port"), default_value=DEFAULT_OLLAMA_PORT)
    url = f"http://127.0.0.1:{port}/api/chat"

    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": provider_messages,
        "stream": bool(stream),
        "options": ollama_options,
    }
    if ollama_tools:
        payload["tools"] = ollama_tools
        if ollama_tool_choice is not None:
            payload["tool_choice"] = ollama_tool_choice

    import requests

    def _post_ollama_chat(request_payload: Dict[str, Any]):
        response_obj = None
        try:
            response_obj = requests.post(
                url,
                json=request_payload,
                timeout=120,
                stream=bool(stream),
            )
            response_obj.raise_for_status()
            return response_obj, None
        except requests.RequestException as e:
            return response_obj, e

    response, request_error = _post_ollama_chat(payload)
    if request_error is not None and payload.get("tools"):
        tool_request_response = response
        fallback_payload = dict(payload)
        fallback_payload.pop("tools", None)
        fallback_payload.pop("tool_choice", None)
        response, fallback_error = _post_ollama_chat(fallback_payload)
        if fallback_error is not None:
            primary_msg = _format_ollama_http_error(url, request_error, tool_request_response)
            fallback_msg = _format_ollama_http_error(url, fallback_error, response)
            attempts: List[Dict[str, str]] = [
                {"label": "ollama with-tools", "url": url, "error": primary_msg},
                {"label": "ollama no-tools", "url": url, "error": fallback_msg},
            ]
            raise ModelCallError(fallback_msg, attempt_errors=attempts)
    elif request_error is not None:
        msg = _format_ollama_http_error(url, request_error, response)
        raise ModelCallError(
            msg,
            attempt_errors=[{"label": "ollama", "url": url, "error": msg}],
        )

    if stream:
        def _iter_stream_payloads():
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                if isinstance(raw_line, (bytes, bytearray)):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                else:
                    line = str(raw_line or "").strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    line = line[6:].strip()
                if line == "[DONE]":
                    break
                try:
                    payload_obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload_obj, dict):
                    yield payload_obj

        class _OllamaStreamResult:
            def __init__(self) -> None:
                self.final_message: Optional[Dict[str, Any]] = None
                self._response = response

            def close(self) -> None:
                try:
                    self._response.close()
                except Exception:
                    pass

            def __iter__(self):
                buffer = ""
                first_chunk = True
                final_role = "assistant"
                tool_calls: List[Dict[str, Any]] = []
                completed = False
                sanitizer = _make_stream_sanitizer()
                try:
                    for chunk in _iter_stream_payloads():
                        message = chunk.get("message")
                        if not isinstance(message, dict):
                            message = {}
                        final_role = str(message.get("role") or final_role)
                        current_tool_calls = _normalize_ollama_tool_calls(message.get("tool_calls"))
                        if current_tool_calls:
                            tool_calls = current_tool_calls
                        raw_delta = message.get("content", "") or ""
                        delta = sanitizer.feed(raw_delta) if raw_delta else ""
                        if delta:
                            if first_chunk:
                                delta = delta.lstrip()
                                first_chunk = False
                            if delta:
                                buffer += delta
                                yield delta
                    tail = sanitizer.flush()
                    if tail:
                        if first_chunk:
                            tail = tail.lstrip()
                            first_chunk = False
                        if tail:
                            buffer += tail
                            yield tail
                    completed = True
                finally:
                    self.final_message = {
                        "role": final_role,
                        "content": buffer,
                    }
                    if tool_calls:
                        self.final_message["tool_calls"] = tool_calls
                    if completed:
                        append_history(buffer, self.final_message)
                    self.close()

        return _OllamaStreamResult()

    try:
        response_data = response.json()
    except Exception as e:
        msg = f"❌ Error parsing Ollama HTTP response at {url}: {str(e)}"
        raise ModelCallError(
            msg,
            attempt_errors=[{"label": "ollama parse", "url": url, "error": msg}],
        ) from e
    message = _extract_message_from_ollama_response_data(response_data)
    ai_response = str(message.get("content", "") or "")
    append_history(ai_response, message)
    return message if return_message else ai_response


def call_ai_with_provider(
    *,
    context: ProviderCallContext,
    append_history: Callable[..., None],
    ollama_importer: Callable[[], Any],
):
    """Dispatch to the right backend based on the resolved ``api_mode``.

    The previous implementation branched on ``context.provider``; that
    forced every label-different-but-API-same provider (e.g. local
    OpenAI-compatible gateways) to be hard-coded as ``"openai"``.
    Now ``provider`` is a free-form label and ``api_mode`` is the
    sole dispatch key (``"ollama"`` → local Ollama HTTP API; anything
    else → OpenAI-compatible HTTP API).
    """
    api_mode = resolve_api_mode(
        params=context.model_params or context.openai_conf,
        provider=context.provider,
    )
    if api_mode == "ollama":
        context_window = parse_context_window(
            ((context.model_params or {}).get("context_window")),
            default_value=DEFAULT_CONTEXT_WINDOW,
        )
        return _call_with_ollama(
            model_name=context.model_name,
            messages=context.messages,
            stream=context.stream,
            return_message=context.return_message,
            image_data=context.image_data,
            image_user_idx=context.image_user_idx,
            image_user_text=context.image_user_text,
            session_summary_mode=context.session_summary_mode,
            memory_query_expansion_mode=context.memory_query_expansion_mode,
            tool_schemas=context.tool_schemas,
            tool_choice=context.tool_choice,
            append_history=append_history,
            ollama_importer=ollama_importer,
            context_window=context_window,
            model_params=context.model_params,
        )
    # OpenAI-compatible HTTP path. When ``openai_conf`` is unset (e.g.
    # the agent was constructed with a non-``openai`` provider label
    # but the user explicitly chose ``api_mode: "chat"|"responses"``),
    # fall back to ``model_params`` so an api_key/base_url discovered
    # from there can still drive the call.
    conf = context.openai_conf or context.model_params
    if conf:
        return _call_with_openai_compatible(
            model_name=context.model_name,
            conf=conf,
            messages=context.messages,
            stream=context.stream,
            return_message=context.return_message,
            image_data=context.image_data,
            image_user_idx=context.image_user_idx,
            image_user_text=context.image_user_text,
            session_summary_mode=context.session_summary_mode,
            memory_query_expansion_mode=context.memory_query_expansion_mode,
            tool_schemas=context.tool_schemas,
            tool_choice=context.tool_choice,
            append_history=append_history,
            api_key_error_msg="❌ Error: OpenAI API key is not configured. Please set api_key in config.jsonc.",
            default_base_url="https://api.openai.com/v1",
        )
    return (
        f"❌ Error: cannot dispatch model call (provider='{context.provider}', "
        f"api_mode='{api_mode}'); set api_mode to 'chat'/'responses'/'ollama' "
        "and ensure the model params are configured."
    )
