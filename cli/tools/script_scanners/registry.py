"""Scanner registry — maps file extensions / interpreter names to scanners."""

from __future__ import annotations

from typing import Dict, Optional, Set, Type

from .base import ScriptTargetScanner


class ScriptScannerRegistry:
    """Registry of :class:`ScriptTargetScanner` subclasses, keyed by both
    file extension and interpreter name for fast dispatch."""

    def __init__(self) -> None:
        self._by_extension: Dict[str, Type[ScriptTargetScanner]] = {}
        self._by_interpreter: Dict[str, Type[ScriptTargetScanner]] = {}

    def register(self, scanner_cls: Type[ScriptTargetScanner]) -> None:
        """Register *scanner_cls* under all its declared extensions and
        interpreter names."""
        for ext in scanner_cls.extensions():
            self._by_extension[ext] = scanner_cls
        for name in scanner_cls.interpreter_names():
            self._by_interpreter[name] = scanner_cls

    def find_by_extension(self, ext: str) -> Optional[Type[ScriptTargetScanner]]:
        return self._by_extension.get(ext.lower())

    def find_by_interpreter(self, name: str) -> Optional[Type[ScriptTargetScanner]]:
        return self._by_interpreter.get(name.lower())


_registry = ScriptScannerRegistry()


def get_registry() -> ScriptScannerRegistry:
    """Return the global :class:`ScriptScannerRegistry` singleton."""
    return _registry
