"""PowerShell script scanner (placeholder)."""

from __future__ import annotations

from typing import Set

from .base import ScriptTargetScanner
from .registry import _registry


class PowerShellScriptScanner(ScriptTargetScanner):
    """Scans PowerShell (``.ps1``, ``.psm1``) scripts for file I/O targets.

    Currently a placeholder — static analysis of PowerShell scripts for
    file operations is not yet implemented.
    """

    @classmethod
    def extensions(cls) -> Set[str]:
        return {".ps1", ".psm1"}

    @classmethod
    def interpreter_names(cls) -> Set[str]:
        return {"powershell", "pwsh"}

    @classmethod
    def inline_flags(cls) -> Set[str]:
        return {"-command", "-c", "/c", "-encodedcommand", "-enc", "-e"}

    @classmethod
    def option_value_flags(cls) -> Set[str]:
        return {"-file", "-f"}

    def _extract_raw_paths(self, source: str) -> Set[str]:
        return set()


_registry.register(PowerShellScriptScanner)
