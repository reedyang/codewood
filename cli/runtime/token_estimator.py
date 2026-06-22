"""Shared token-estimation logic.

Token counting is needed both when assembling the model context
(:class:`~cli.runtime.llm_context_manager.LLMContextManager`) and by the
session-memory service for usage accounting. Centralizing it here guarantees
both paths reuse identical estimation behavior (tokenizer warmup + heuristic
fallback), so usage percentages and budgeting stay consistent.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Optional

from ..config.app_info import get_app_logger_root


class TokenEstimator:
    """Estimate token counts for text and chat messages.

    Prefers an agent-supplied ``token_estimator`` callable, then a warmed-up
    ``tiktoken`` (cl100k_base) counter, and finally a CJK-aware character
    heuristic. The tokenizer warmup runs in a background thread so the first
    callers fall back to the heuristic instead of blocking.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._builtin_token_counter: Optional[Callable[[str], int]] = None
        self._builtin_token_counter_init_done = False
        self._token_counter_warmup_started = False
        self._token_counter_lock = threading.Lock()
        self._start_token_counter_warmup()

    def _start_token_counter_warmup(self) -> None:
        with self._token_counter_lock:
            if self._token_counter_warmup_started:
                return
            self._token_counter_warmup_started = True

        def _run() -> None:
            try:
                import tiktoken  # type: ignore

                enc = tiktoken.get_encoding("cl100k_base")
                self._builtin_token_counter = lambda s: len(enc.encode(str(s or "")))
            except Exception:
                self._builtin_token_counter = None
            finally:
                self._builtin_token_counter_init_done = True

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"{get_app_logger_root()}-token-counter-warmup",
        ).start()

    def resolve_token_counter(self) -> Optional[Callable[[str], int]]:
        custom = getattr(self.agent, "token_estimator", None)
        if callable(custom):
            return custom
        if self._builtin_token_counter_init_done:
            return self._builtin_token_counter
        # Non-blocking: warmup continues in background; foreground falls back
        # to heuristic estimation until tokenizer is ready.
        self._start_token_counter_warmup()
        return None

    def estimate_text_tokens(self, text: str) -> int:
        s = str(text or "")
        if not s:
            return 0
        counter = self.resolve_token_counter()
        if callable(counter):
            try:
                n = int(counter(s))
                if n >= 0:
                    return n
            except Exception:
                pass
        cjk = len(re.findall(r"[\u3400-\u9fff]", s))
        other = max(0, len(s) - cjk)
        # CJK average token density is higher; ASCII often ~= 4 chars per token.
        return cjk + max(1, (other + 3) // 4)

    def estimate_message_tokens(self, role: str, content: str) -> int:
        return 6 + self.estimate_text_tokens(role) + self.estimate_text_tokens(content)

    def clip_text_to_token_budget(self, text: str, max_tokens: int) -> str:
        s = str(text or "")
        if max_tokens <= 0 or not s:
            return ""
        if self.estimate_text_tokens(s) <= max_tokens:
            return s
        lo, hi = 0, len(s)
        best = ""
        suffix = "…"
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = s[:mid].rstrip()
            if mid < len(s):
                candidate = (candidate + suffix) if candidate else suffix
            if self.estimate_text_tokens(candidate) <= max_tokens:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best
