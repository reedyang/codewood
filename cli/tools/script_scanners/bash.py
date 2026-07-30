"""Bash / sh script scanner (placeholder)."""

from __future__ import annotations

from typing import Set

from .base import ScriptTargetScanner
from .registry import _registry


class BashScriptScanner(ScriptTargetScanner):
    """Scans Bash (``.sh``, ``.bash``) scripts for file I/O targets.

    Currently a placeholder — static analysis of shell scripts for file
    operations is non-trivial and not yet implemented.
    """

    @classmethod
    def extensions(cls) -> Set[str]:
        return {".sh", ".bash", ".zsh", ".ksh", ".fish"}

    @classmethod
    def interpreter_names(cls) -> Set[str]:
        return {"bash", "sh", "zsh", "ksh", "dash", "fish"}

    @classmethod
    def inline_flags(cls) -> Set[str]:
        return {"-c"}

    def _extract_raw_paths(self, source: str) -> Set[str]:
        return set()


_registry.register(BashScriptScanner)
