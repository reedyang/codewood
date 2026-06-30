"""Fallback cache adapter that probes unknown APIs using known usage structures.

Attempts to extract cache stats by trying the Chat Completions format first,
then the Responses API format.  ``supports_cache_stats()`` returns based on
whether the last extraction succeeded — pessimistic after first failure,
optimistic again after a success.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from cli.ai.cache_adapter import BaseCacheAdapter


class FallbackCacheAdapter(BaseCacheAdapter):
    """Use known OpenAI usage structures to extract cache stats from any API.

    Extraction is attempted in order:

    1. Chat Completions: ``usage.prompt_tokens_details.cached_tokens``
    2. Responses API:    ``usage.input_tokens_details.cached_tokens``
    3. Root tokens only: ``usage.prompt_tokens`` or ``usage.input_tokens``
       (when neither ``*_tokens_details.cached_tokens`` is available)

    When only root-level ``input_tokens`` / ``prompt_tokens`` are found (no
    cache-breakdown details), the result dict carries an ``input_tokens`` key
    instead of ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.

    ``supports_cache_stats()`` returns the result of the most recent extraction:
    ``True`` after a successful extraction, ``False`` otherwise.  Initial state
    is ``True`` (optimistic).
    """

    def __init__(self) -> None:
        self._last_extraction_success = True

    def supports_cache_stats(self) -> bool:
        return self._last_extraction_success

    def extract_cache_stats(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, int]]:
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            self._last_extraction_success = False
            return None

        result = self._try_chat_completions(usage)
        if result is not None:
            hit, miss = result
            self._last_extraction_success = True
            return {"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}

        result = self._try_responses(usage)
        if result is not None:
            hit, miss = result
            self._last_extraction_success = True
            return {"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}

        result = self._try_root_tokens_only(usage)
        if result is not None:
            self._last_extraction_success = True
            return {"input_tokens": result}

        self._last_extraction_success = False
        return None

    @staticmethod
    def _try_chat_completions(usage: Dict[str, Any]) -> Optional[tuple[int, int]]:
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict) or "cached_tokens" not in details:
            return None
        hit = _safe_int(details.get("cached_tokens"))
        total = _safe_int(usage.get("prompt_tokens"))
        miss = max(0, total - hit)
        return (hit, miss)

    @staticmethod
    def _try_responses(usage: Dict[str, Any]) -> Optional[tuple[int, int]]:
        details = usage.get("input_tokens_details")
        if not isinstance(details, dict) or "cached_tokens" not in details:
            return None
        hit = _safe_int(details.get("cached_tokens"))
        total = _safe_int(usage.get("input_tokens"))
        miss = max(0, total - hit)
        return (hit, miss)

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
        return True


def _safe_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
