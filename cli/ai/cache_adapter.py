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


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0


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

    def extract_output_usage(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, int]]:
        """Extract output-token counts from an API response.

        Returns a dict with keys ``output_tokens`` and ``reasoning_tokens``,
        or ``None`` when the response does not contain output-token info.

        The default implementation tries the standard OpenAI fields:

        * ``usage.output_tokens`` / ``usage.completion_tokens`` \
          (whichever exists)
        * ``usage.completion_tokens_details.reasoning_tokens`` / \
          ``usage.output_tokens_details.reasoning_tokens`` \
          (whichever exists; ``0`` if neither)
        """
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            return None

        output_tokens = _safe_int(usage.get("output_tokens"))
        if not output_tokens:
            output_tokens = _safe_int(usage.get("completion_tokens"))
        if not output_tokens:
            return None

        reasoning_tokens = 0
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            reasoning_tokens = _safe_int(details.get("reasoning_tokens"))
        if not reasoning_tokens:
            details = usage.get("output_tokens_details")
            if isinstance(details, dict):
                reasoning_tokens = _safe_int(details.get("reasoning_tokens"))

        return {"output_tokens": output_tokens, "reasoning_tokens": reasoning_tokens}


class CacheAdapterManager:
    """Registry of cache adapters resolved by base URL."""

    def __init__(self) -> None:
        from cli.ai.adapters.deepseek_cache_adapter import DeepSeekCacheAdapter
        from cli.ai.adapters.openai_cache_adapter import OpenAICacheAdapter
        from cli.ai.adapters.fallback_cache_adapter import FallbackCacheAdapter

        self._adapters: List[BaseCacheAdapter] = [
            DeepSeekCacheAdapter(),
            OpenAICacheAdapter(),
            FallbackCacheAdapter(),
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
