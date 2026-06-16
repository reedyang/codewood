"""Spawn and manage the Code Wood backend (serve mode) subprocess.

In a frozen build the GUI re-launches the same ``codewood.exe`` in
``serve`` mode. During development it launches
``python <repo>/src/main.py serve`` instead. Either way the backend
prints a one-line JSON handshake (``{"port", "token"}``) on stdout that
this module reads to learn where to connect.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple


class BackendError(RuntimeError):
    pass


def _clean_frozen_env() -> dict:
    """Return a copy of the environment without PyInstaller's private vars.

    A frozen (one-file) parent exports bootstrap variables such as
    ``_PYI_ARCHIVE_FILE`` / ``_PYI_APPLICATION_HOME_DIR`` / ``_MEIPASS2``
    that point at *its own* extraction directory. If they leak into a child
    that is itself a frozen executable, the child resolves its bundle to the
    wrong place (breaking imports and the bundled .NET runtime). Strip them
    so the child bootstraps cleanly.
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("_PYI") or key.startswith("_MEI"):
            env.pop(key, None)
    return env


class BackendProcess:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.token: Optional[str] = None

    def _resolve(self) -> Tuple[List[str], str]:
        """Return (command, cwd) for the backend depending on environment.

        The GUI and the backend share a single executable. In a frozen
        build the GUI re-launches itself with the ``serve`` command; in
        development it launches ``python src/main.py serve``.
        """
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
            return [str(exe), "serve", "--port", "0"], str(exe.parent)

        repo_root = Path(__file__).resolve().parents[2]
        main_py = repo_root / "src" / "main.py"
        if not main_py.exists():
            raise BackendError(f"Backend entry script not found: {main_py}")
        return [sys.executable, str(main_py), "serve", "--port", "0"], str(repo_root)

    def start(self, timeout: float = 45.0) -> Tuple[int, str]:
        command, cwd = self._resolve()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
            env=_clean_frozen_env(),
        )

        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise BackendError("Backend exited before sending a handshake.")
                continue
            stripped = line.strip()
            try:
                data = json.loads(stripped)
            except Exception:
                # Non-JSON startup lines (e.g. environment warnings) are ignored.
                continue
            if isinstance(data, dict) and "port" in data and "token" in data:
                self.port = int(data["port"])
                self.token = str(data["token"])
                break

        if not self.token or not self.port:
            self.stop()
            raise BackendError("Timed out waiting for the backend handshake.")

        threading.Thread(target=self._drain_stdout, daemon=True).start()
        return self.port, self.token

    def _drain_stdout(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            return
        try:
            for _ in self.proc.stdout:
                pass
        except Exception:
            pass

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.token and self.port:
            try:
                req = urllib.request.Request(
                    self.base_url + "/shutdown",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
        for stopper in ("wait", "terminate", "kill"):
            try:
                if stopper == "wait":
                    self.proc.wait(timeout=6)
                    return
                getattr(self.proc, stopper)()
                self.proc.wait(timeout=4)
                return
            except Exception:
                continue
