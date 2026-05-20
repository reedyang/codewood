import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.config.model_providers import DEFAULT_CONTEXT_WINDOW, parse_context_window


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
    openwebui_conf: Optional[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    stream: bool
    return_message: bool
    image_data: Optional[str]
    image_user_idx: Optional[int]
    image_user_text: str
    session_summary_mode: bool
    memory_query_expansion_mode: bool
    domain_classifier_mode: bool


def prepare_image_input(
    image_path: Optional[str],
    messages: List[Dict[str, Any]],
    internal_mode: bool,
) -> Tuple[Optional[str], Optional[int], str, Optional[str]]:
    if image_path is None:
        return None, None, "", None
    if internal_mode:
        return None, None, "", "❌ 错误：当前内部模式不支持图片输入。"

    import base64

    with open(str(image_path), "rb") as image_file:
        image_data = base64.b64encode(image_file.read()).decode("utf-8")
    image_user_idx = None
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            image_user_idx = idx
            break
    if image_user_idx is None:
        return None, None, "", "❌ 错误：多模态消息构建失败，缺少用户消息。"
    image_user_text = str(messages[image_user_idx].get("content", "") or "")
    return image_data, image_user_idx, image_user_text, None


def _stream_openai_like_response(
    resp: Any,
    append_history: Callable[[str], None],
    decode_unicode: bool = False,
):
    def gen():
        buffer = ""
        first_chunk = True
        iter_kwargs = {"decode_unicode": True} if decode_unicode else {}
        for line in resp.iter_lines(**iter_kwargs):
            if not line:
                continue
            if decode_unicode:
                if not isinstance(line, str) or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                data_str = data
            else:
                if not isinstance(line, (bytes, bytearray)) or not line.startswith(b"data: "):
                    continue
                data = line[6:]
                if data.strip() == b"[DONE]":
                    break
                data_str = data.decode("utf-8", errors="replace")
            try:
                delta = json.loads(data_str)["choices"][0]["delta"].get("content", "")
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
    stream_decode_unicode: bool,
):
    import requests

    api_key = conf.get("api_key")
    base_url = conf.get("base_url", default_base_url)
    if not api_key:
        return api_key_error_msg

    payload: Dict[str, Any] = {"model": model_name, "messages": messages, "stream": stream}
    if image_data is not None and image_user_idx is not None:
        provider_messages = [dict(m) for m in messages]
        provider_messages[image_user_idx] = {
            **provider_messages[image_user_idx],
            "content": [
                {"type": "text", "text": image_user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }
        payload["messages"] = provider_messages
    if session_summary_mode or memory_query_expansion_mode or domain_classifier_mode:
        payload["max_tokens"] = 512
    if memory_query_expansion_mode or domain_classifier_mode:
        payload["temperature"] = 0.2

    url = base_url.rstrip("/") + "/chat/completions"
    context_window = parse_context_window(
        conf.get("context_window"), default_value=DEFAULT_CONTEXT_WINDOW
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Context-Window": str(context_window),
    }
    resp = requests.post(url, headers=headers, json=payload, verify=False, timeout=120, stream=stream)
    resp.raise_for_status()
    if stream:
        return _stream_openai_like_response(
            resp=resp,
            append_history=append_history,
            decode_unicode=stream_decode_unicode,
        )

    data = resp.json()
    message = data["choices"][0]["message"]
    ai_response = message.get("content", "") or ""
    append_history(ai_response)
    return message if return_message else ai_response


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
        return "❌ 错误：未安装 ollama 包。请运行：pip install ollama"

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
            api_key_error_msg="❌ 错误：OpenAI API密钥未配置。请在 config.json 的 model.params 中设置 api_key。",
            default_base_url="https://api.openai.com/v1",
            stream_decode_unicode=False,
        )
    if context.provider == "openwebui" and context.openwebui_conf:
        return _call_with_openai_compatible(
            model_name=context.model_name,
            conf=context.openwebui_conf,
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
            api_key_error_msg="❌ 错误：OpenWebUI API密钥未配置。请在 config.json 的 model.params 中设置 api_key。",
            default_base_url="http://localhost:8080/v1",
            stream_decode_unicode=True,
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
    return f"❌ 错误：不支持的模型提供者 '{context.provider}'。支持的提供者：ollama, openai, openwebui"
