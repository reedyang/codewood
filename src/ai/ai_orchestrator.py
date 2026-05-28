from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai_provider_clients import (
    AICallContext,
    ProviderCallContext,
    call_ai_with_provider,
    prepare_image_input,
)
from .ai_special_mode_prompts import build_special_mode_messages


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


class AIOrchestrator:
    def __init__(self, context: AgentAIContext) -> None:
        self.context = context

    def call(self, *, call_ctx: AICallContext):
        provider = str(self.context.provider or "")
        model_name = str(self.context.model_name or "")
        try:
            special_messages, special_record_history, special_error = build_special_mode_messages(
                user_input=call_ctx.user_input,
                stream=call_ctx.stream,
                minimal_classifier=call_ctx.minimal_classifier,
                freedom_combined_review=call_ctx.freedom_combined_review,
                reflection_mode=call_ctx.reflection_mode,
                session_summary_mode=call_ctx.session_summary_mode,
                memory_query_expansion_mode=call_ctx.memory_query_expansion_mode,
                domain_classifier_mode=call_ctx.domain_classifier_mode,
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

            if not provider or not model_name:
                return "❌ Error: model is not configured correctly. Please check the model settings in config.json."

            internal_mode = any(
                (
                    call_ctx.freedom_combined_review,
                    call_ctx.minimal_classifier,
                    call_ctx.reflection_mode,
                    call_ctx.session_summary_mode,
                    call_ctx.memory_query_expansion_mode,
                    call_ctx.domain_classifier_mode,
                )
            )
            image_data, image_user_idx, image_user_text, image_error = prepare_image_input(
                image_path=call_ctx.image_path,
                messages=messages,
                internal_mode=internal_mode,
            )
            if image_error:
                return image_error

            def _append_history(ai_response: str) -> None:
                if not record_history:
                    return
                if not call_ctx.history_skip_user:
                    _u = (
                        call_ctx.history_user_input
                        if call_ctx.history_user_input is not None
                        else call_ctx.user_input
                    )
                    self.context.history_writer("user", _u)
                self.context.history_writer("assistant", ai_response)

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
                domain_classifier_mode=call_ctx.domain_classifier_mode,
            )
            return call_ai_with_provider(
                context=provider_ctx,
                append_history=_append_history,
                ollama_importer=self.context.ollama_importer,
            )
        except Exception as e:
            return f"Error calling LLM API: {str(e)} (provider: {provider}, model: {model_name})"
