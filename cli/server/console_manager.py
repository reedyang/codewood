"""Backend PTY session management for the GUI embedded console.

The desktop GUI shows a tabbed, interactive terminal. Each tab is backed by a
long-lived PTY process owned here (the frontend xterm.js view and the model
both attach to the same session). On Windows we use ``pywinpty`` (ConPTY);
on POSIX we fall back to the stdlib ``pty`` module so the manager is testable
off Windows. When no PTY backend is available the manager reports unsupported
and the console feature degrades cleanly.

Threat context: the console runs an interactive shell the user already trusts
(same privileges as the app). Inbound data is written to the PTY stdin as-is by
design. We still validate the requested shell ``kind`` against a strict
allowlist and never interpolate caller-supplied strings into the launcher.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

# Shell kinds the GUI may request. Mapped to a concrete launcher per-platform.
# ``shell`` is the generic default shell used on Linux/macOS (the user's
# ``$SHELL``); the Windows-specific kinds are offered only on Windows.
SHELL_KINDS = ("powershell", "cmd", "gitbash", "shell")

# Human label prefix used to auto-name tabs (frontend may localize separately).
_KIND_LABELS = {
    "powershell": "PowerShell",
    "cmd": "Command Prompt",
    "gitbash": "Git Bash",
    "shell": "Terminal",
}


def _is_windows() -> bool:
    return os.name == "nt"


_SYS32 = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32"
).lower()


def _find_git_bash() -> Optional[str]:
    """Return the absolute path to Git-for-Windows bash.exe, or None."""
    if os.name != "nt":
        return None

    # 1. Well-known install locations -------------------------------------------
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    # ProgramW6432 bypasses WOW64 redirection on 32-bit Python.
    pw6432 = os.environ.get("ProgramW6432", "")
    if pw6432:
        candidates.append(os.path.join(pw6432, "Git", "bin", "bash.exe"))
        candidates.append(os.path.join(pw6432, "Git", "usr", "bin", "bash.exe"))
    # Per-user install (via the Git for Windows standalone installer).
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        candidates.append(
            os.path.join(localappdata, "Programs", "Git", "bin", "bash.exe")
        )
    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    # 2. ``where.exe bash`` – full PATH search, exclude WSL ---------------------
    try:
        out = subprocess.check_output(
            ["where", "bash"], text=True, timeout=5
        )
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                real = os.path.realpath(line)
            except (TypeError, OSError):
                real = line
            if _SYS32 not in real.lower():
                return line
    except Exception:
        pass

    # 3. Derive from ``git.exe`` on PATH ----------------------------------------
    git = shutil.which("git")
    if git:
        try:
            git_real = os.path.realpath(git)
        except (TypeError, OSError):
            git_real = git
        git_dir = os.path.dirname(git_real)
        root = os.path.dirname(git_dir)
        candidate = os.path.join(root, "usr", "bin", "bash.exe")
        if os.path.isfile(candidate):
            return candidate

    # 4. ``shutil.which("bash")`` – accept any that isn't WSL -------------------
    exe = shutil.which("bash")
    if exe:
        try:
            real = os.path.realpath(exe)
        except (TypeError, OSError):
            real = exe
        if _SYS32 not in real.lower():
            return exe

    return None


def _resolve_windows_launcher(kind: str) -> Optional[List[str]]:
    """Return the argv for a Windows shell kind, or None when not found."""
    if kind == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        return [exe] if exe else None
    if kind == "cmd":
        exe = shutil.which("cmd") or os.environ.get("ComSpec")
        return [exe] if exe else None
    if kind == "gitbash":
        exe = _find_git_bash()
        return [exe, "--login", "-i"] if exe else None
    return None


def _resolve_posix_launcher(kind: str) -> Optional[List[str]]:
    """POSIX launcher (Linux/macOS and tests)."""
    if kind in ("gitbash",):
        exe = shutil.which("bash")
        return [exe, "-i"] if exe else None
    if kind == "shell":
        # The user's login shell, falling back to bash/sh.
        exe = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
        return [exe, "-i"] if exe else None
    # powershell / cmd have no real POSIX equivalent; map to the user's shell.
    exe = shutil.which("pwsh") if kind == "powershell" else None
    if not exe:
        exe = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
    return [exe] if exe else None


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


class ConsoleSession:
    """A single PTY-backed terminal session.

    Owns the child process, a reader thread that drains PTY output into a
    bounded line buffer, and a registry of output subscribers (the WebSocket
    writers). Thread-safe for the operations the server calls concurrently.
    """

    def __init__(
        self,
        session_id: str,
        kind: str,
        title: str,
        cwd: str,
        buffer_lines: int = 1000,
    ) -> None:
        self.id = session_id
        self.kind = kind
        self.title = title
        self.cwd = cwd or os.getcwd()
        self.cols = 80
        self.rows = 24
        self._buffer_lines = max(1, int(buffer_lines or 1000))
        # Completed lines plus the in-progress trailing fragment. ``_total``
        # counts every completed line ever produced (not just retained), so the
        # model can reason about absolute positions even after truncation.
        self._lines: Deque[str] = deque(maxlen=self._buffer_lines)
        self._pending = ""
        self._total = 0
        # Bounded raw-byte tail of recent PTY output. A newly attached xterm
        # view replays this so the shell banner/prompt printed before it
        # connected is visible (otherwise the terminal shows only a cursor).
        self._raw = bytearray()
        self._raw_cap = 256 * 1024
        self._lock = threading.RLock()
        self._subscribers: List[Callable[[bytes], None]] = []
        self._closed = False
        self._proc: Any = None
        self._backend = ""  # "winpty" | "pty" | ""
        self._reader: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    def start(self) -> bool:
        """Spawn the PTY. Returns True on success, False when unsupported."""
        launcher = (
            _resolve_windows_launcher(self.kind)
            if _is_windows()
            else _resolve_posix_launcher(self.kind)
        )
        if not launcher:
            return False
        if _is_windows():
            return self._start_winpty(launcher)
        return self._start_posix(launcher)

    def _start_winpty(self, launcher: List[str]) -> bool:
        try:
            from winpty import PtyProcess  # type: ignore
        except Exception:
            return False
        try:
            # Pass the arg list directly — PtyProcess.spawn accepts both str
            # and List[str].  Passing a list avoids the internal shlex.split()
            # which can mishandle quoted paths containing spaces.
            self._proc = PtyProcess.spawn(
                launcher, cwd=self.cwd, dimensions=(self.rows, self.cols)
            )
            self._backend = "winpty"
        except Exception:
            return False
        self._spawn_reader(self._read_winpty)
        return True

    def _start_posix(self, launcher: List[str]) -> bool:
        try:
            import pty  # noqa: WPS433 (POSIX-only stdlib)
        except Exception:
            return False
        try:
            pid, fd = pty.fork()
        except Exception:
            return False
        if pid == 0:
            try:
                os.chdir(self.cwd)
            except Exception:
                pass
            try:
                os.execvp(launcher[0], launcher)
            except Exception:
                os._exit(127)
        self._proc = pid
        self._backend = "pty"
        self._posix_fd = fd
        self._spawn_reader(self._read_posix)
        return True

    def _spawn_reader(self, target: Callable[[], None]) -> None:
        self._reader = threading.Thread(
            target=target, name=f"console-reader-{self.id}", daemon=True
        )
        self._reader.start()

    def _read_winpty(self) -> None:
        import time

        proc = self._proc
        while not self._closed:
            try:
                data = proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                if not self.is_alive():
                    break
                # ``read`` can return empty without blocking; back off briefly
                # so we don't spin the CPU while the shell is idle.
                time.sleep(0.01)
                continue
            chunk = data.encode("utf-8", "replace") if isinstance(data, str) else data
            self._ingest(chunk)
        self._on_reader_exit()

    def _read_posix(self) -> None:
        fd = getattr(self, "_posix_fd", -1)
        while not self._closed:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            self._ingest(data)
        self._on_reader_exit()

    def _on_reader_exit(self) -> None:
        # Notify subscribers the stream ended so the UI can show an exit hint.
        self._broadcast(b"")

    # ------------------------------------------------------------------
    # Output ingestion + buffering
    def _ingest(self, chunk: bytes) -> None:
        self._broadcast(chunk)
        try:
            text = chunk.decode("utf-8", "replace")
        except Exception:
            return
        with self._lock:
            self._raw.extend(chunk)
            if len(self._raw) > self._raw_cap:
                # Keep the most recent window; drop the oldest bytes.
                del self._raw[: len(self._raw) - self._raw_cap]
            self._pending += text
            # Split on newlines; keep the trailing partial line as pending.
            while True:
                idx = self._pending.find("\n")
                if idx < 0:
                    break
                line = self._pending[:idx].rstrip("\r")
                self._pending = self._pending[idx + 1 :]
                self._lines.append(line)
                self._total += 1

    def _broadcast(self, chunk: bytes) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(chunk)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Subscribers (WebSocket writers)
    def add_subscriber(self, fn: Callable[[bytes], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)
            replay = bytes(self._raw)
        # Replay outside the lock so a slow writer can't stall ingestion.
        if replay:
            try:
                fn(replay)
            except Exception:
                pass

    def add_live_subscriber(self, fn: Callable[[bytes], None]) -> None:
        """Subscribe to live output only (no retained-buffer replay).

        Used by the shared SSE fan-out, where replaying a session's scrollback
        would leak it to every connected client. Per-terminal replay is served
        on demand via ``snapshot_raw``."""
        with self._lock:
            self._subscribers.append(fn)

    def snapshot_raw(self) -> bytes:
        """Return a copy of the retained raw-byte output tail."""
        with self._lock:
            return bytes(self._raw)

    def remove_subscriber(self, fn: Callable[[bytes], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Input / control
    def write(self, data: str) -> None:
        if self._closed or not self._proc:
            return
        try:
            if self._backend == "winpty":
                self._proc.write(data)
            elif self._backend == "pty":
                os.write(self._posix_fd, data.encode("utf-8", "replace"))
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            cols = max(1, min(2000, int(cols)))
            rows = max(1, min(2000, int(rows)))
        except (TypeError, ValueError):
            return
        self.cols, self.rows = cols, rows
        if self._closed or not self._proc:
            return
        try:
            if self._backend == "winpty":
                self._proc.setwinsize(rows, cols)
            elif self._backend == "pty":
                import fcntl  # noqa: WPS433
                import struct
                import termios

                fcntl.ioctl(
                    self._posix_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
        except Exception:
            pass

    def is_alive(self) -> bool:
        proc = self._proc
        if not proc:
            return False
        try:
            if self._backend == "winpty":
                return bool(proc.isalive())
            if self._backend == "pty":
                pid, _ = os.waitpid(proc, os.WNOHANG)
                return pid == 0
        except Exception:
            return False
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        try:
            if self._backend == "winpty" and proc:
                proc.terminate(force=True)
            elif self._backend == "pty":
                try:
                    os.close(self._posix_fd)
                except OSError:
                    pass
                try:
                    os.kill(proc, 9)
                except OSError:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Reads for the model
    def read_lines(self, start: int, count: int) -> Dict[str, Any]:
        """Return retained lines in ``[start, start + count)`` (absolute index).

        ``totalLines`` is the absolute number of completed lines produced so
        far. When the requested range predates the retained window (older lines
        were truncated), only the still-retained subset is returned and
        ``truncated`` is set."""
        with self._lock:
            total = self._total
            retained = list(self._lines)
            pending = self._pending
        retained_count = len(retained)
        first_retained = total - retained_count
        try:
            start = max(0, int(start))
            count = max(0, min(2000, int(count)))
        except (TypeError, ValueError):
            start, count = 0, 0
        end = start + count
        # Clip the requested absolute range to what we still hold.
        clip_start = max(start, first_retained)
        clip_end = min(end, total)
        out: List[str] = []
        if clip_end > clip_start:
            lo = clip_start - first_retained
            hi = clip_end - first_retained
            out = retained[lo:hi]
        return {
            "lines": out,
            "start": clip_start,
            "totalLines": total,
            "retainedFrom": first_retained,
            "truncated": start < first_retained,
            "pending": pending,
        }

    def info(self) -> Dict[str, Any]:
        with self._lock:
            total = self._total
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
            "totalLines": total,
            "alive": self.is_alive(),
            "backend": self._backend,
        }


class ConsoleManager:
    """Owns all console sessions and tracks the active one."""

    def __init__(self, cwd_getter: Optional[Callable[[], str]] = None) -> None:
        self._cwd_getter = cwd_getter
        self._sessions: Dict[str, ConsoleSession] = {}
        self._order: List[str] = []
        self._active_id = ""
        self._counts: Dict[str, int] = {k: 0 for k in SHELL_KINDS}
        self._lock = threading.RLock()
        self._buffer_lines = 1000
        self._seq = 0

    def set_buffer_lines(self, n: int) -> None:
        try:
            self._buffer_lines = max(100, min(100000, int(n)))
        except (TypeError, ValueError):
            pass

    def _cwd(self) -> str:
        if callable(self._cwd_getter):
            try:
                return str(self._cwd_getter() or os.getcwd())
            except Exception:
                return os.getcwd()
        return os.getcwd()

    def open(self, kind: str) -> Dict[str, Any]:
        k = str(kind or "").strip().lower()
        if k not in SHELL_KINDS:
            return {"success": False, "error": f"unknown shell kind: {kind}"}
        with self._lock:
            self._counts[k] = self._counts.get(k, 0) + 1
            ordinal = self._counts[k]
            self._seq += 1
            sid = f"con-{self._seq:x}"
            title = f"{kind_label(k)} {ordinal}"
            session = ConsoleSession(
                sid, k, title, self._cwd(), buffer_lines=self._buffer_lines
            )
        if not session.start():
            with self._lock:
                # Roll back the counter so the next attempt keeps numbering sane.
                self._counts[k] = max(0, self._counts.get(k, 1) - 1)
            return {
                "success": False,
                "error": f"could not start {kind_label(k)} (PTY backend unavailable)",
            }
        with self._lock:
            self._sessions[sid] = session
            self._order.append(sid)
            self._active_id = sid
        return {"success": True, **session.info()}

    def close(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            try:
                self._order.remove(session_id)
            except ValueError:
                pass
            if self._active_id == session_id:
                self._active_id = self._order[-1] if self._order else ""
        session.close()
        return True

    def get(self, session_id: str) -> Optional[ConsoleSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def set_active(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                self._active_id = session_id
                return True
        return False

    def active(self) -> Optional[ConsoleSession]:
        with self._lock:
            return self._sessions.get(self._active_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            order = list(self._order)
            active = self._active_id
            sessions = dict(self._sessions)
        out: List[Dict[str, Any]] = []
        for sid in order:
            session = sessions.get(sid)
            if session is None:
                continue
            info = session.info()
            info["active"] = sid == active
            out.append(info)
        return out

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._order.clear()
            self._active_id = ""
        for session in sessions:
            session.close()
