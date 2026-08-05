"""Spawn and manage the Code Wood backend (serve mode) subprocess.

In a frozen build the GUI re-launches the same ``codewood.exe`` in
``serve`` mode. During development it launches
``python <repo>/cli/main.py serve`` instead. Either way the backend
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
    # Force the child's stdio to UTF-8 from the very first byte. ``main()``
    # also reconfigures the streams, but interpreter-level output (faulthandler,
    # warnings, an early import error) can fire before that runs; setting the
    # env var covers those too. Harmless for a frozen child that has no
    # separate Python interpreter env to honor it.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
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
        development it launches ``python cli/main.py serve``.
        """
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
            return [str(exe), "serve", "--port", "0"], str(exe.parent)

        repo_root = Path(__file__).resolve().parents[2]
        main_py = repo_root / "cli" / "main.py"
        if not main_py.exists():
            raise BackendError(f"Backend entry script not found: {main_py}")
        return [sys.executable, str(main_py), "serve", "--port", "0"], str(repo_root)

    # Keep the last few non-handshake output lines so a startup failure can
    # be reported back to the user with the backend's own diagnostics (e.g.
    # "model provider unsupported", a config parse error, a traceback) rather
    # than the opaque "exited before handshake" message.
    _MAX_CAPTURED_LINES = 40

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
            # The backend forces its stdout to UTF-8 (see ``_force_utf8_std_streams``
            # in main.py) so it can emit Unicode banners on any locale. Decode
            # with the matching codec here — otherwise on a non-UTF-8 system
            # locale (e.g. GBK on Chinese Windows) the parent would mis-decode
            # the handshake/diagnostics. ``errors="replace"`` keeps a stray byte
            # from ever raising while reading.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=_clean_frozen_env(),
        )

        assert self.proc.stdout is not None
        captured: List[str] = []

        def _remember(text_line: str) -> None:
            line_str = text_line.rstrip("\r\n")
            if not line_str:
                return
            captured.append(line_str)
            if len(captured) > self._MAX_CAPTURED_LINES:
                del captured[0 : len(captured) - self._MAX_CAPTURED_LINES]

        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    # Drain whatever remains so the user sees the real cause.
                    try:
                        for tail in self.proc.stdout:
                            _remember(tail)
                    except Exception:
                        pass
                    raise BackendError(
                        self._format_exit_error(
                            "Backend exited before sending a handshake.",
                            captured,
                        )
                    )
                continue
            stripped = line.strip()
            try:
                data = json.loads(stripped)
            except Exception:
                # Non-JSON startup lines (e.g. environment warnings, config
                # errors, tracebacks) are not the handshake — keep them for
                # diagnostics in case the process exits without handshaking.
                _remember(line)
                continue
            if isinstance(data, dict) and "port" in data and "token" in data:
                self.port = int(data["port"])
                self.token = str(data["token"])
                break

        if not self.token or not self.port:
            self.stop()
            raise BackendError(
                self._format_exit_error(
                    "Timed out waiting for the backend handshake.",
                    captured,
                )
            )

        threading.Thread(target=self._drain_stdout, daemon=True).start()
        return self.port, self.token

    @staticmethod
    def _format_exit_error(headline: str, captured: List[str]) -> str:
        """Combine a headline with the backend's captured output (if any).

        Strips ANSI color sequences so the error window stays readable, and
        bounds the appended detail to the last handful of lines.
        """
        if not captured:
            return headline
        import re

        ansi = re.compile(r"\x1b\[[0-9;]*m")
        tail = [ansi.sub("", ln) for ln in captured[-12:]]
        detail = "\n".join(ln for ln in tail if ln.strip())
        if not detail:
            return headline
        return f"{headline}\n\nBackend output:\n{detail}"

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

    def restart(self, timeout: float = 45.0) -> Tuple[int, str]:
        """Stop the current backend (if any) and start a fresh process.

        A fresh ``serve`` process binds a new ephemeral port and generates a
        new token, so callers must re-publish the returned ``(port, token)``
        to whatever depends on the old endpoint (e.g. the frontend).
        """
        self.stop()
        self.proc = None
        self.port = None
        self.token = None
        return self.start(timeout=timeout)

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
