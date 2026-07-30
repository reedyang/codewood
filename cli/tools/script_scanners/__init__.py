"""Script target-path scanner package.

Provides a single entry point :func:`expand_command_file_paths` that
detects script-file execution in a shell command and delegates to the
appropriate language-specific :class:`ScriptTargetScanner`.

Adding support for a new language
----------------------------------
1. Create a new module (e.g. ``ruby.py``) in this package.
2. Subclass :class:`~base.ScriptTargetScanner`.
3. Import and register it at the bottom of the new module::

       from .registry import _registry
       _registry.register(MyScanner)

4. Import the module in this ``__init__.py`` to trigger registration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

# Import scanner modules to trigger auto-registration
from . import bash         # noqa: F401
from . import node         # noqa: F401
from . import powershell   # noqa: F401
from . import python       # noqa: F401

from .base import (
    ScriptExecution,
    ScriptTargetScanner,
    parse_script_execution,
)
from .registry import get_registry


def expand_command_file_paths(
    command: str,
    cwd: Path,
    cmd_paths: Set[str],
) -> Set[str]:
    """Unified entry point for expanding command file paths with script-scanned targets.

    Detects whether *command* executes a script file, and if so, uses the
    registered :class:`ScriptTargetScanner` for that language to discover
    additional file paths the script may create, modify, or delete.

    Args:
        command: The raw shell command string.
        cwd: The working directory the command runs from.
        cmd_paths: File paths already extracted from command tokens.

    Returns:
        The union of *cmd_paths* and any additional paths discovered by
        scanning the invoked script file.
    """
    registry = get_registry()
    exec_info = parse_script_execution(command, cwd, registry)
    if exec_info is None:
        return cmd_paths

    # Find the right scanner — prefer interpreter-based lookup, fall
    # back to file extension.
    scanner_cls = None
    if exec_info.interpreter:
        scanner_cls = registry.find_by_interpreter(exec_info.interpreter)
    if scanner_cls is None and exec_info.extension:
        scanner_cls = registry.find_by_extension(exec_info.extension)
    if scanner_cls is None:
        return cmd_paths

    scanner = scanner_cls()
    scanned = scanner.scan(exec_info.script_path, cwd)
    if scanned:
        return cmd_paths | scanned
    return cmd_paths
