"""Base classes and utilities for script-file target-path scanning."""

from __future__ import annotations

import os
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type


@dataclass
class ScriptExecution:
    """Parsed information about a script execution command."""

    script_path: Path
    """Resolved absolute path to the script file on disk."""
    interpreter: Optional[str] = None
    """Lowercase interpreter executable name (``python``, ``bash``, …)."""
    extension: str = ""
    """Lowercase file extension including the dot (``.py``, ``.sh``, …)."""

    def __post_init__(self) -> None:
        if not self.extension and self.script_path.suffix:
            self.extension = self.script_path.suffix.lower()


class ScriptTargetScanner(ABC):
    """Strategy for scanning a script file to discover file paths that the
    script may **create, modify, or delete** at runtime.

    Subclasses implement language-specific parsing in
    :meth:`_extract_raw_paths` and declare metadata via the classmethods
    below so the registry can auto-dispatch.
    """

    # ---- metadata (override in subclasses) ---------------------------------

    @classmethod
    @abstractmethod
    def extensions(cls) -> Set[str]:
        """File extensions this scanner handles (lowercase, dot-prefixed).

        Example: ``{'.py', '.py3'}``.
        """

    @classmethod
    @abstractmethod
    def interpreter_names(cls) -> Set[str]:
        """Lowercase interpreter executable names this scanner is associated with.

        Example: ``{'python', 'python3', 'py'}``.
        """

    @classmethod
    def inline_flags(cls) -> Set[str]:
        """Flags that signal *inline code* (not a script file).

        When the command contains one of these flags the parser will NOT
        look for a script file argument.

        Example: ``{'-c', '-m'}`` for Python.
        """
        return set()

    @classmethod
    def option_value_flags(cls) -> Set[str]:
        """Flags whose **next** token is the script-file path.

        Example: ``{'-File', '-F'}`` for PowerShell.
        """
        return set()

    # ---- public API --------------------------------------------------------

    def scan(self, script_path: Path, cwd: Path) -> Set[str]:
        """Read *script_path*, extract raw file-target paths, and resolve them
        as absolute paths under *cwd*.

        This is a **template method** — subclasses only need to implement
        :meth:`_extract_raw_paths`.
        """
        source = _read_text_file(script_path)
        if source is None:
            return set()
        raw = self._extract_raw_paths(source)
        return _resolve_raw_paths(raw, cwd)

    # ---- subclasses implement ----------------------------------------------

    @abstractmethod
    def _extract_raw_paths(self, source: str) -> Set[str]:
        """Parse *source* code and return a set of raw path strings.

        Paths may be relative or absolute; resolution is handled by the
        base class.  Only string-literal paths should be returned —
        dynamically-constructed paths (f-strings, variables, os.path.join)
        cannot be resolved statically.
        """


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _read_text_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _split_command(command: str) -> List[str]:
    """Tokenise a shell command string, respecting platform quoting rules."""
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _resolve_raw_paths(raw_paths: Set[str], cwd: Path) -> Set[str]:
    """Resolve raw path strings into absolute paths relative to *cwd*.

    Follows the same logic as :func:`_add_paths_from_inline_code` in
    ``shell.py``: existing paths and paths whose parent directory exists
    are included; directories are expanded to their contained files.
    """
    result: Set[str] = set()
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = cwd / p
        try:
            p = p.resolve()
        except (OSError, ValueError):
            continue
        if p.exists():
            if p.is_dir():
                try:
                    for f in p.rglob("*"):
                        if f.is_file():
                            result.add(str(f))
                except (OSError, PermissionError):
                    pass
            elif p.is_file():
                result.add(str(p))
        else:
            parent = p.parent
            if parent.exists() and parent.is_dir():
                result.add(str(p))
    return result


def _exe_base(token: str) -> str:
    """Normalise the first token of a command to its interpreter base name.

    ``python.exe``, ``python3``, ``/usr/bin/python`` all yield ``python``.
    """
    base = token.replace("\\", "/").split("/")[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------


def parse_script_execution(
    command: str,
    cwd: Path,
    registry: Any,  # ScriptScannerRegistry — forward reference
) -> Optional[ScriptExecution]:
    """Parse a shell *command* to determine whether it executes a script file.

    Returns :class:`ScriptExecution` with the script path, interpreter name,
    and extension, or ``None`` when the command does not invoke a script file.
    """
    stripped = command.strip()
    if not stripped:
        return None
    parts = _split_command(stripped)
    if not parts:
        return None

    first = parts[0]
    exe = _exe_base(first)

    # ---- Case 1: interpreter-based invocation (e.g. ``python script.py``) --
    scanner_cls = registry.find_by_interpreter(exe)
    if scanner_cls is not None:
        scanner = scanner_cls()
        inline = scanner_cls.inline_flags()
        value_flags = scanner_cls.option_value_flags()

        # Walk non-flag tokens looking for the script-file argument.
        i = 1
        while i < len(parts):
            t = parts[i].lower().lstrip('"').lstrip("'")
            if t == "--":
                i += 1
                break
            if t in inline:
                return None  # inline code — no script file
            if t in value_flags:
                if i + 1 >= len(parts):
                    return None
                script_path = _resolve_script_token(parts[i + 1], cwd)
                if script_path is not None and script_path.is_file():
                    return ScriptExecution(
                        script_path=script_path,
                        interpreter=exe,
                    )
                return None
            if t.startswith("-") or (os.name == "nt" and t.startswith("/")):
                i += 1
                continue
            break
        if i >= len(parts):
            return None
        script_path = _resolve_script_token(parts[i], cwd)
        if script_path is not None and script_path.is_file():
            return ScriptExecution(
                script_path=script_path,
                interpreter=exe,
            )
        return None

    # ---- Case 2: direct script execution (e.g. ``./script.py``) ------------
    ext = Path(first).suffix.lower()
    scanner_cls = registry.find_by_extension(ext)
    if scanner_cls is not None:
        script_path = _resolve_script_token(first, cwd)
        if script_path is not None and script_path.is_file():
            return ScriptExecution(
                script_path=script_path,
                interpreter=None,
                extension=ext,
            )
    return None


def _resolve_script_token(token: str, cwd: Path) -> Optional[Path]:
    """Resolve a script-file token to an absolute path, or ``None``."""
    tok = token.strip('"').strip("'")
    if not tok:
        return None
    p = Path(tok)
    if not p.is_absolute():
        p = cwd / p
    try:
        p = p.resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None
