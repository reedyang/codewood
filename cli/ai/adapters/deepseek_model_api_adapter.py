"""Model API adapter for DeepSeek API cache-hit statistics.

DeepSeek API (identified by base_url containing ``api.deepseek.com``) reports
prompt-cache hit/miss tokens in the ``usage`` section of the response body.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from cli.ai.model_api_adapter import BaseModelApiAdapter


class DeepSeekModelApiAdapter(BaseModelApiAdapter):
    """Extract cache stats from DeepSeek API responses.

    DeepSeek returns these fields in ``response["usage"]``:
      - ``prompt_cache_hit_tokens``
      - ``prompt_cache_miss_tokens``

    Both are integers representing the token counts for input cache hits/misses.
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
        result = self._try_root_tokens_only(usage)
        if result is not None:
            return {"input_tokens": result}
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
