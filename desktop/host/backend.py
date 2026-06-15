"""Spawn and manage the Code Wood backend (serve mode) subprocess.

In a frozen build the GUI runs as ``codewoodw.exe`` and launches the
sibling ``codewood.exe serve``. During development it launches
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


class BackendProcess:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.token: Optional[str] = None

    def _resolve(self) -> Tuple[List[str], str]:
        """Return (command, cwd) for the backend depending on environment."""
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            name = "codewood.exe" if os.name == "nt" else "codewood"
            backend = exe_dir / name
            if not backend.exists():
                raise BackendError(
                    f"Backend executable not found next to the GUI: {backend}"
                )
            return [str(backend), "serve", "--port", "0"], str(exe_dir)

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
