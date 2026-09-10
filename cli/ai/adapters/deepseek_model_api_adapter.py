"""Model API adapter for DeepSeek API cache-hit statistics.

DeepSeek API (identified by base_url containing ``api.deepseek.com``) reports
prompt-cache statistics in the ``usage`` section of the response body, using a
different shape per endpoint:

* ``/chat/completions`` reports the hit/miss split explicitly as
  ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.
* ``/responses`` reports the OpenAI-compatible ``input_tokens`` plus the cached
  portion in ``input_tokens_details.cached_tokens`` (the miss count is the
  difference between the two).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from cli.ai.model_api_adapter import BaseModelApiAdapter


class DeepSeekModelApiAdapter(BaseModelApiAdapter):
    """Extract cache stats from DeepSeek API responses.

    ``/chat/completions`` returns these fields in ``response[\"usage\"]``:
      - ``prompt_cache_hit_tokens``
      - ``prompt_cache_miss_tokens``

    ``/responses`` returns instead::

        usage.input_tokens                        →  total input
        usage.input_tokens_details.cached_tokens  →  hit
        total - hit                               →  miss

    All are integers representing the token counts for input cache hits/misses.
    """

    def supports_cache_stats(self) -> bool:
        return True

    def extract_cache_stats(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, int]]:
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            return None
        if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
            hit_val = _safe_int(usage.get("prompt_cache_hit_tokens"))
            miss_val = _safe_int(usage.get("prompt_cache_miss_tokens"))
            return {
                "prompt_cache_hit_tokens": hit_val,
                "prompt_cache_miss_tokens": miss_val,
            }
        result = self._try_token_details(usage)
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
    def _try_token_details(usage: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        """Extract cached tokens from a ``*_tokens_details`` sub-object.

        Handles both the Responses API naming (``input_tokens_details`` over
        ``input_tokens``) and the Chat Completions naming
        (``prompt_tokens_details`` over ``prompt_tokens``).

        Returns ``(cached_tokens, total_input_tokens)`` or None when neither
        sub-object carries a ``cached_tokens`` field.
        """
        for details_key, root_key in (
            ("input_tokens_details", "input_tokens"),
            ("prompt_tokens_details", "prompt_tokens"),
        ):
            details = usage.get(details_key)
            if isinstance(details, dict) and "cached_tokens" in details:
                return (_safe_int(details.get("cached_tokens")), _safe_int(usage.get(root_key)))
        return None

    @staticmethod
    def _try_root_tokens_only(usage: Dict[str, Any]) -> Optional[int]:
        """Extract total input tokens when no cache-breakdown fields are
        available.  Tries ``input_tokens`` first, then ``prompt_tokens``."""
        if "input_tokens" in usage:
            return _safe_int(usage["input_tokens"])
        if "prompt_tokens" in usage:
            return _safe_int(usage["prompt_tokens"])
        return None

    @staticmethod
    def matches(base_url: str) -> bool:
        url_lower = str(base_url or "").strip().lower()
        return "api.deepseek.com" in url_lower


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
