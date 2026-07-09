"""Model API adapter for OpenAI API cache-hit statistics.

Supports both the Chat Completions API and the Responses API — the two APIs
use different ``usage`` field structures.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from cli.ai.model_api_adapter import BaseModelApiAdapter


class OpenAIModelApiAdapter(BaseModelApiAdapter):
    """Extract cache stats from OpenAI API responses.

    **Chat Completions API**::

        usage.prompt_tokens_details.cached_tokens  →  hit
        usage.prompt_tokens - hit                  →  miss

    **Responses API**::

        usage.input_tokens_details.cached_tokens   →  hit
        usage.input_tokens - hit                   →  miss
    """

    def supports_cache_stats(self) -> bool:
        return True

    def extract_cache_stats(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, int]]:
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            return None

        result = self._try_chat_completions(usage)
        if result is not None:
            cached_tokens, total_input = result
            return {
                "prompt_cache_hit_tokens": cached_tokens,
                "prompt_cache_miss_tokens": max(0, total_input - cached_tokens),
            }

        result = self._try_responses(usage)
        if result is not None:
            cached_tokens, total_input = result
            return {
                "prompt_cache_hit_tokens": cached_tokens,
                "prompt_cache_miss_tokens": max(0, total_input - cached_tokens),
            }

        result = self._try_root_tokens_only(usage)
        if result is not None:
            return {"input_tokens": result}

        return None

    @staticmethod
    def _try_chat_completions(usage: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        """Try to extract cached_tokens and prompt_tokens from Chat Completions usage.

        Returns ``(cached_tokens, prompt_tokens)`` or None.
        """
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict) or "cached_tokens" not in details:
            return None
        cached = _safe_int(details.get("cached_tokens"))
        total_input = _safe_int(usage.get("prompt_tokens"))
        return (cached, total_input)

    @staticmethod
    def _try_responses(usage: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        """Try to extract cached_tokens and input_tokens from Responses API usage.

        Returns ``(cached_tokens, input_tokens)`` or None.
        """
        details = usage.get("input_tokens_details")
        if not isinstance(details, dict) or "cached_tokens" not in details:
            return None
        cached = _safe_int(details.get("cached_tokens"))
        total_input = _safe_int(usage.get("input_tokens"))
        return (cached, total_input)

    @staticmethod
    def _try_root_tokens_only(usage: Dict[str, Any]) -> Optional[int]:
        """Extract total input tokens when no ``*_tokens_details.cached_tokens``
        is available.  Tries ``input_tokens`` first (Responses API naming),
        then ``prompt_tokens`` (Chat Completions naming)."""
        if "input_tokens" in usage:
            return _safe_int(usage["input_tokens"])
        if "prompt_tokens" in usage:
            return _safe_int(usage["prompt_tokens"])
        return None

    @staticmethod
    def matches(base_url: str) -> bool:
        url_lower = str(base_url or "").strip().lower()
        return "api.openai.com" in url_lower


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
