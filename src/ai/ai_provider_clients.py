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


def _sanitize_assistant_text(text: Any) -> str:
    if not isinstance(text, str) or not text:
        return ""
    return _THINK_TAG_RE.sub("", text)


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
    append_history: Callable[[str], None],
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
                delta = _sanitize_assistant_text(_extract_stream_text_delta(payload))
                if delta:
                    if first_chunk:
                        delta = delta.lstrip()
                        first_chunk = False
                    if delta:
                        buffer += delta
                        yield delta
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
            append_history(buffer)

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
    text = str(raw or "").strip().lower()
    if text in ("", "auto"):
        return "auto"
    if text in ("chat", "chat_completions", "chat/completions", "completions"):
        return "chat"
    if text in ("response", "responses"):
        return "responses"
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
    append_history: Callable[[str], None],
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
    append_history(ai_response)
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
    append_history: Callable[[str], None],
):
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
        if first_error is not None:
            raise first_error
        raise RuntimeError("OpenAI request failed and no alternate URL strategy is available.")

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
        if first_error is not None:
            raise second_error from first_error
        raise second_error


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
    append_history: Callable[[str], None],
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
        except Exception as e:
            last_error = e
            _OPENAI_ROUTE_LOG.warning(
                "openai-route kind-failed model=%s api_kind=%s error=%s",
                model_name,
                api_kind,
                str(e),
            )
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("OpenAI request failed: no API mode candidates were available.")


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
    append_history: Callable[[str], None],
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
        response, fallback_error = _post_ollama_chat(fallback_payload)
        if fallback_error is not None:
            return _format_ollama_http_error(url, request_error, tool_request_response)
    elif request_error is not None:
        return _format_ollama_http_error(url, request_error, response)

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
                try:
                    for chunk in _iter_stream_payloads():
                        message = chunk.get("message")
                        if not isinstance(message, dict):
                            message = {}
                        final_role = str(message.get("role") or final_role)
                        current_tool_calls = _normalize_ollama_tool_calls(message.get("tool_calls"))
                        if current_tool_calls:
                            tool_calls = current_tool_calls
                        delta = _sanitize_assistant_text(message.get("content", "") or "")
                        if delta:
                            if first_chunk:
                                delta = delta.lstrip()
                                first_chunk = False
                            if delta:
                                buffer += delta
                                yield delta
                    completed = True
                finally:
                    self.final_message = {
                        "role": final_role,
                        "content": buffer,
                    }
                    if tool_calls:
                        self.final_message["tool_calls"] = tool_calls
                    if completed:
                        append_history(buffer)
                    self.close()

        return _OllamaStreamResult()

    try:
        response_data = response.json()
    except Exception as e:
        return f"❌ Error parsing Ollama HTTP response at {url}: {str(e)}"
    message = _extract_message_from_ollama_response_data(response_data)
    ai_response = str(message.get("content", "") or "")
    append_history(ai_response)
    return message if return_message else ai_response


def call_ai_with_provider(
    *,
    context: ProviderCallContext,
    append_history: Callable[[str], None],
    ollama_importer: Callable[[], Any],
):
    if context.provider == "openai" and context.openai_conf:
        return _call_with_openai_compatible(
            model_name=context.model_name,
            conf=context.openai_conf,
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
            api_key_error_msg="❌ Error: OpenAI API key is not configured. Please set api_key in config.jsonc model.params.",
            default_base_url="https://api.openai.com/v1",
        )
    if context.provider == "ollama":
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
    return f"❌ Error: unsupported model provider '{context.provider}'. Supported providers: ollama, openai"
