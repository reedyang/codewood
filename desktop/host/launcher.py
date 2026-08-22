"""Thin windowed launcher for the Code Wood desktop GUI.

This tiny, console-free (``--noconsole`` / windowed) executable exists only
so that double-clicking it opens the GUI *without* flashing a console
window. It immediately starts the sibling ``codewood`` executable's ``app``
command — with no console window — and then exits.

All terminal-UI and GUI logic lives in ``codewood`` itself; this launcher
deliberately imports nothing beyond the standard library so its packaged
executable stays small (no pywebview / prompt_toolkit / tiktoken, etc.).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_GUI_DETACHED_ENV = "CODEWOOD_GUI_DETACHED"


def _backend_command() -> list[str]:
    """Return the command that runs ``codewood`` for this launch mode.

    Normal launches run the ``app`` GUI. A toast-activation launch (started by
    Windows when a task-completion notification is clicked) passes
    ``--toast-activate`` through so the child foregrounds the already-running
    window and exits — no GUI work, and no console window (the child is
    spawned with ``CREATE_NO_WINDOW`` in :func:`main`).
    """
    name = "codewood.exe" if os.name == "nt" else "codewood"
    extra = list(sys.argv[1:])
    toast_activate = bool(extra) and extra[0] == "--toast-activate"
    suffix = extra if toast_activate else ["app"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Support both packaging layouts: the launcher sitting next to
        # codewood (one-dir bundle, the normal case) or one directory above
        # it (codewood placed in a "codewood/" subfolder).
        for candidate in (exe_dir / name, exe_dir / "codewood" / name):
            if candidate.exists():
                return [str(candidate)] + suffix
        return [str(exe_dir / name)] + suffix
    # Development: run the repository entry point directly.
    repo_root = Path(__file__).resolve().parents[2]
    return [sys.executable, str(repo_root / "cli" / "main.py")] + suffix


def main() -> int:
    # Strip PyInstaller's private bootstrap variables so the (also frozen)
    # child resolves its own one-file bundle correctly, and ask it to run the
    # GUI inline (it is already window-free here, so no extra re-spawn needed).
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("_PYI") or k.startswith("_MEI"))
    }
    env[_GUI_DETACHED_ENV] = "1"
    if os.name == "nt":
        env.setdefault("PYTHONNET_RUNTIME", "netfx")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True

    cmd = _backend_command()

    # macOS: the .app's main process must stay the process that owns the GUI
    # window. Spawning a detached child makes macOS show the window in the Dock
    # with a generic "exec" icon, separated from the Code Wood app icon. Replacing
    # our own image keeps the same PID, so LaunchServices still associates this
    # process with the (still-running) .app bundle. Windows/Linux keep spawning
    # a detached child so the launcher can return control immediately.
    if sys.platform == "darwin":
        try:
            os.execvpe(cmd[0], cmd, env)
        except Exception:
            return 1

    try:
        subprocess.Popen(  # noqa: S603 - launching our trusted sibling executable
            cmd,
            env=env,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
