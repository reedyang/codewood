"""Node.js / JavaScript script scanner (placeholder)."""

from __future__ import annotations

from typing import Set

from .base import ScriptTargetScanner
from .registry import _registry


class NodeScriptScanner(ScriptTargetScanner):
    """Scans Node.js / JavaScript (``.js``, ``.mjs``, ``.cjs``) scripts
    for file I/O targets.

    Currently a placeholder — static analysis of JS for file operations
    is not yet implemented.
    """

    @classmethod
    def extensions(cls) -> Set[str]:
        return {".js", ".mjs", ".cjs"}

    @classmethod
    def interpreter_names(cls) -> Set[str]:
        return {"node", "nodejs"}

    @classmethod
    def inline_flags(cls) -> Set[str]:
        return {"-e", "-p"}

    def _extract_raw_paths(self, source: str) -> Set[str]:
        return set()


_registry.register(NodeScriptScanner)
