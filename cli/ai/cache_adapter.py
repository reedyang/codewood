"""Provider-specific cache-hit statistics extraction via Adapter pattern.

DeepSeek API (identified by base_url containing ``api.deepseek.com``) reports
prompt-cache hit/miss tokens in the ``usage`` section of the response body.
Other providers may add similar fields in future — each adapter implements
detection and extraction for one provider family.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


class BaseCacheAdapter(ABC):
    """Extract cache-hit statistics from a provider's API response."""

    @abstractmethod
    def supports_cache_stats(self) -> bool:
        """Return True when this adapter is operational (e.g. feature flags)."""
        ...

    @abstractmethod
    def extract_cache_stats(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, int]]:
        """Extract ``{prompt_cache_hit_tokens, prompt_cache_miss_tokens}``.

        Returns None when the response does not contain cache stats.
        """
        ...

    @staticmethod
    @abstractmethod
    def matches(base_url: str) -> bool:
        """Return True if this adapter should handle the given base URL."""
        ...


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


class CacheAdapterManager:
    """Registry of cache adapters resolved by base URL."""

    def __init__(self) -> None:
        self._adapters: List[BaseCacheAdapter] = [
            DeepSeekCacheAdapter(),
        ]

    def register(self, adapter: BaseCacheAdapter) -> None:
        self._adapters.append(adapter)

    def resolve(self, base_url: str) -> Optional[BaseCacheAdapter]:
        for adapter in self._adapters:
            if adapter.matches(base_url):
                if adapter.supports_cache_stats():
                    return adapter
                return None
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
