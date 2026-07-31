import json
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
        provider = str(self.context.provider or "")
        model_name = str(self.context.model_name or "")
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
                clean_content: Optional[str] = None
                output_tokens: Optional[int] = None
                reasoning_tokens: Optional[int] = None
                token_count_includes_reasoning: Optional[bool] = None
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
                    clean_content = message.get("_clean_content")
                    output_tokens = message.get("_output_tokens")
                    reasoning_tokens = message.get("_reasoning_tokens")
                    token_count_includes_reasoning = message.get("_token_count_includes_reasoning")
                if not assistant_text.strip():
                    plan_payload = _build_tool_calls_plan_payload(message)
                    if plan_payload:
                        self.context.history_writer("assistant", plan_payload, tool_calls=tool_calls_data, cache_stats=cache_stats, clean_content=clean_content, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens, token_count_includes_reasoning=token_count_includes_reasoning)
                        return
                    _AI_HISTORY_LOG.warning(
                        "llm-history empty-assistant skipped provider=%s model=%s stream=%s return_message=%s history_skip_user=%s",
                        provider,
                        model_name,
                        bool(call_ctx.stream),
                        bool(call_ctx.return_message),
                        bool(call_ctx.history_skip_user),
                    )
                    return
                self.context.history_writer("assistant", assistant_text, tool_calls=tool_calls_data, cache_stats=cache_stats, clean_content=clean_content, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens, token_count_includes_reasoning=token_count_includes_reasoning, thinking=message.get("_thinking", "") if isinstance(message, dict) else None, thinking_from_content=message.get("_thinking_from_content") if isinstance(message, dict) else None)

            provider_ctx = ProviderCallContext(
                provider=provider,
                model_name=model_name,
                model_params=self.context.model_params,
                openai_conf=self.context.openai_conf,
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
