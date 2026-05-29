import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.app_info import get_app_config_dirname, get_app_logger_root
from ..core.config.model_providers import DEFAULT_CONTEXT_WINDOW, parse_context_window
from ..core.logging.app_logging import get_logger


_OPENAI_API_ROUTE_CACHE_FILE = "openai_api_route_cache.json"
_OPENAI_API_ROUTE_CACHE_LOCK = threading.Lock()
_OPENAI_API_ROUTE_CACHE_LOADED = False
_OPENAI_API_ROUTE_CACHE: Dict[str, Any] = {"prefer_no_suffix": {}}
_OPENAI_ROUTE_LOG = get_logger(f"{get_app_logger_root()}.openai_route")


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
    domain_classifier_mode: bool = False
    image_path: Optional[str] = None
    history_user_input: Optional[str] = None
    history_skip_user: bool = False


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
    domain_classifier_mode: bool


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


def _stream_openai_like_response(
    resp: Any,
    append_history: Callable[[str], None],
):
    def gen():
        buffer = ""
        first_chunk = True
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
                delta = ""
                if isinstance(payload, dict):
                    try:
                        delta = payload["choices"][0]["delta"].get("content", "")
                    except Exception:
                        delta = ""
                    if not delta:
                        event_type = str(payload.get("type") or "")
                        if "output_text.delta" in event_type:
                            d = payload.get("delta")
                            if isinstance(d, str):
                                delta = d
                        elif event_type.endswith(".delta"):
                            d = payload.get("delta")
                            if isinstance(d, str):
                                delta = d
                        if not delta:
                            out = payload.get("output_text")
                            if isinstance(out, str):
                                delta = out
                if delta:
                    if first_chunk:
                        delta = delta.lstrip()
                        first_chunk = False
                    if delta:
                        buffer += delta
                        yield delta
            except Exception:
                continue
        append_history(buffer)

    return gen()


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
        if item_type in ("output_text", "input_text", "reasoning_text", "text"):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


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


def _merge_default_drop_params(
    payload: Dict[str, Any], configured_drop_params: List[str]
) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in configured_drop_params:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)

    tools_value = payload.get("tools")
    has_non_empty_tools = isinstance(tools_value, list) and len(tools_value) > 0
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
    domain_classifier_mode: bool,
    additional_drop_params: List[str],
) -> Dict[str, Any]:
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
        if session_summary_mode or memory_query_expansion_mode or domain_classifier_mode:
            payload["max_output_tokens"] = 512
        if memory_query_expansion_mode or domain_classifier_mode:
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
    if session_summary_mode or memory_query_expansion_mode or domain_classifier_mode:
        payload["max_tokens"] = 512
    if memory_query_expansion_mode or domain_classifier_mode:
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
    domain_classifier_mode: bool,
    additional_drop_params: List[str],
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
        domain_classifier_mode=domain_classifier_mode,
        additional_drop_params=additional_drop_params,
    )
    resp = _post_openai_request(url=url, headers=headers, payload=payload, stream=stream)
    if stream:
        return _stream_openai_like_response(
            resp=resp,
            append_history=append_history,
        )

    data = resp.json()
    message = _extract_message_from_openai_response_data(data)
    ai_response = message.get("content", "") or ""
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
    domain_classifier_mode: bool,
    additional_drop_params: List[str],
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
            domain_classifier_mode=domain_classifier_mode,
            additional_drop_params=additional_drop_params,
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
            domain_classifier_mode=domain_classifier_mode,
            additional_drop_params=additional_drop_params,
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
        for item in output:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "message":
                continue
            content_text = _extract_text_from_response_content(item.get("content"))
            if content_text:
                return {"role": str(item.get("role") or "assistant"), "content": content_text}
        for item in output:
            if not isinstance(item, dict):
                continue
            content_text = _extract_text_from_response_content(item.get("content"))
            if content_text:
                return {"role": str(item.get("role") or "assistant"), "content": content_text}

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
    domain_classifier_mode: bool,
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
                domain_classifier_mode=domain_classifier_mode,
                additional_drop_params=additional_drop_params,
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
    domain_classifier_mode: bool,
    append_history: Callable[[str], None],
    ollama_importer: Callable[[], Any],
    context_window: int,
):
    try:
        ollama = ollama_importer()
    except ImportError:
        return "❌ Error: the 'ollama' package is not installed. Please run: pip install ollama"

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
    if session_summary_mode:
        ollama_options.update({"num_predict": 512, "temperature": 0.3})
    elif memory_query_expansion_mode or domain_classifier_mode:
        ollama_options.update({"num_predict": 512, "temperature": 0.2})

    if stream:
        response = ollama.chat(
            model=model_name,
            messages=provider_messages,
            stream=True,
            options=ollama_options,
        )

        def gen():
            buffer = ""
            first_chunk = True
            for chunk in response:
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    if first_chunk:
                        delta = delta.lstrip()
                        first_chunk = False
                    if delta:
                        buffer += delta
                        yield delta
            append_history(buffer)

        return gen()

    chat_kwargs: Dict[str, Any] = {
        "model": model_name,
        "messages": provider_messages,
        "stream": False,
        "options": ollama_options,
    }
    response = ollama.chat(**chat_kwargs)
    message = response.get("message", {}) or {}
    ai_response = message.get("content", "") or ""
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
            domain_classifier_mode=context.domain_classifier_mode,
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
            domain_classifier_mode=context.domain_classifier_mode,
            append_history=append_history,
            ollama_importer=ollama_importer,
            context_window=context_window,
        )
    return f"❌ Error: unsupported model provider '{context.provider}'. Supported providers: ollama, openai"
