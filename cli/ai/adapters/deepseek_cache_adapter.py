"""Cache adapter for DeepSeek API cache-hit statistics.

DeepSeek API (identified by base_url containing ``api.deepseek.com``) reports
prompt-cache hit/miss tokens in the ``usage`` section of the response body.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from cli.ai.cache_adapter import BaseCacheAdapter


class DeepSeekCacheAdapter(BaseCacheAdapter):
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
        if "prompt_cache_hit_tokens" not in usage and "prompt_cache_miss_tokens" not in usage:
            return None
        hit_val = _safe_int(usage.get("prompt_cache_hit_tokens"))
        miss_val = _safe_int(usage.get("prompt_cache_miss_tokens"))
        return {
            "prompt_cache_hit_tokens": hit_val,
            "prompt_cache_miss_tokens": miss_val,
        }

    @staticmethod
    def matches(base_url: str) -> bool:
        url_lower = str(base_url or "").strip().lower()
        return "api.deepseek.com" in url_lower


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
