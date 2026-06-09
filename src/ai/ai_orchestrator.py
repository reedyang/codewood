import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.app_info import get_app_logger_root
from ..core.logging.app_logging import get_logger
from .ai_provider_clients import (
    AICallContext,
    ModelCallError,
    ProviderCallContext,
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
    work_directory: str
    history_writer: Callable[[str, str], None]
    regular_message_builder: Callable[[str, str], Tuple[List[Dict[str, Any]], bool]]
    ollama_importer: Callable[[], Any]
    # Optional sink for multi-attempt model-call error messages that should be
    # displayed on screen and survive terminal-resize redraws but must NOT be
    # persisted to chat history. Receives a single pre-formatted string.
    ephemeral_notice_writer: Optional[Callable[[str], None]] = None


class AIOrchestrator:
    def __init__(self, context: AgentAIContext) -> None:
        self.context = context

    def call(self, *, call_ctx: AICallContext):
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
                    reflection_mode=call_ctx.reflection_mode,
                    session_summary_mode=call_ctx.session_summary_mode,
                    memory_query_expansion_mode=call_ctx.memory_query_expansion_mode,
                    work_directory=str(self.context.work_directory),
                )
                if special_error:
                    return special_error
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
                return "❌ Error: model is not configured correctly. Please check the model settings in config.jsonc."

            internal_mode = any(
                (
                    call_ctx.freedom_combined_review,
                    call_ctx.minimal_classifier,
                    call_ctx.reflection_mode,
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
                return image_error

            def _append_history(
                ai_response: str,
                message: Optional[Dict[str, Any]] = None,
            ) -> None:
                if not record_history:
                    return
                if not call_ctx.history_skip_user:
                    _u = (
                        call_ctx.history_user_input
                        if call_ctx.history_user_input is not None
                        else call_ctx.user_input
                    )
                    self.context.history_writer("user", _u)
                assistant_text = str(ai_response or "")
                if not assistant_text.strip():
                    # When the model returned only standard `tool_calls` (no visible
                    # text content), persist a synthetic JSON plan so the chat
                    # history replay (`_parse_model_tool_plan_history_content`)
                    # can still surface the tool call. This keeps tools like
                    # `apply_patch` from disappearing from chat history when the
                    # provider omits a textual content payload.
                    plan_payload = _build_tool_calls_plan_payload(message)
                    if plan_payload:
                        self.context.history_writer("assistant", plan_payload)
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
                self.context.history_writer("assistant", assistant_text)

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
            )
            return call_ai_with_provider(
                context=provider_ctx,
                append_history=_append_history,
                ollama_importer=self.context.ollama_importer,
            )
        except ModelCallError as e:
            formatted = _format_model_call_error_for_display(
                e, provider=provider, model_name=model_name
            )
            sink = self.context.ephemeral_notice_writer
            if callable(sink):
                try:
                    sink(formatted)
                except Exception:
                    pass
            # Return a one-line summary so callers that simply render the
            # returned string still show something. The full multi-attempt
            # detail is delivered through ``ephemeral_notice_writer`` so
            # it can survive terminal-resize redraws without being
            # persisted to chat history.
            return (
                f"Error calling LLM API: {str(e)} "
                f"(provider: {provider}, model: {model_name})"
            )
        except Exception as e:
            return f"Error calling LLM API: {str(e)} (provider: {provider}, model: {model_name})"


def _format_model_call_error_for_display(
    error: ModelCallError, *, provider: str, model_name: str
) -> str:
    """Render every captured attempt as its own line so the user can see
    exactly which retry strategies were tried and why each one failed."""
    header = (
        f"❌ Model call failed (provider: {provider}, model: {model_name}). "
        f"Tried {len(error.attempt_errors)} attempt(s):"
        if error.attempt_errors
        else f"❌ Model call failed (provider: {provider}, model: {model_name}): {str(error)}"
    )
    if not error.attempt_errors:
        return header
    lines = [header]
    for idx, attempt in enumerate(error.attempt_errors, start=1):
        label = str(attempt.get("label") or "").strip()
        url = str(attempt.get("url") or "").strip()
        err_text = str(attempt.get("error") or "").strip()
        head = f"  {idx}."
        if label:
            head += f" [{label}]"
        if url:
            head += f" {url}"
        lines.append(head)
        for err_line in err_text.splitlines() or [""]:
            lines.append(f"     {err_line}")
    return "\n".join(lines)
