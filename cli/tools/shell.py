"""Tool: shell.

This module hosts the full shell execution pipeline (formerly
cli/actions/command_actions.py) plus the ShellTool class.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..actions.command_execution_buffer import CommandExecutionBuffer
from ..config.app_info import get_app_config_dirname, get_app_runtime_attr_name
from ..core.logging.app_logging import get_logger
from .script_scanners import expand_command_file_paths

_log = get_logger("codewood.shell_diff")

# ---------------------------------------------------------------------------
# Read-only command whitelist — commands that are known to never create,
# modify, or delete files.  For these we skip the git-stash / workspace-
# snapshot overhead entirely.
# ---------------------------------------------------------------------------
# Each pattern is tested case-insensitively against the full (normalized)
# command string.  The command must also pass a redirect check: ">", ">>",
# and pipe "|" disqualify it because the right-hand side could write.
_READ_ONLY_COMMAND_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # --- navigation / system info -----------------------------------
        r"^(cd|chdir|pushd|popd)(\s|$)",
        r"^(pwd|whoami|hostname|uname|date|time|ver|set|env)(\s|$)",
        r"^timeout(\s+/t\s+\d+|\s+\d+)(\s+(/nobreak|-n))?(\s*$|$)",
        r"^sleep(\s+\d+)(\s*$|$)",
        # --- directory listing ------------------------------------------
        r"^(dir|ls|ll|la|tree)(\.exe)?(\s|/[^ ]*)*$",
        r"^(gci|get-childitem)(\.exe)?(\s|$)",
        # --- PowerShell get-location ------------------------------------
        r"^(gl|pwd|get-location)(\.exe)?(\s|$)",
        # --- file reading -----------------------------------------------
        r"^(type|cat|head|tail|more|less)(\.exe)?\s",
        r"^(gc|get-content)(\.exe)?\s",
        # --- search -----------------------------------------------------
        r"^(find|findstr|grep|rg|ag|ack)(\.exe)?(\s|$)",
        r"^(sls|select-string)(\.exe)?(\s|$)",
        # --- text processing / comparison --------------------------------
        r"^(wc|sort|uniq|cut|tr|diff|fc|comp|comm)(\s|$)",
        r"^diff\s",
        r"^(echo|printf)(\s|$)",  # only safe without redirects
        # --- path / command lookup --------------------------------------
        r"^(which|where|whereis)(\s|$)",
        r"^type\s+\S+\s*$",  # cmd.exe "type" as "which" — single arg
        # --- help -------------------------------------------------------
        r"^(help|man|info)(\s|$)",
        r"^--?(help|h|\?)(\s|$)",
        # --- network diagnostics / system info --------------------------
        r"^(ping|pathping|tracert|traceroute|netstat|nslookup|dig)(\.exe)?(\s|$)",
        r"^(ipconfig|ifconfig|arp|nbtstat|getmac|systeminfo|tasklist)(\.exe)?(\s|$)",
        r"^route\s+print(\s|$)",
        # --- version queries --------------------------------------------
        r"^(python|python3|node|npm|go|rustc|java|javac|gcc|g\+\+|clang)\s+--version(\s|$)",
        r"^(pip|pip3|gem)\s+(--version|-V)(\s|$)",
        # --- frontend test / type-check ----------------------------------
        # These can write caches, coverage, or emit output, but their file
        # churn is intentionally not tracked (no snapshot / diff overhead).
        r"^npx(\s+--[^\s]+)*\s+vitest\s+run(\s|$)",
        r"^npx(\s+--[^\s]+)*\s+tsc(\s|$)",
        # --- python test runners -----------------------------------------
        r"^(python|python3|py)(\.exe)?\s+-m\s+(pytest|unittest)(\s|$)",
        r"^pytest(\.exe)?(\s|$)",
    ]
]

# Commands that look like they might modify files but are safe when used
# standalone (no arguments).
_READ_ONLY_COMMAND_EXACT: Set[str] = {c.lower() for c in [
    "pwd", "whoami", "hostname", "uname", "date", "time", "ver", "dir",
]}

# ---------------------------------------------------------------------------
# Sandbox failure analysis — heuristics that tell the model whether a failed
# command was (very likely) blocked by the sandbox itself: denied writes
# outside the workspace, network disabled, sandbox spawn failures, etc.
# These only apply while a sandbox plan is active, so false positives are
# bounded to sandboxed runs.
# ---------------------------------------------------------------------------
_SANDBOX_ACCESS_DENIED_RE = re.compile(
    r"(?i)(access is denied|access denied|permission denied|"
    r"operation not permitted|is not permitted|eacces|"
    r"errno\s*13|errno\s*10013)"
)

# Network-failure output patterns used only when the sandbox has network
# access disabled *and* the command is a known network command.
_SANDBOX_NETWORK_RE = re.compile(
    r"(?i)(network is unreachable|network unreachable|no route to host|"
    r"connection (?:timed out|refused|reset)|errno\s*1006[01]|"
    r"errno\s*10060|errno\s*10061|unable to (?:resolve|connect)|"
    r"could not resolve host|name or service not known|"
    r"temporary failure in name resolution|getaddrinfo failed|timed out)"
)

_SANDBOX_NETWORK_COMMAND_RE = re.compile(
    r"(?i)\b(git\s+(?:fetch|clone|pull|push|ls-remote)|"
    r"curl|wget|ping|tracert|nslookup|dig|ssh|scp|rsync|"
    r"pip\s+install|pip3\s+install|python\s+-m\s+(?:pip|uv)\s+install|"
    r"npm\s+(?:install|ci|i\b)|pnpm\s+(?:install|i\b)|"
    r"yarn\s+(?:add|install)|nuget\s+install|"
    r"uv\s+(?:add|sync|pip\s+install)|go\s+(?:get|mod\s+download)|"
    r"cargo\s+(?:add|update))\b"
)


_CURL_WRITE_FLAGS: Set[str] = {
    # Flags that make curl write response body to a local file.
    "-o", "--output", "-O", "--remote-name",
    # These require a filename argument; skip the entire curl cmd
    # when any of them appear.
}


# git subcommands that are strictly read-only: they never create, modify, or
# delete files in the working tree, refs, or object database.  Commands
# matching this set (after global options such as ``-C <path>`` are skipped)
# are safe to exempt from file-change monitoring and the before-content
# snapshot.
_GIT_STRICTLY_READONLY_SUBCOMMANDS: Set[str] = {
    "status", "log", "diff", "show", "rev-parse", "describe",
    "ls-files", "ls-tree", "blame", "shortlog", "grep",
    "whatchanged", "rev-list", "merge-base", "for-each-ref",
    "count-objects", "var", "version", "help", "fsck",
    "cherry", "name-rev", "check-attr", "check-ignore",
    "check-ref-format", "diff-tree", "diff-index", "diff-files",
    "cat-file", "config",
}

# git subcommands that are *usually* read-only but also accept flags or
# sub-subcommands that write.  Mapping: subcommand -> set of dangerous flags /
# sub-subcommands; when none of them appears the invocation is read-only.
# ``git stash`` with no arguments is ``git stash push`` (writes!), so the
# no-arguments form of ``stash`` is treated as dangerous below.
_GIT_CONDITIONALLY_READONLY_SUBCOMMANDS: Dict[str, Set[str]] = {
    "branch": {
        "-d", "-D", "-m", "-M", "-c", "-C", "-u", "--delete",
        "--move", "--copy", "--set-upstream-to", "--unset-upstream",
        "--edit-description",
    },
    "tag": {
        "-d", "-a", "-s", "-u", "-f", "-m", "-F", "-e",
        "--delete", "--annotate", "--sign", "--force", "--message",
        "--file", "--edit",
    },
    "remote": {
        "add", "rename", "remove", "set-url", "set-head", "prune",
        "update",
    },
    "stash": {
        "push", "pop", "apply", "drop", "clear", "create", "store",
        "branch", "save",
    },
    "submodule": {
        "add", "update", "init", "deinit", "set-url", "set-branch",
        "absorbgitdirs", "sync", "foreach", "summary",
    },
    "worktree": {
        "add", "remove", "move", "prune", "lock", "unlock",
    },
    "notes": {
        "add", "copy", "append", "edit", "remove", "prune", "set-head",
        "merge",
    },
    "reflog": {"expire", "delete"},
}


def _is_read_only_git_command(command: str) -> Optional[bool]:
    """Classify a git invocation as read-only or not.

    Returns ``True`` when the git subcommand (with global options such as
    ``-C <path>`` / ``--no-pager`` / ``-c key=val`` skipped) is known to never
    touch the working tree, ``False`` when it may write files/refs/objects,
    and ``None`` when *command* is not a git invocation at all (the caller
    falls back to the generic read-only whitelist).

    This deliberately replaces the old prefix regexes (``^git status`` etc.)
    which neither understood ``git -C <repo> status`` nor noticed that
    ``git branch -d`` / ``git tag -a`` / ``git remote add`` write refs.
    """
    s = str(command or "").strip()
    if not s:
        return None
    parts = _split_shell_like(s)
    if not parts:
        return None
    if _token_exe_base(_strip_wrapping_quotes(parts[0])) != "git":
        return None
    sub_idx = _git_subcommand_index(parts)
    if sub_idx is None:
        # bare "git" / "git --version" / "git --help" prints info; read-only.
        return True
    sub = _strip_wrapping_quotes(parts[sub_idx]).lower()
    if sub in ("--version", "--help", "-h", "version", "help"):
        return True
    if sub in _GIT_STRICTLY_READONLY_SUBCOMMANDS:
        return True
    danger = _GIT_CONDITIONALLY_READONLY_SUBCOMMANDS.get(sub)
    if danger is None:
        return False
    rest = parts[sub_idx + 1:]
    if not rest:
        # No arguments: ``git stash`` means ``git stash push`` (writes!);
        # the other conditional subcommands default to read-only listings.
        return sub != "stash"
    for tok in rest:
        if _strip_wrapping_quotes(tok).lower() in danger:
            return False
    return True


def _is_read_only_command(command: str) -> bool:
    """Return True when *command* is a known read-only operation that
    cannot create, modify, or delete workspace files.

    Two conditions must both be true:
    1. No redirect operators (``>``, ``>>``) or pipes (``|``) — a
       redirect or pipeline right-hand side could write to disk.
    2. The command matches at least one pattern in the read-only whitelist
       OR the first token is an exact match for a known-safe builtin.

    ``curl`` receives extra scrutiny: commands that include ``-o`` / ``-O``
    / ``--output`` / ``--remote-name`` are NOT considered read-only because
    they explicitly write to disk.
    """
    stripped = command.strip()
    if not stripped:
        return True  # empty command produces no file changes
    if any(op in stripped for op in (">", ">>", "|")):
        return False
    git_ro = _is_read_only_git_command(stripped)
    if git_ro is not None:
        return git_ro
    for pat in _READ_ONLY_COMMAND_PATTERNS:
        if pat.search(stripped):
            return True
    # The command may have been rewritten by enforce_workspace_rg_for_shell_command
    # (e.g. "rg pattern" → "D:\path\rg.exe pattern").  Extract the basename of
    # the first token and try matching against that as well.
    first_token = stripped.split(None, 1)[0] if stripped else ""
    exe_base = Path(first_token).name.lower()
    if exe_base and exe_base != first_token.lower():
        # Re-run patterns against the shorter basename form
        for pat in _READ_ONLY_COMMAND_PATTERNS:
            if pat.search(exe_base):
                return True
    if first_token in _READ_ONLY_COMMAND_EXACT:
        return True
    # ---- curl special-case: allow GET/HEAD/verbatim requests that don't
    #      write to local files via -o / -O / --output / --remote-name ----
    if exe_base in ("curl", "curl.exe"):
        tokens = shlex_split_safe(stripped)
        for tok in tokens:
            if tok in _CURL_WRITE_FLAGS:
                return False
        return True
    return False


def shlex_split_safe(text: str) -> List[str]:
    """Split *text* with shlex, falling back to str.split on error."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()

from ..core.console_utils import (
    GUI_CMD_OUTPUT_END,
    GUI_DIFF_BEGIN,
    GUI_DIFF_END,
    _SpinnerTicker,
    _WorkingStatusTicker,
    _ansi_gray,
    _safe_console_write,
)
from ..core.console_title import restore_app_console_title
from ..core.localization import translate


def _t(agent: Any, key: str, fallback: Optional[str] = None, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), fallback=fallback, **kwargs)

SHELL_OUTPUT_DISPLAY_TAIL_LINES = 30
SHELL_OUTPUT_DISPLAY_RESERVED_LINES = 3
SHELL_WORKING_STATUS_MARQUEE_FPS = 10.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\a\x1b]*(?:\a|\x1b\\)")
# Strips ConPTY-injected CSI sequences (window ops, DA, private modes)
# while preserving SGR color/style codes (which end with 'm'), cursor
# movement (A/B/C/D → \b/space), cursor position (H → \b via vcol),
# horizontal-absolute (G → \r), and erase-line (K → \r).
_PTY_CSI_STRIP_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@E-FI-JL-lno-~]")
_CURSOR_LEFT_RE = re.compile(r"\x1b\[([0-9]*)D")
_CURSOR_RIGHT_RE = re.compile(r"\x1b\[([0-9]*)C")
_CURSOR_UP_RE = re.compile(r"\x1b\[([0-9]*)A")
_CURSOR_DOWN_RE = re.compile(r"\x1b\[([0-9]*)B")
_CUP_RE = re.compile(r"\x1b\[(\d+);(\d+)H")
_CHA_RE = re.compile(r"\x1b\[(\d*)G")
_EL_RE = re.compile(r"\x1b\[\d*K")
_STREAM_ATTR_TERMINAL_COLUMNS = get_app_runtime_attr_name("terminal_columns")
_STREAM_ATTR_OUTPUT_INDENT_WIDTH = get_app_runtime_attr_name("output_indent_width")

# How long to wait for the pipe-reader thread to fully drain process output
# before closing the command-output block. The reader terminates on EOF once
# the process tree exits, so this normally returns in milliseconds; the cap
# only bounds the wait when a grandchild keeps an inherited pipe handle open.
_SHELL_DRAIN_TIMEOUT = 15.0


# Abort notices appended to a shell round that was interrupted mid-flight. A
# plain cancel (user Stop / ESC) reports ``command aborted by user`` (kept as a
# stable marker several callers match). A *pause* — the GUI "send immediately"
# queue-jump that interrupts the running task so a new message can be processed
# first — reports that the user interrupted the call to add more information.
SHELL_CANCEL_ABORT_NOTICE = "Command aborted by user\n"
SHELL_PAUSE_ABORT_NOTICE = (
    "User interrupted this call and is preparing to supplement more information\n"
)


# Non-interactive shell execution attaches no stdin, so an interactive command
# (REPL, wizard, pager, editor, ...) blocks forever waiting for keys.  If the
# process stays silent for ``_SHELL_INTERACTIVE_IDLE_TIMEOUT`` seconds it is
# treated as blocked on interactive input and auto-terminated so the shell tool
# returns instead of spinning.  ``_SHELL_MAX_TOTAL_TIMEOUT`` is an absolute
# safety cap so a hung process can never keep a turn alive indefinitely.
_SHELL_INTERACTIVE_IDLE_TIMEOUT = 30.0
_SHELL_MAX_TOTAL_TIMEOUT = 900.0


def _should_flush_pending_cha_at_eof(ch: str) -> bool:
    """Whether a buffered single-char ConPTY frame should survive EOF.

    ``_pending_cha`` exists only to delay a lone visible character long enough
    to see whether a following CHA/EL clear sequence erases it. Table-like
    commands on Windows can leave a stray trailing backslash frame just before
    the clear; if the process exits immediately afterward, surfacing that
    residue creates a bogus standalone ``\\`` in chat history.

    Preserve ordinary characters at EOF so legitimate one-character output
    (for example ``print("x", end="")``) is not lost.
    """
    return str(ch or "") not in {"\\", "/"}

def _collapse_cr_output(text: str) -> str:
    """Collapse \\r-based line overwrites and \\b-based backspaces
    (spinners, progress bars, timeout countdowns) in captured output.

    Each line overwritten by consecutive \\r is reduced to just the last
    segment. Consecutive \\b overwrites are collapsed by processing
    backspace character-by-character.
    CRLF (\\r\\n) is preserved as LF.
    """
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    out = []
    last_idx = len(lines) - 1
    for idx, line in enumerate(lines):
        if "\r" in line:
            parts = line.split("\r")
            non_empty = [p for p in parts if p]
            is_completed = idx < last_idx
            if is_completed and len(non_empty) >= 3:
                # Spinner / progress bar with 3+ frames on a completed
                # line whose last frame was never finalized.  Clear it.
                # Simple \r overwrites (2 frames) are kept — they're
                # legitimate output, not indeterminate animation.
                out.append("")
                continue
            if is_completed and len(non_empty) == 1 and parts[0] == "":
                # Single non-empty frame after leading \r: spinner that
                # wasn't finalized.  Clear.
                out.append("")
                continue
            line = line.rsplit("\r", 1)[-1]
        if "\b" in line:
            line = _handle_backspace_collapse(line)
        out.append(line)
    return "\n".join(out)


def _handle_backspace_collapse(text: str) -> str:
    """Process \\b (backspace) overwrite semantics on a single line."""
    if "\b" not in text:
        return text
    cur = []
    col = 0
    for ch in text:
        if ch == "\b":
            if col > 0:
                col -= 1
        else:
            if col < len(cur):
                cur[col] = ch
            else:
                cur.append(ch)
            col += 1
    return "".join(cur)


# On Windows, try to use winpty (ConPTY) so child processes like
# timeout.exe see a real console handle instead of a redirected pipe.
# Tests can set this to None to disable.
import subprocess as _subprocess_mod
_ORIG_SUBPROCESS_POPEN = _subprocess_mod.Popen
_WINPTY_PTYPROCESS = None
if sys.platform == "win32":
    try:
        from winpty import PtyProcess as _WINPTY_PTYPROCESS
    except ImportError:
        pass

if _WINPTY_PTYPROCESS is not None:

    class _WinPtyReader:
        """Wrap winpty read() as a byte-stream pipe for _stream_and_capture."""
        def __init__(self, pty_proc, activity_tracker=None):
            self._pty = pty_proc
            self._activity_tracker = activity_tracker
            self._buf = ""
            self._virtual_col = 0
            self._pending_cha = None
        def read(self, n=1024):
            try:
                while True:
                    if self._buf:
                        data = self._buf
                        self._buf = ""
                    else:
                        data = self._pty.read(4096)
                        if not data:
                            # Process closed: flush any pending buffered
                            # character before signalling EOF.
                            return self._flush_pending()
                    if self._activity_tracker is not None:
                        try:
                            self._activity_tracker()
                        except Exception:
                            pass
                    # Strip ConPTY-injected sequences (window ops, DA responses,
                    # private mode sets) but preserve SGR color/style codes.
                    stripped = _PTY_CSI_STRIP_RE.sub("", data)
                    stripped = ANSI_OSC_RE.sub("", stripped)
                    # Convert CUP (Cursor Position) to backspace using a
                    # virtual-column tracker so timeout.exe-style countdowns
                    # that jump to row 2, col N translate to the right number
                    # of \\b to overwrite the target digit.
                    def _handle_cup(m):
                        row = int(m.group(1) or 1)
                        col = int(m.group(2) or 1)
                        target = max(0, col - 1)
                        cur = self._virtual_col
                        if cur > target:
                            return "\b" * (cur - target)
                        if cur < target:
                            return " " * (target - cur)
                        return ""
                    stripped = _CUP_RE.sub(_handle_cup, stripped)
                    # Convert cursor-movement CSI / CHA / EL.
                    stripped = _CURSOR_LEFT_RE.sub(
                        lambda m: "\b" * int(m.group(1) or 1), stripped,
                    )
                    stripped = _CURSOR_RIGHT_RE.sub(
                        lambda m: " " * int(m.group(1) or 1), stripped,
                    )
                    stripped = _CURSOR_UP_RE.sub("\r", stripped)
                    stripped = _CURSOR_DOWN_RE.sub("\n", stripped)
                    def _handle_cha(m):
                        col = max(1, int(m.group(1) or 1))
                        target = max(0, col - 1)
                        cur = self._virtual_col
                        if cur > target:
                            return "\b" * (cur - target)
                        if cur < target:
                            return " " * (target - cur)
                        return ""
                    stripped = _CHA_RE.sub(_handle_cha, stripped)
                    stripped = _EL_RE.sub("", stripped)
                    # Advance virtual column for visible characters so that
                    # subsequent CUP conversions compute the correct offset.
                    for ch in stripped:
                        if ch == "\r":
                            self._virtual_col = 0
                        elif ch == "\n":
                            self._virtual_col = 0
                        elif ch == "\b":
                            if self._virtual_col > 0:
                                self._virtual_col -= 1
                        elif ch != "\x1b":
                            self._virtual_col += 1
                    if not stripped:
                        continue
                    # If the previous chunk was a single visible character that
                    # looks like a ConPTY table-clearing artifact (trailing
                    # \\ or / before a CHA+EL erase), buffer it.  Ordinary
                    # digits/letters are never buffered — they belong to live
                    # countdowns / progress updates and must pass through.
                    visible = stripped.replace("\r", "").replace("\n", "").replace("\b", "")
                    if (
                        visible == stripped
                        and len(stripped) == 1
                        and stripped not in "\r\n\b"
                        and stripped in {"\\", "/"}
                    ):
                        if self._pending_cha is None:
                            self._pending_cha = stripped
                            continue
                    # \r-only chunk (old CHA/EL path): the buffered character
                    # was being erased — drop both the buffer and the chunk.
                    if visible == "" and stripped.strip("\r") == "":
                        if self._pending_cha is not None:
                            self._pending_cha = None
                        continue
                    # \b-only chunk (CHA→\b* + EL path): still clear a
                    # buffered table artifact, but keep the \b* bytes —
                    # they may be CUP positioning needed by the output.
                    if visible == "" and stripped.strip("\b") == "":
                        if self._pending_cha is not None:
                            self._pending_cha = None
                    # Emit: prepend any non-erased buffered character.
                    if self._pending_cha is not None:
                        stripped = self._pending_cha + stripped
                        self._pending_cha = None
                    return stripped.encode("utf-8", errors="replace")
            except EOFError:
                return self._flush_pending()
        def _flush_pending(self) -> bytes:
            if self._pending_cha is not None:
                ch = self._pending_cha
                self._pending_cha = None
                if not _should_flush_pending_cha_at_eof(ch):
                    return b""
                return ch.encode("utf-8", errors="replace")
            return b""
        def read1(self, n=1024):
            return self.read(n)
        def close(self):
            self._flush_pending()

    class _WinPtyWriter:
        """Wrap winpty write() to accept bytes (like subprocess.PIPE)."""
        def __init__(self, pty_proc):
            self._pty = pty_proc
        def write(self, data: bytes):
            self._pty.write(data.decode("utf-8", errors="replace"))
        def flush(self):
            pass
        def close(self):
            pass

    class _WinPtyProc:
        """Mimic subprocess.Popen interface backed by a winpty ConPTY process."""
        _IDLE_EOF_TIMEOUT = 3.0

        def __init__(self, pty_proc):
            self._pty = pty_proc
            self.pid = pty_proc.pid
            self.returncode = None
            self._last_activity = time.time()
            self.stdout = _WinPtyReader(pty_proc, activity_tracker=self._track_activity)
            self.stdin = None
        def _track_activity(self):
            self._last_activity = time.time()
        def _maybe_send_eof(self):
            if hasattr(self._pty, "sendeof"):
                try:
                    self._pty.sendeof()
                except Exception:
                    pass
        def wait(self, timeout=None):
            import subprocess as _sp
            if timeout is None:
                while self._pty.isalive():
                    time.sleep(0.1)
                    if time.time() - self._last_activity > self._IDLE_EOF_TIMEOUT:
                        self._maybe_send_eof()
            else:
                deadline = time.time() + timeout
                while self._pty.isalive() and time.time() < deadline:
                    time.sleep(0.05)
                    if time.time() - self._last_activity > self._IDLE_EOF_TIMEOUT:
                        self._maybe_send_eof()
                if self._pty.isalive():
                    raise _sp.TimeoutExpired(cmd=self.pid, timeout=timeout)
            self.returncode = self._pty.exitstatus or 0
            return self.returncode
        def poll(self):
            if not self._pty.isalive():
                self.returncode = self._pty.exitstatus or 0
            return self.returncode
        def kill(self):
            self._pty.kill()
        def terminate(self):
            self._pty.terminate(force=True)


def _wait_for_process_exit_or_interactive_timeout(
    process: Any,
    agent: Any,
    activity_state: Dict[str, Any],
    idle_timeout: Optional[float] = None,
    max_total_timeout: Optional[float] = None,
) -> Tuple[int, bool]:
    """Wait for ``process`` to exit, auto-terminating it when it goes silent.

    Non-interactive shell execution attaches no stdin, so an interactive
    command (a REPL, ``npm init``, a pager, an editor, ...) blocks forever
    waiting for keys.  Detect that signature — no output for ``idle_timeout``
    seconds — and kill the whole process tree so the tool returns instead of
    spinning.  ``max_total_timeout`` bounds the wait unconditionally so a hung
    process can never keep a turn alive forever.

    ``activity_state`` is a shared dict whose ``last_activity`` key is refreshed
    by the pipe-reader thread every time the process produces output.

    Returns ``(returncode, timed_out)`` where ``timed_out`` is True when the
    process had to be auto-terminated.
    """
    if idle_timeout is None:
        idle_timeout = _SHELL_INTERACTIVE_IDLE_TIMEOUT
    if max_total_timeout is None:
        max_total_timeout = _SHELL_MAX_TOTAL_TIMEOUT
    poller = getattr(process, "poll", None)
    if not callable(poller):
        # Legacy process object without poll(): fall back to blocking wait().
        try:
            return int(process.wait() or 0), False
        except Exception:
            return -1, False
    start = time.time()
    while True:
        try:
            code = poller()
        except Exception:
            code = None
        if code is not None:
            return int(code or 0), False
        now = time.time()
        last_activity = float(activity_state.get("last_activity") or start)
        if (now - last_activity) >= idle_timeout:
            break
        if (now - start) >= max_total_timeout:
            break
        time.sleep(0.1)
    terminator = getattr(agent, "_terminate_single_process_tree", None)
    if callable(terminator):
        try:
            terminator(process)
        except Exception:
            pass
    try:
        code = process.wait(timeout=_SHELL_DRAIN_TIMEOUT)
    except Exception:
        code = None
    if code is None:
        try:
            process.kill()
        except Exception:
            pass
        try:
            code = process.poll()
        except Exception:
            code = None
    return int(code if code is not None else -1), True


def _abandoned_shell_return_code(process: Any) -> int:
    """Return code to report when a shell round is abandoned on user interrupt.

    The interrupt path already terminated the process tree; read the exit
    status if it has landed, otherwise fall back to the conventional 130
    (SIGINT) code like the direct ``!`` execution path.
    """
    try:
        poller = getattr(process, "poll", None)
        if callable(poller):
            code = poller()
            if code is not None:
                return int(code)
    except Exception:
        pass
    return 130


def _shell_abort_notice(agent: Any, process: Any) -> str:
    """Return the user-facing notice for an aborted shell round.

    Consumes the per-process pause mark so exactly one notice is chosen: a
    pause (GUI "send immediately" queue-jump) reports that the user
    interrupted the call to add more information; anything else keeps the
    stable ``command aborted by user`` cancel marker that callers match on.
    """
    consume_pause = getattr(agent, "_consume_process_pause", None)
    if callable(consume_pause):
        try:
            if bool(consume_pause(process)):
                return SHELL_PAUSE_ABORT_NOTICE
        except Exception:
            pass
    return SHELL_CANCEL_ABORT_NOTICE


def _resolve_shell_execution_cwd(agent: Any) -> Path:
    resolver = getattr(agent, "_shell_execution_cwd", None)
    if callable(resolver):
        try:
            resolved = resolver()
            if isinstance(resolved, Path):
                return resolved
            if resolved:
                return Path(str(resolved))
        except Exception:
            pass
    raw_root = getattr(agent, "workspace_root", None)
    if raw_root:
        try:
            root = Path(str(raw_root)).expanduser().resolve()
            if root.exists() and root.is_dir():
                return root
        except Exception:
            pass
    return Path(getattr(agent, "work_directory", Path.cwd()))


def _reset_work_directory_to_startup_initial(agent: Any) -> None:
    """Best-effort restore of agent work directory to startup initial directory after shell execution."""
    try:
        resetter = getattr(agent, "_reset_work_directory_to_startup_initial", None)
        if callable(resetter):
            resetter()
    except Exception:
        pass


def _count_output_lines(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return len(parts)


def _strip_console_color_controls(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    no_osc = ANSI_OSC_RE.sub("", raw)
    return ANSI_ESCAPE_RE.sub("", no_osc)


def _gray_shell_display_text(text: str) -> str:
    plain = _strip_console_color_controls(text)
    if not plain:
        return ""
    trailing_newline = plain.endswith("\n")
    core = plain[:-1] if trailing_newline else plain
    if not core:
        return "\n" if trailing_newline else ""
    colored = _ansi_gray(core)
    return colored + ("\n" if trailing_newline else "")


def _clear_streamed_output_window(
    stream: Any,
    rendered_lines: int,
    cursor_at_line_start: bool,
) -> None:
    lines = max(0, int(rendered_lines or 0))
    if lines <= 0:
        return
    try:
        stream.write("\r")
        if bool(cursor_at_line_start):
            stream.write("\x1b[1A")
        for idx in range(lines):
            stream.write("\r\x1b[2K")
            if idx < (lines - 1):
                stream.write("\x1b[1A")
        stream.write("\r")
        stream.flush()
    except Exception:
        pass


def _format_omitted_lines_notice(omitted_lines: int, stream: Any, language: Any = None) -> str:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate

    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    msg = translate("output.omitted_lines", lang, count=int(omitted_lines))
    try:
        if hasattr(stream, "isatty") and stream.isatty():
            return f"\x1b[90;3m{msg}\x1b[0m\n"
    except Exception:
        pass
    return msg + "\n"


def _terminal_columns_for_tail_display(stream: Any) -> int:
    try:
        fn = getattr(stream, _STREAM_ATTR_TERMINAL_COLUMNS, None)
        if callable(fn):
            cols = int(fn() or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    try:
        if stream is not None and hasattr(stream, "fileno"):
            cols = int(os.get_terminal_size(stream.fileno()).columns or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    try:
        cols = int(shutil.get_terminal_size(fallback=(80, 24)).columns or 80)
        if cols > 0:
            return cols
    except Exception:
        pass
    return 80


def _terminal_rows_for_tail_display(stream: Any) -> int:
    try:
        if stream is not None and hasattr(stream, "fileno"):
            rows = int(os.get_terminal_size(stream.fileno()).lines or 0)
            if rows > 0:
                return rows
    except Exception:
        pass
    try:
        rows = int(shutil.get_terminal_size(fallback=(80, 24)).lines or 24)
        if rows > 0:
            return rows
    except Exception:
        pass
    return 24


def _dynamic_tail_line_limit(
    stream: Any,
    max_tail_lines: int = SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    reserved_lines: int = SHELL_OUTPUT_DISPLAY_RESERVED_LINES,
) -> int:
    rows = _terminal_rows_for_tail_display(stream)
    safe_rows = max(1, int(rows) - max(0, int(reserved_lines or 0)))
    return min(safe_rows, max(1, int(max_tail_lines or 1)))


def _wrap_line_for_display(line: str, width: int) -> List[str]:
    clean = ANSI_ESCAPE_RE.sub("", str(line or "")).expandtabs(4)
    if not clean:
        return [""]
    w = max(1, int(width or 1))
    chunks: List[str] = []
    current: List[str] = []
    current_w = 0
    for ch in clean:
        if unicodedata.combining(ch):
            ch_w = 0
        else:
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf"):
                ch_w = 0
            else:
                ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if current and (current_w + ch_w > w):
            chunks.append("".join(current))
            current = [ch]
            current_w = ch_w
        else:
            current.append(ch)
            current_w += ch_w
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _visual_rows_for_display_text(text: str, width: int) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return sum(len(_wrap_line_for_display(line, width)) for line in lines)


def _build_tail_output_for_display(
    text: str,
    stream: Any,
    tail_lines: int = SHELL_OUTPUT_DISPLAY_TAIL_LINES,
    display_indent_width: int = 0,
    allow_partial_start_line: bool = True,
    language: Any = None,
) -> str:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text as _text

    raw = str(text or "")
    if not raw:
        return ""
    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    try:
        stream_indent = int(getattr(stream, _STREAM_ATTR_OUTPUT_INDENT_WIDTH, 0) or 0)
    except Exception:
        stream_indent = 0
    try:
        extra_indent = int(display_indent_width or 0)
    except Exception:
        extra_indent = 0
    content_width = max(1, _terminal_columns_for_tail_display(stream) - max(0, stream_indent + extra_indent))
    limit = max(1, int(tail_lines or 1))
    line_chunks = [_wrap_line_for_display(line, content_width) for line in lines]
    total_visual_rows = sum(len(chunks) for chunks in line_chunks)
    if total_visual_rows <= limit:
        return raw

    notice_text = f"... omitted {len(lines)} lines ..."
    notice_rows = max(1, _visual_rows_for_display_text(notice_text, content_width))
    remaining_rows = max(0, limit - notice_rows)
    selected_lines: List[str] = []
    selected_start = len(lines)
    partial_start_line = False
    used_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        chunks = line_chunks[idx]
        chunk_count = len(chunks)
        if used_rows + chunk_count <= remaining_rows:
            selected_lines.insert(0, lines[idx])
            selected_start = idx
            used_rows += chunk_count
            continue
        available = remaining_rows - used_rows
        if available > 0 and bool(allow_partial_start_line):
            selected_lines.insert(0, "\n".join(chunks[-available:]))
            selected_start = idx
            partial_start_line = True
        break

    if not selected_lines and lines:
        selected_lines.insert(0, lines[-1])
        selected_start = len(lines) - 1
        partial_start_line = False

    omitted = selected_start
    if partial_start_line:
        omitted += 1
    if not selected_lines:
        omitted = len(lines)
    tail = "\n".join(selected_lines)
    if tail and trailing_newline:
        tail += "\n"
    return _format_omitted_lines_notice(omitted, stream, language=language) + tail


def _select_logical_tail_output_for_live_replay(
    text: str,
    stream: Any,
    tail_lines: int,
    display_indent_width: int = 0,
) -> Tuple[str, int]:
    raw = str(text or "")
    if not raw:
        return "", 0
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    if not trailing_newline and lines:
        # During live replay the last logical line may still be arriving from
        # the subprocess. Do not use it to seed a resized window.
        lines = lines[:-1]
    if not lines:
        return "", 0
    limit = max(1, int(tail_lines or 1))
    try:
        stream_indent = int(getattr(stream, _STREAM_ATTR_OUTPUT_INDENT_WIDTH, 0) or 0)
    except Exception:
        stream_indent = 0
    try:
        extra_indent = int(display_indent_width or 0)
    except Exception:
        extra_indent = 0
    content_width = max(
        1,
        _terminal_columns_for_tail_display(stream) - max(0, stream_indent + extra_indent),
    )
    line_chunks = [_wrap_line_for_display(line, content_width) for line in lines]
    total_visual_rows = sum(len(chunks) for chunks in line_chunks)
    if total_visual_rows <= limit:
        return "\n".join(lines) + "\n", 0

    notice_text = f"... omitted {len(lines)} lines ..."
    notice_rows = max(1, _visual_rows_for_display_text(notice_text, content_width))
    remaining_rows = max(0, limit - notice_rows)
    selected: List[str] = []
    selected_start = len(lines)
    used_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        chunk_count = len(line_chunks[idx])
        if used_rows + chunk_count > remaining_rows:
            break
        selected.insert(0, lines[idx])
        selected_start = idx
        used_rows += chunk_count

    omitted = max(0, selected_start)
    if not selected:
        omitted = len(lines)
    tail = "\n".join(selected) + ("\n" if selected else "")
    return tail, omitted


def _build_logical_tail_output_for_live_replay(
    text: str,
    stream: Any,
    tail_lines: int,
    display_indent_width: int = 0,
    language: Any = None,
) -> str:
    tail, omitted = _select_logical_tail_output_for_live_replay(
        text,
        stream,
        tail_lines,
        display_indent_width=display_indent_width,
    )
    if omitted <= 0:
        return tail
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate as _translate

    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    return f"{_translate('output.omitted_lines', lang, count=omitted)}\n{tail}"


def _append_completed_output_lines(
    text: str,
    completed_lines: List[str],
    pending_state: Dict[str, str],
) -> None:
    chunk = str(text or "")
    if not chunk:
        return
    normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
    pending = str(pending_state.get("text", "")) + normalized
    parts = pending.split("\n")
    if pending.endswith("\n"):
        completed_lines.extend(parts[:-1])
        pending_state["text"] = ""
    else:
        completed_lines.extend(parts[:-1])
        pending_state["text"] = parts[-1] if parts else ""


def _enforce_windows_powershell_command_prefix(command: str) -> Dict[str, Any]:
    if os.name != "nt":
        return {"ok": True, "command": command}
    cmd = str(command or "").strip()
    if not cmd:
        return {"ok": True, "command": command}
    # Models occasionally wrap the entire ``powershell ...`` invocation in an
    # extra pair of double quotes (typically because over-eager JSON escaping
    # left ``\"`` markers around the whole token). cmd.exe then tries to
    # locate an executable literally named ``"powershell ..."`` and fails
    # with ``The system cannot find the path specified.``. Peel a single
    # surrounding double-quote layer when doing so reveals a recognizable
    # ``powershell`` invocation.
    if len(cmd) >= 2 and cmd[0] == '"' and cmd[-1] == '"':
        inner = cmd[1:-1].strip()
        if re.match(r"(?i)^powershell(?:\.exe)?\b", inner):
            cmd = inner
    if not re.match(r"(?i)^powershell(?:\.exe)?\b", cmd):
        return {"ok": True, "command": cmd}
    m = re.match(
        r"(?is)^powershell(?:\.exe)?\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(.+)$",
        cmd,
    )
    if not m:
        return {
            "ok": False,
            "error": 'On Windows, PowerShell must be called as: powershell -ExecutionPolicy Bypass -Command "<command>"',
        }
    # Normalize executable token to `powershell` while preserving the command payload.
    payload = m.group(1).strip()
    return {"ok": True, "command": f"powershell -ExecutionPolicy Bypass -Command {payload}"}


# Match a ``powershell ... -Command <payload>`` invocation in a way that's
# tolerant of mixed casing and the optional ``.exe`` suffix.
_WIN_POWERSHELL_COMMAND_RE = re.compile(
    r"(?is)^(?P<exe>powershell(?:\.exe)?)\s+(?P<head>(?:-ExecutionPolicy\s+Bypass\s+)?)"
    r"-Command\s+(?P<payload>.+)$"
)


def _strip_powershell_payload_quotes(payload: str) -> Tuple[str, str]:
    """Return ``(inner, quote_kind)`` after peeling one matched outer layer of
    quotes from a PowerShell ``-Command`` payload.

    PowerShell strings escape an embedded ``"`` as ``\\"`` (when the whole
    token came through cmd.exe) or as ``""`` (PS-native). For single-quoted
    strings the escape is ``''``. Quote kind is ``"d"``, ``"s"`` or ``""``.
    Also tolerates payloads where the model double-escaped the outer quotes
    (``\\"...\\"``) — that's invalid powershell-from-cmd syntax but the
    intent is unambiguous.
    """
    s = str(payload or "").strip()
    # Handle backslash-escaped outer double-quote wrapper (``\"...\"``) that
    # over-escaping models tend to produce. Peel both halves before falling
    # through to the standard quote-pair handling.
    if len(s) >= 4 and s.startswith('\\"') and s.endswith('\\"'):
        s = s[2:-2].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        quote = s[0]
        inner = s[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace('""', '"')
            return inner, "d"
        else:
            inner = inner.replace("''", "'")
            return inner, "s"
    return s, ""


_POWERSHELL_LITERAL_ESCAPE_RE = re.compile(r"\\(?P<ch>[nrt\"'])")


def _decode_powershell_literal_escapes(text: str) -> str:
    """Translate the literal escape sequences models commonly emit (``\\n``,
    ``\\r``, ``\\t``, ``\\\"``, ``\\'``) into the real characters they meant.

    PowerShell uses backtick escapes (``` `n ```), so ``\\n`` is otherwise a
    no-op two-character literal in the resulting script. Decoding them is
    therefore safe for legitimate scripts and unlocks here-strings (which
    *require* a real LF immediately after ``@'`` / ``@"``).
    """
    def _sub(match: "re.Match[str]") -> str:
        ch = match.group("ch")
        return {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "\"": "\"",
            "'": "'",
        }.get(ch, match.group(0))

    return _POWERSHELL_LITERAL_ESCAPE_RE.sub(_sub, str(text or ""))


def _powershell_payload_needs_normalization(payload: str) -> bool:
    """Heuristic: only rewrite payloads that are likely to fail cmd.exe round
    tripping. Keep simple one-liners untouched so we don't perturb commands
    the model already wrote correctly."""
    s = str(payload or "")
    if not s:
        return False
    if "\n" in s or "\r" in s:
        return True
    if "\\n" in s or "\\r" in s:
        return True
    if "@'" in s or '@"' in s:
        return True
    return False


def _encode_powershell_script_as_encoded_command(script: str) -> str:
    """Encode a PowerShell script for the ``-EncodedCommand`` parameter.

    PowerShell expects a Base64 string built from the script's UTF-16-LE
    bytes. This bypasses every layer of cmd.exe / PowerShell quoting since
    the encoded blob is plain ASCII with no whitespace or quote characters.
    """
    return base64.b64encode(str(script or "").encode("utf-16-le")).decode("ascii")


def _normalize_windows_powershell_command_for_compat(command: str) -> str:
    """Make best-effort fixes to common ``powershell -Command "..."`` mistakes
    on Windows so that semantically valid scripts the model emits actually
    reach PowerShell intact.

    Concretely:

    * Decodes literal ``\\n`` / ``\\r`` / ``\\t`` / ``\\"`` escape sequences
      in the ``-Command`` payload to real characters (no-op for normal PS
      strings; required for here-strings to parse).
    * When the resulting payload spans multiple lines or otherwise contains
      content that wouldn't survive cmd.exe argument parsing, re-emits the
      command as ``powershell -ExecutionPolicy Bypass -EncodedCommand
      <base64>`` so quoting concerns are bypassed entirely.

    Returns the original ``command`` unchanged on non-Windows hosts, when
    no PowerShell invocation is detected, or when the payload looks safe
    to leave alone.
    """
    if os.name != "nt":
        return command
    raw = str(command or "")
    if not raw.strip():
        return command
    match = _WIN_POWERSHELL_COMMAND_RE.match(raw.strip())
    if not match:
        return command
    payload_raw = match.group("payload").strip()
    if not _powershell_payload_needs_normalization(payload_raw):
        return command
    payload, quote_kind = _strip_powershell_payload_quotes(payload_raw)
    decoded = _decode_powershell_literal_escapes(payload)
    # If decoding didn't actually change anything and the payload is single
    # line, leave the command alone — the model already wrote something
    # cmd.exe can dispatch.
    if decoded == payload and "\n" not in decoded and "\r" not in decoded:
        # No newlines after decoding either: nothing to fix.
        if quote_kind:
            return command
    encoded = _encode_powershell_script_as_encoded_command(decoded)
    return f"powershell -ExecutionPolicy Bypass -EncodedCommand {encoded}"


# Tools whose non-executable arguments are patterns / pathspecs where a ``/``
# is significant and must not be rewritten to ``\`` (which would change the
# pattern semantics or be treated as an escape character).
_WINDOWS_EXE_ONLY_TOOLS = {
    "rg",
    "grep",
    "egrep",
    "fgrep",
    "findstr",
    "ag",
    "ack",
    "ripgrep",
    "sed",
    "awk",
    "perl",
    "git",
}

# Looks like a Windows filename (``name.ext``) — a strong file-path signal.
_WINDOWS_PATH_EXTENSION_RE = re.compile(r"(?i)\.[a-z0-9]{1,8}$")
# ``C:/...`` / ``c:\...`` style absolute paths.
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[a-z]:")
# Glob / regex metacharacters that would change meaning if a ``/`` inside
# the token were rewritten to ``\`` (``src/**/*.ts``, ``foo/bar.py$`` ...).
_WINDOWS_PATH_SKIP_CHARS_RE = re.compile(r"[*?\[\]{}()|^$+]")


def _convert_windows_path_token(tok: str, *, is_exe: bool) -> str:
    """Rewrite ``/`` separators in a single shell token on Windows.

    Returns the token unchanged when it is not a safe file-path candidate:
    URLs, git refs / scp-style remotes, option flags (``-x`` / ``/x``),
    globs and regex-looking tokens are all left alone.  ``is_exe`` marks the
    first token (the executable), which cmd.exe cannot resolve with ``/``.
    """
    if "/" not in tok:
        return tok
    quote = ""
    inner = tok
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        quote = tok[0]
        inner = tok[1:-1]
    if not inner or "/" not in inner:
        return tok
    if "://" in inner or "@" in inner:
        return tok
    if inner.startswith("-") or inner.startswith("/"):
        return tok
    if _WINDOWS_PATH_SKIP_CHARS_RE.search(inner):
        return tok
    # ``foo\/bar`` is an escaped slash in a regex, not a path.
    if "\\/" in inner:
        return tok
    if not is_exe:
        looks_like_path = (
            inner.startswith(".")
            or inner.startswith("~")
            or bool(_WINDOWS_DRIVE_PREFIX_RE.match(inner))
            or bool(_WINDOWS_PATH_EXTENSION_RE.search(inner))
        )
        if not looks_like_path:
            return tok
    converted = inner.replace("/", "\\")
    if quote:
        return quote + converted + quote
    return converted


def _normalize_windows_shell_path_separators(command: str) -> str:
    """Convert ``/`` path separators to ``\\`` in a Windows shell command.

    cmd.exe cannot resolve an executable whose path uses ``/``
    (``.venv-windows/Scripts/python.exe`` fails with ``'.venv-windows' is not
    recognized``) and treats ``/`` inside arguments of its built-ins as
    switches (``del cli/tests/x.txt`` → ``Invalid switch - "tests"``).

    Only path-like tokens are rewritten; URLs, option flags, git refs,
    glob/regex-looking tokens, PowerShell payloads and commands whose
    arguments are patterns/pathspecs (``rg``, ``grep``, ``git`` ...) are left
    untouched.  No-op on non-Windows hosts.
    """
    if os.name != "nt":
        return command
    s = str(command or "").strip()
    if not s:
        return command
    # PowerShell resolves ``/`` paths natively and an ``-EncodedCommand``
    # base64 blob may legitimately contain ``/`` — skip the whole invocation.
    if re.match(r"(?is)^(?:powershell|pwsh)(?:\.exe)?(?:\s|$)", s):
        return command
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        peeled = s[1:-1].strip()
        if re.match(r"(?i)^powershell(?:\.exe)?\b", peeled):
            return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    base0 = _token_exe_base(_strip_wrapping_quotes(parts[0]))
    if base0 == "cmd" and len(parts) >= 3 and parts[1].lower() in ("/c", "/k"):
        inner_raw = " ".join(parts[2:])
        inner = _strip_wrapping_quotes(inner_raw)
        if inner != inner_raw:
            inner = _strip_wrapping_quotes(inner)
        inner_norm = _normalize_windows_shell_path_separators(inner)
        if inner_norm == inner:
            return command
        import subprocess

        return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_norm])
    exe_only = base0 in _WINDOWS_EXE_ONLY_TOOLS
    new_parts = [
        _convert_windows_path_token(tok, is_exe=(i == 0))
        if (i == 0 or not exe_only)
        else tok
        for i, tok in enumerate(parts)
    ]
    rebuilt = " ".join(new_parts)
    if rebuilt == s:
        return command
    return call_prefix + rebuilt


def _sandbox_escalation_hint(reason: str, plan: Any) -> str:
    """Model-facing guidance shown when a failure looks sandbox-caused.

    The hint first asks the model to reflect on whether the command is truly
    necessary (or whether a sandbox-safe alternative exists), then explains
    how to request a one-time, user-approved sandbox bypass.
    """
    if reason == "not_provisioned":
        return (
            "This failure is a sandbox configuration problem, not a command "
            "problem. Run 'codewood sandbox setup' from an elevated terminal "
            "(or Settings > Security > Sandbox settings > Set up sandbox). "
            "Alternatively you may request a one-time sandbox bypass by "
            "re-running this command with `bypass_sandbox: true` on the "
            "shell tool (the user will be asked to approve)."
        )
    if reason == "network_blocked":
        return (
            "The sandbox has network access disabled, so this failure is very "
            "likely caused by the sandbox network block. First reflect on "
            "whether network access is truly necessary, or whether the "
            "dependency/package can be resolved offline. If it is truly "
            "required, you may request a one-time sandbox bypass by re-running "
            "this command with `bypass_sandbox: true` (the user will be asked "
            "to approve)."
        )
    return (
        "This failure is very likely caused by the sandbox "
        "(level=%s): the command may have tried to write outside the allowed "
        "workspace, touch a protected path, or use a capability the sandbox "
        "denies. First reflect on whether the command is truly necessary or "
        "whether a sandbox-safe alternative exists (prefer the built-in "
        "read/grep/glob tools and keep writes inside the workspace). If it is "
        "truly required, you may request a one-time sandbox bypass by "
        "re-running this command with `bypass_sandbox: true` (the user will "
        "be asked to approve)."
        % (getattr(plan, "level", "") or "sandbox")
    )


def _sandbox_failure_analysis(
    command: str,
    return_code: int,
    out: str,
    sandbox_plan: Any,
) -> Optional[Dict[str, Any]]:
    """Return sandbox-failure metadata when a failed command was very likely
    blocked by the sandbox; ``None`` when no sandbox was active or the failure
    does not look sandbox-caused.

    ``sandbox_plan`` must be the *effective* plan (``None`` after a bypass was
    approved), so an escalated run is never mislabelled as sandbox-caused.
    """
    if sandbox_plan is None:
        return None
    text = str(out or "")
    reason: Optional[str] = None
    if "Sandboxed command could not be started" in text:
        reason = "spawn_failure"
    elif int(return_code) == 5:
        # ERROR_ACCESS_DENIED — the classic sandbox-denied exit code.
        reason = "access_denied"
    elif _SANDBOX_ACCESS_DENIED_RE.search(text):
        reason = "access_denied"
    elif (
        (not bool(getattr(sandbox_plan, "network", True)))
        and _SANDBOX_NETWORK_COMMAND_RE.search(command)
        and _SANDBOX_NETWORK_RE.search(text)
    ):
        reason = "network_blocked"
    if reason is None:
        return None
    return {
        "sandbox_related": True,
        "sandbox_reason": reason,
        "sandbox_level": getattr(sandbox_plan, "level", "") or "",
        "sandbox_network": bool(getattr(sandbox_plan, "network", True)),
        "sandbox_escalation_hint": _sandbox_escalation_hint(
            reason, sandbox_plan
        ),
    }


def _apply_sandbox_escalation_approval(
    agent: Any,
    command: str,
    sandbox_plan: Any,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Ask the user to approve a one-time sandbox bypass for ``command``.

    Returns ``(approval, result)``:

    - ``('approved', None)`` — the user approved; the caller runs the command
      unsandboxed for this one call only.
    - ``('rejected', result)`` — plain reject; ``result`` ends the task.
    - ``('rejected_with_supplement', result)`` — reject + feedback;
      ``result`` keeps the task running with the user's text attached.
    """
    level = str(getattr(sandbox_plan, "level", "") or "sandbox")
    execution_policy = str(
        getattr(agent, "execution_policy", "confirmation")
    ).lower()
    if execution_policy == "unlimited":
        # Unlimited mode: the user has opted out of all safety checks and
        # confirmations, so a model sandbox-bypass request is auto-approved.
        _log.info("execution_policy=unlimited: sandbox bypass auto-approved")
        return "approved", None
    prompt_text = _t(
        agent,
        "execution_policy.prompt.escalate_sandbox_no_command",
        fallback=(
            "⚠️ The model requests to bypass the sandbox (level={level}) and "
            "run this command with full permissions (one-time). Approve? "
            "Yes=approve, No=reject and end the task, Reject & supplement "
            "info=reject but continue with your feedback."
        ).format(level=level),
        level=level,
    )
    ok = agent._prompt_confirm_yes_no_maybe_always(
        prompt_text,
        offer_always=False,
        kind="shell",
        shell_command=command,
        display_command=command,
    )
    if ok:
        return "approved", None
    from ..services.execution_policy_service import get_confirm_supplement

    if get_confirm_supplement(agent):
        declined = agent._confirm_declined_result(
            "User rejected the sandbox-escalation request; the command was "
            "not executed (sandboxed execution also skipped)."
        )
        declined.setdefault("sandbox_escalation_rejected", True)
        return "rejected_with_supplement", declined
    return (
        "rejected",
        {
            "success": False,
            "user_cancelled": True,
            "cancelled_by_user": True,
            "retryable": False,
            "sandbox_escalation_rejected": True,
            "error": (
                "User rejected the sandbox-escalation request; the command "
                "was NOT executed. This ends the current task: do not retry "
                "this command and do not request sandbox escalation again "
                "unless the user explicitly asks."
            ),
        },
    )


def _sandbox_active_for_agent(agent: Any) -> bool:
    """Return True when a sandbox plan would apply to this agent's commands.

    Mirrors the plan resolution used inside :func:`action_shell_command`
    (via the regular import, not the by-path load) so the shell tool can
    decide *before* execution whether a `bypass_sandbox` request will go
    through a human approval gate. Never raises.
    """
    try:
        from ..core.sandbox import (
            SANDBOX_LEVEL_FULL_ACCESS,
            normalize_sandbox_level,
            sandbox_plan_for_agent,
        )

        if (
            normalize_sandbox_level(getattr(agent, "sandbox_level", None))
            == SANDBOX_LEVEL_FULL_ACCESS
        ):
            return False
        return sandbox_plan_for_agent(agent) is not None
    except Exception:
        return False


def action_shell_command(
    agent: Any,
    command: str,
    confirmed: bool = False,
    interactive: bool = True,
    input_data: Optional[str] = None,
    bypass_sandbox: bool = False,
) -> dict:
    """Run a shell command; capture stdout/stderr for AI context while echoing to the terminal."""
    # Capture the thread-bound session key on the main thread BEFORE any
    # pipe-reader threads start.  This is the workspace-qualified key
    # (ws_id::chat_id) that _runtime_for_thread() uses for lookup —
    # stable even after the user switches workspace/chat.
    _shell_session_key = ""
    try:
        _shell_session_key = str(agent._current_session_chat_key() or "")
    except Exception:
        pass
    if not command.strip():
        return {"success": False, "error": "Command cannot be empty"}
    manual_confirm_from_ai = bool(getattr(agent, "_manual_confirm_required_shell_once", False))
    if manual_confirm_from_ai:
        agent._manual_confirm_required_shell_once = False
    command = ensure_absolute_script_for_shell_cwd(agent, command.strip())
    command = _normalize_windows_shell_path_separators(command)
    command = enforce_workspace_rg_for_shell_command(agent, command)
    command = tune_7z_output_for_piped_terminal(command, agent)
    command = _enforce_git_no_pager_for_shell_command(command)
    enforce_res = _enforce_windows_powershell_command_prefix(command)
    if not enforce_res.get("ok", False):
        return {"success": False, "error": str(enforce_res.get("error", "PowerShell command format is invalid"))}
    command = str(enforce_res.get("command") or command)
    # Rescue common Windows PowerShell quoting failures (literal ``\n`` in
    # here-strings, over-escaped wrappers, etc.) without changing the user-
    # visible command summary that already got captured upstream.
    command = _normalize_windows_powershell_command_for_compat(command)
    policy = agent._get_path_policy()
    decision = policy.can_run_shell_in_workdir(
        is_dependency_install=is_dependency_install_command(command),
        is_ai_workspace_script=is_ai_workspace_script_command(agent, command),
    )
    if not decision.get("allowed", False):
        return {"success": False, "error": decision.get("error", "")}
    agent._load_confirm_allowlist()

    # Resolve the sandbox plan (None => full access / unsupported platform).
    # A configured sandbox that is not provisioned fails closed: the command
    # is refused instead of silently running unsandboxed.
    sandbox_plan = None
    _sandbox_plan_original = None
    _sandbox_bypass_approved = False
    _sandbox_block = None
    try:
        # Load the sandbox implementation by absolute path: the GUI backend
        # must run the exact code from this repository regardless of any
        # sys.path / .pyc caching ambiguity.
        import importlib.util

        _sb_init = (
            Path(__file__).resolve().parent.parent / "core" / "sandbox" / "__init__.py"
        )
        _sb_spec = importlib.util.spec_from_file_location(
            "codewood_sandbox_runtime",
            _sb_init,
            submodule_search_locations=[str(_sb_init.parent)],
        )
        if _sb_spec is None or _sb_spec.loader is None:
            raise ImportError(f"cannot load sandbox module from {_sb_init}")
        _sb = importlib.util.module_from_spec(_sb_spec)
        # Register before exec_module: @dataclass looks up the class module in
        # sys.modules while the module body is still executing.
        sys.modules[_sb.__name__] = _sb
        _sb_spec.loader.exec_module(_sb)

        _sandbox_block = _sb.sandbox_block_error(agent)
        sandbox_plan = _sb.sandbox_plan_for_agent(agent)
        _sandbox_plan_original = sandbox_plan
        _log.info(
            "sandbox plan: level=%r network=%r plan=%s",
            getattr(agent, "sandbox_level", None),
            getattr(agent, "sandbox_network", None),
            sandbox_plan,
        )
    except Exception as exc:
        _log.warning("sandbox plan resolution failed: %s", exc)
        sandbox_plan = None

    # The escalation decision and fail-closed check live OUTSIDE the module
    # load try/except: an exception in the approval flow must never silently
    # fall back to an unsandboxed run (fail closed, not fail open).
    if bypass_sandbox and (sandbox_plan is not None or _sandbox_block):
        # Model-initiated privilege escalation: run this command unsandboxed
        # (one-time) only after the user approves. The approval prompt maps
        # to y=approve / n=reject (end task) / r=reject & supplement info
        # (continue task).
        _escalation, _escalation_result = _apply_sandbox_escalation_approval(
            agent, command, sandbox_plan
        )
        if _escalation_result is not None:
            return _escalation_result
        _sandbox_bypass_approved = True
        sandbox_plan = None
        _log.info("sandbox escalation approved by user; running unsandboxed")
    elif _sandbox_block:
        # Fail closed: a configured sandbox must never silently fall back to
        # an unsandboxed run. (``bypass_sandbox`` is the only explicit
        # user-approved exception, handled above.)
        _log.warning("sandbox fail-closed: %s", _sandbox_block)
        return {
            "success": False,
            "sandbox_related": True,
            "sandbox_reason": "not_provisioned",
            "sandbox_escalation_hint": _sandbox_escalation_hint(
                "not_provisioned", sandbox_plan
            ),
            "error": _sandbox_block,
        }

    # Determine which files this command targets for deletion *before* the
    # confirmation prompt so we can auto-skip approval when the model is
    # merely cleaning up files it created during the current task.
    execution_cwd = _resolve_shell_execution_cwd(agent)
    _delete_snapshots: Dict[str, str] = {}
    _all_delete_targets_self_created = False
    _all_delete_targets_in_cache = False
    try:
        if _is_potential_delete_command(command):
            # include_missing: safety classification must work even when the
            # cache is empty or the target file was already removed.
            _delete_targets_pre = _extract_delete_file_paths(
                command, execution_cwd, include_missing=True
            )
            if _delete_targets_pre:
                _delete_target_strs = {str(t) for t in _delete_targets_pre}
                _tracker_pre = getattr(agent, "file_change_tracker", None)
                if _tracker_pre is not None:
                    _tracker_changes = _tracker_pre.get_changes()
                    _created_paths = {
                        os.path.normcase(c.file_path) for c in _tracker_changes
                        if c.change_type == "create"
                    }
                    _all_delete_targets_self_created = (
                        bool(_delete_target_strs)
                        and all(
                            os.path.normcase(t) in _created_paths
                            for t in _delete_target_strs
                        )
                    )
                # Deleting files under the workspace cache directory is always
                # safe (cache is disposable) — skip the confirmation prompt.
                _all_delete_targets_in_cache = all(
                    policy.is_workspace_cache_path(t) for t in _delete_targets_pre
                ) and bool(_delete_targets_pre)
                # Cache files are disposable: exclude them from snapshotting /
                # backup / change-list tracking so a cache cleanup never
                # surfaces as a recorded file change (or a recoverable backup).
                _delete_targets_pre = [
                    t
                    for t in _delete_targets_pre
                    if not policy.is_workspace_cache_path(t)
                ]
                _delete_snapshots = _snapshot_files_content(_delete_targets_pre)
    except Exception:
        pass

    execution_policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
    in_allowlist = agent._shell_command_in_allowlist(command)
    force_manual_confirm_by_policy = (
        (
            ((execution_policy in ("moderate", "unlimited")) and (not confirmed))
            or manual_confirm_from_ai
        )
        and (not in_allowlist)
    )
    # A user-approved sandbox bypass already went through a human
    # confirmation for this exact command: never re-prompt the generic
    # command confirmation (and never let a policy/manual-confirm flag
    # re-require it).
    if _sandbox_bypass_approved:
        force_manual_confirm_by_policy = False
        confirmed = True
    # Hard guard: if AI/policy requires manual confirmation, never bypass it via confirmed=True.
    if force_manual_confirm_by_policy:
        confirmed = False
    should_prompt_confirm = (
        force_manual_confirm_by_policy
        or ((not confirmed) and (not in_allowlist))
    )
    # Auto-skip approval when the command only deletes files that the model
    # itself created during the current task.
    if should_prompt_confirm and (
        _all_delete_targets_self_created or _all_delete_targets_in_cache
    ):
        should_prompt_confirm = False
    if should_prompt_confirm:
        # The selection/inline confirmation UI renders the command on its own
        # styled line, so use a command-less question and pass the command
        # separately via ``display_command``. The plain-text fallback inside
        # ``_prompt_confirm_yes_no_maybe_always`` re-appends the command.
        if force_manual_confirm_by_policy:
            prompt_text = _t(
                agent,
                "execution_policy.prompt.manual_confirmation_required_no_command",
                fallback="⚠️ AI requires manual confirmation. Please confirm this command before continuing.",
            )
            confirm_reason = getattr(agent, "_last_auto_confirm_reason", None) or None
        else:
            prompt_text = _t(
                agent,
                "execution_policy.prompt.confirm_shell_no_command",
                fallback="⚠️ Confirm executing this system command?",
            )
            confirm_reason = None
        ok = agent._prompt_confirm_yes_no_maybe_always(
            prompt_text,
            offer_always=agent._shell_confirm_should_offer_always(command),
            kind="shell",
            shell_command=command,
            display_command=command,
            confirm_reason=confirm_reason,
        )
        if not ok:
            builder = getattr(agent, "_confirm_declined_result", None)
            if callable(builder):
                return builder("Operation cancelled by user")
            return {"success": False, "error": "Operation cancelled by user"}

    import subprocess

    merge_path: Optional[str] = None

    # Skip expensive file-change monitoring for commands that are known to
    # be read-only (dir, ls, cat, grep, git status, etc.).  This avoids
    # unnecessary git dirty/stash/workspace-snapshot overhead.
    _skip_file_monitoring = _is_read_only_command(command)

    # Snapshot the entire workspace file listing (metadata only) so we can
    # detect file creations and modifications after the command runs.
    _before_file_list: Dict[str, Tuple[float, int]] = {}
    if not _skip_file_monitoring:
        _before_file_list = _snapshot_workspace_file_list(execution_cwd)

    _repo_root = _git_repo_root(execution_cwd) if not _skip_file_monitoring else None
    # Snapshot the contents git cannot recover after the command overwrites
    # them (untracked files, tracked files with unstaged modifications, and
    # command-referenced paths) so real diffs can be built afterwards.
    # This replaces the previous git-stash round-trip, which could leave the
    # working tree in a conflicted state (3-way merge conflicts) whenever a
    # file had both staged and unstaged changes.
    _before_content_snapshot: Dict[str, str] = {}
    if not _skip_file_monitoring:
        _before_content_snapshot = _snapshot_workspace_before_content(
            command, execution_cwd, _repo_root,
        )
        _log.info("before-content snapshot: %d files repo_root=%s skip_monitor=%s",
                  len(_before_content_snapshot), _repo_root, _skip_file_monitoring)

    try:
        run_env = os.environ.copy()
        run_env.setdefault("PYTHONUTF8", "1")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")
        run_env.setdefault("PYTHONUNBUFFERED", "1")
        merge_env_name = resolve_model_context_file_env(agent, command)
        if merge_env_name:
            try:
                fd, merge_p = tempfile.mkstemp(prefix="modelctx_", suffix=".txt")
                os.close(fd)
                merge_path = merge_p
                run_env[merge_env_name] = merge_path
            except OSError:
                merge_path = None
        # Global policy: all shell/script execution is non-interactive.
        interactive = False
        return_code = -1
        timed_out = False
        out = ""
        displayed_out = ""
        aborted_by_user = False
        pause_interrupt = False
        status_ticker: Optional[_WorkingStatusTicker] = None
        status_ticker_lock = threading.Lock()

        def _stop_status_ticker() -> None:
            nonlocal status_ticker
            with status_ticker_lock:
                if status_ticker is None:
                    return
                try:
                    status_ticker.stop()
                finally:
                    status_ticker = None
        try:
            # The desktop GUI renders its own "Working.../Worked for" indicator,
            # so suppress the terminal ticker (which would otherwise leave a
            # stale "Working... (0s ...)" line in the GUI step output).
            if not bool(getattr(agent, "_gui_plain_stream", False)):
                feedback_line = str(getattr(agent, "_last_tool_call_feedback_line", "") or "")
                status_ticker = _SpinnerTicker(
                    sys.stdout,
                    prefix_line=feedback_line,
                    fps=SHELL_WORKING_STATUS_MARQUEE_FPS,
                )
                status_ticker.start()
            if interactive:
                import codecs

                stdout_chunks: List[str] = []
                stream_chunks_lock = threading.Lock()
                allow_realtime_echo = threading.Event()
                allow_realtime_echo.set()

                def _restore_console_after_interactive() -> None:
                    if sys.platform != "win32":
                        return
                    try:
                        _safe_console_write(
                            "\x1b[0m\x1b[?25h\x1b[?2004l",
                            sys.stdout,
                            append_newline=False,
                        )
                    except Exception:
                        pass

                def _stream_and_capture(
                    pipe: Any,
                    target: Any,
                    bucket: List[str],
                ) -> None:
                    if _shell_session_key:
                        _tls = agent.__dict__.get("_session_tls")
                        if _tls is not None:
                            try:
                                _tls.chat_id = _shell_session_key
                            except Exception:
                                pass
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    realtime_started = False
                    try:
                        while True:
                            if hasattr(pipe, "read1"):
                                chunk = pipe.read1(1024)
                            else:
                                chunk = pipe.read(1024)
                            if not chunk:
                                break
                            text_chunk = decoder.decode(chunk, final=False)
                            if text_chunk:
                                with stream_chunks_lock:
                                    bucket.append(text_chunk)
                                _stop_status_ticker()
                                if allow_realtime_echo.is_set():
                                    if not realtime_started:
                                        realtime_started = True
                                        ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                        if callable(ensure_line):
                                            try:
                                                ensure_line()
                                            except Exception:
                                                pass
                                    _safe_console_write(text_chunk, target, append_newline=False)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            with stream_chunks_lock:
                                bucket.append(tail)
                            _stop_status_ticker()
                            if allow_realtime_echo.is_set():
                                if not realtime_started:
                                    realtime_started = True
                                    ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                    if callable(ensure_line):
                                        try:
                                            ensure_line()
                                        except Exception:
                                            pass
                                _safe_console_write(tail, target, append_newline=False)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                try:
                    process = None
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        cwd=str(execution_cwd.resolve()),
                        env=run_env,
                        stdin=sys.stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=False,
                    )
                    reg_proc = getattr(agent, "_register_interruptible_process", None)
                    if callable(reg_proc):
                        reg_proc(process)
                    t_out = threading.Thread(
                        target=_stream_and_capture,
                        args=(process.stdout, sys.stdout, stdout_chunks),  # type: ignore[arg-type]
                        daemon=True,
                    )
                    t_out.start()
                    return_code = process.wait()
                    # Stop direct stream echo immediately after process exit to
                    # prevent delayed raw chunks from appearing in later turns.
                    allow_realtime_echo.clear()
                    t_out.join(timeout=_SHELL_DRAIN_TIMEOUT)
                    with stream_chunks_lock:
                        out = "".join(stdout_chunks)
                    out = _collapse_cr_output(out)
                    consume_abort = getattr(agent, "_consume_process_aborted", None)
                    if callable(consume_abort):
                        aborted_by_user = bool(consume_abort(process))
                    if aborted_by_user:
                        notice = _shell_abort_notice(agent, process)
                        # The user terminated the call: drop the partial output
                        # already read so only the abort notice is recorded.
                        out = notice
                        if notice == SHELL_PAUSE_ABORT_NOTICE:
                            pause_interrupt = True
                finally:
                    try:
                        unreg_proc = getattr(agent, "_unregister_interruptible_process", None)
                        if callable(unreg_proc):
                            unreg_proc(process)
                    except Exception:
                        pass
                    _restore_console_after_interactive()
            else:
                import codecs

                run_input: Optional[bytes] = None
                if input_data is not None:
                    run_input = str(input_data).encode("utf-8")
                stdout_chunks: List[str] = []
                stdout_completed_lines: List[str] = []
                stdout_pending_line_state: Dict[str, str] = {"text": ""}
                activity_state: Dict[str, Any] = {"last_activity": time.time()}
                stream_chunks_lock = threading.Lock()
                create_streams = getattr(agent, "_create_direct_shell_output_streams", None)
                process_ref: Dict[str, Any] = {"process": None}
                # Set once the main flow abandons the round on a user interrupt:
                # the background worker stops live display echo and discards the
                # residual pipe output instead of waiting for it to drain.
                abandoned = threading.Event()
                live_tail_limit = _dynamic_tail_line_limit(sys.stdout, reserved_lines=1)

                def _is_current_process_aborted() -> bool:
                    checker = getattr(agent, "_is_process_aborted", None)
                    if not callable(checker):
                        return False
                    try:
                        return bool(checker(process_ref.get("process")))
                    except Exception:
                        return False

                live_stream_state: Dict[str, Any] = {
                    "first_line_emitted": False,
                    "rendered_line_count": 0,
                    "cursor_at_line_start": True,
                    "_first_write_cleared_ticker_line": False,
                    "_first_text_emitted_notified": False,
                    "_suppress_first_write_clear": False,
                    "apply_gray": False,
                    "max_visible_lines": max(1, int(live_tail_limit)),
                    "max_visible_lines_provider": (
                        lambda: _dynamic_tail_line_limit(sys.stdout, reserved_lines=1)
                    ),
                    "suppress_leading_blank_once": True,
                    "on_text_emitted": _stop_status_ticker,
                    "suppress_desync_when": _is_current_process_aborted,
                }

                def _recover_live_window_desync_once() -> None:
                    if bool(live_stream_state.get("_desync_recovery_in_progress", False)):
                        return
                    live_stream_state["_desync_recovery_in_progress"] = True
                    live_stream_state["disable_live_render"] = True
                    _stop_status_ticker()
                    snapshot_out = ""
                    try:
                        reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
                        if callable(reload_fn):
                            try:
                                reload_fn(include_startup_overview=True)
                            except TypeError:
                                reload_fn()
                    except Exception:
                        pass
                    try:
                        with stream_chunks_lock:
                            snapshot_out = _strip_console_color_controls(
                                "".join(stdout_chunks)
                            )
                    except Exception:
                        pass
                    display_out = snapshot_out
                    live_limit_out = int(live_stream_state.get("max_visible_lines", 1) or 1)
                    omitted_base = 0
                    try:
                        live_limit_out = max(
                            1,
                            int(live_stream_state.get("max_visible_lines", 0) or 0),
                            int(_dynamic_tail_line_limit(sys.stdout, reserved_lines=1) or 0),
                        )
                        if display_out:
                            display_out, omitted_out = _select_logical_tail_output_for_live_replay(
                                display_out,
                                sys.stdout,
                                live_limit_out,
                                display_indent_width=4,
                            )
                            omitted_base += int(omitted_out or 0)
                            display_out = _strip_console_color_controls(display_out)
                    except Exception:
                        pass
                    if not display_out:
                        live_stream_state["_desync_skip_current_chunk"] = True
                        live_stream_state["drop_until_next_newline"] = bool(
                            str(stdout_pending_line_state.get("text", ""))
                        )
                        live_stream_state["disable_live_render"] = False
                        live_stream_state["_desync_recovery_in_progress"] = False
                        return
                    restore_limit = live_stream_state.get("max_visible_lines")
                    restore_provider = live_stream_state.get("max_visible_lines_provider")
                    try:
                        live_stream_state["first_line_emitted"] = bool(int(omitted_base or 0) > 0)
                        live_stream_state["rendered_line_count"] = 0
                        live_stream_state["cursor_at_line_start"] = True
                        live_stream_state["cursor_visual_col"] = 0
                        live_stream_state["_first_write_cleared_ticker_line"] = True
                        live_stream_state["_live_rendered_buffer"] = ""
                        live_stream_state["_live_omitted_base_lines"] = int(omitted_base or 0)
                        live_stream_state["suspend_desync_detection"] = True
                        live_stream_state["suspend_drop_until_next_newline"] = True
                        live_stream_state["max_visible_lines"] = max(
                            int(live_limit_out or 0) + 1000,
                            int(SHELL_OUTPUT_DISPLAY_TAIL_LINES or 0) + 1000,
                        )
                        live_stream_state["max_visible_lines_provider"] = None
                        if callable(create_streams):
                            preview_out, _ = create_streams(live_stream_state)
                        else:
                            preview_out = sys.stdout
                        live_stream_state["disable_live_render"] = False
                        if display_out:
                            preview_out.write(display_out)
                            preview_out.flush()
                        if display_out:
                            live_stream_state["_desync_skip_current_chunk"] = True
                            live_stream_state["drop_until_next_newline"] = bool(
                                str(stdout_pending_line_state.get("text", ""))
                            )
                        live_stream_state["_stream_local_state_version"] = int(
                            live_stream_state.get("_stream_local_state_version", 0) or 0
                        ) + 1
                    except Exception:
                        pass
                    finally:
                        try:
                            live_stream_state["max_visible_lines"] = restore_limit
                            live_stream_state["max_visible_lines_provider"] = restore_provider
                        except Exception:
                            pass
                        live_stream_state["suspend_drop_until_next_newline"] = False
                        live_stream_state["suspend_desync_detection"] = False
                        live_stream_state["disable_live_render"] = False
                        live_stream_state["_desync_recovery_in_progress"] = False

                live_stream_state["on_live_window_desynced"] = _recover_live_window_desync_once
                if callable(create_streams):
                    try:
                        out_stream, _ = create_streams(live_stream_state)
                    except Exception:
                        out_stream = sys.stdout
                else:
                    out_stream = sys.stdout

                def _stream_and_capture(
                    pipe: Any,
                    target: Any,
                    bucket: List[str],
                    completed_lines: List[str],
                    pending_line_state: Dict[str, str],
                ) -> None:
                    if _shell_session_key:
                        _tls = agent.__dict__.get("_session_tls")
                        if _tls is not None:
                            try:
                                _tls.chat_id = _shell_session_key
                            except Exception:
                                pass
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    realtime_started = False

                    def _write_display_chunk(text: str) -> None:
                        if abandoned.is_set():
                            return
                        try:
                            target.write(text)
                            target.flush()
                        except Exception:
                            # Rendering must not stop pipe draining. Keep the
                            # bounded live window intact and let the final tail
                            # replay come from the captured output.
                            pass

                    try:
                        while True:
                            if hasattr(pipe, "read1"):
                                chunk = pipe.read1(1024)
                            else:
                                chunk = pipe.read(1024)
                            if not chunk:
                                break
                            text_chunk = decoder.decode(chunk, final=False)
                            if text_chunk:
                                activity_state["last_activity"] = time.time()
                                with stream_chunks_lock:
                                    bucket.append(text_chunk)
                                    _append_completed_output_lines(
                                        text_chunk,
                                        completed_lines,
                                        pending_line_state,
                                    )
                                _stop_status_ticker()
                                if not realtime_started:
                                    realtime_started = True
                                    ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                    if callable(ensure_line):
                                        try:
                                            ensure_line()
                                        except Exception:
                                            pass
                                _write_display_chunk(text_chunk)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            activity_state["last_activity"] = time.time()
                            with stream_chunks_lock:
                                bucket.append(tail)
                                _append_completed_output_lines(
                                    tail,
                                    completed_lines,
                                    pending_line_state,
                                )
                            _stop_status_ticker()
                            if not realtime_started:
                                realtime_started = True
                                ensure_line = getattr(agent, "_ensure_terminal_line_start", None)
                                if callable(ensure_line):
                                    try:
                                        ensure_line()
                                    except Exception:
                                        pass
                            _write_display_chunk(tail)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                worker_state: Dict[str, Any] = {
                    "done": threading.Event(),
                    "return_code": -1,
                    "timed_out": False,
                }

                def _run_shell_worker() -> None:
                    # Bind the workspace-qualified chat key on the worker thread
                    # so the interruptible-process registration lands under the
                    # right chat bucket (serve-mode per-chat interrupt scoping).
                    if _shell_session_key:
                        _tls = agent.__dict__.get("_session_tls")
                        if _tls is not None:
                            try:
                                _tls.chat_id = _shell_session_key
                            except Exception:
                                pass
                    process = None
                    try:
                        _sandbox_spawn_error = None
                        _log.info(
                            "sandbox worker branch: sandbox_plan=%s",
                            sandbox_plan,
                        )
                        try:
                            import inspect as _inspect

                            _backend_cls = type(sandbox_plan.backend)
                            _log.info(
                                "sandbox pre-spawn: backend=%s.%s source=%s",
                                _backend_cls.__module__,
                                _backend_cls.__name__,
                                _inspect.getsourcefile(_backend_cls),
                            )
                        except Exception as _sb_diag:
                            _log.info("sandbox pre-spawn diag failed: %s", _sb_diag)
                        if sandbox_plan is not None:
                            try:
                                process = sandbox_plan.spawn(
                                    command,
                                    cwd=str(execution_cwd.resolve()),
                                    env=run_env,
                                    stdin_data=run_input,
                                )
                                _log.info(
                                    "sandbox spawn returned: process=%s pid=%s",
                                    type(process).__name__,
                                    getattr(process, "pid", "?"),
                                )
                                try:
                                    _sb_win = sys.modules.get(
                                        "codewood_sandbox_runtime.windows"
                                    )
                                    if _sb_win is not None:
                                        _sb_user = _sb_win._process_user_sid(
                                            int(getattr(process, "pid", 0) or 0)
                                        )
                                        _log.info(
                                            "sandbox spawned process user: %s",
                                            _sb_user,
                                        )
                                        # Hard fail-closed: the spawned process
                                        # must run as a sandbox user; anything
                                        # else means the sandbox did not apply.
                                        if _sb_user and (
                                            "codewoodsand" not in _sb_user.lower()
                                        ):
                                            try:
                                                process.kill()
                                            except Exception:
                                                pass
                                            raise RuntimeError(
                                                "sandbox process user mismatch: "
                                                f"{_sb_user} (expected sandbox user)"
                                            )
                                except Exception as _sb_user_err:
                                    _log.info(
                                        "sandbox user lookup failed: %s",
                                        _sb_user_err,
                                    )
                            except Exception as _sandbox_err:
                                # Fail closed: a sandboxed command must never
                                # fall back to an unsandboxed run.
                                _sandbox_spawn_error = str(_sandbox_err)
                                process = None
                                _log.info(
                                    "sandbox spawn raised: %s", _sandbox_err
                                )
                        if _sandbox_spawn_error is not None:
                            worker_state["spawn_error"] = _sandbox_spawn_error
                            worker_state["return_code"] = -1
                            worker_state["done"].set()
                            return
                        _winpty_obj = None
                        # PowerShell -Command invocations don't need a pty;
                        # winpty's ConPTY can interfere with output capture.
                        _is_ps_command = bool(
                            re.match(r"(?i)^powershell(?:\.exe)?\s", command.strip())
                        )
                        # pywinpty spawn() passes argv through subprocess.list2cmdline
                        # which escapes internal double-quotes with backslashes (Unix
                        # convention).  cmd.exe does not recognise that convention, so
                        # commands that contain their own double quotes would receive
                        # mangled arguments.  Skip the winpty path for those commands
                        # and let them fall through to the regular pipe-based Popen
                        # which uses shell=True and preserves quoting correctly.
                        _command_has_quotes = '"' in command
                        if (
                            # A sandbox plan has already supplied a process.
                            # Never replace it with WinPTY: WinPTY starts the
                            # command as the desktop user and would bypass the
                            # sandbox identity and its ACL restrictions.
                            process is None
                            and _WINPTY_PTYPROCESS is not None
                            and subprocess.Popen is _ORIG_SUBPROCESS_POPEN
                            and not _is_ps_command
                            and not _command_has_quotes
                        ):
                            try:
                                _comspec = run_env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
                                _raw_pty = _WINPTY_PTYPROCESS.spawn(
                                    [_comspec, "/c", command],
                                    cwd=str(execution_cwd.resolve()),
                                    env=run_env,
                                )
                                _winpty_obj = _WinPtyProc(_raw_pty)
                                _winpty_obj.stdin = _WinPtyWriter(_raw_pty)
                                process = _winpty_obj
                            except Exception:
                                _winpty_obj = None
                        if process is None:
                            process = subprocess.Popen(
                                command,
                                shell=True,
                                cwd=str(execution_cwd.resolve()),
                                env=run_env,
                                stdin=subprocess.DEVNULL if run_input is None else subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=False,
                            )
                        process_ref["process"] = process
                        if run_input is not None:
                            try:
                                if process.stdin is not None:
                                    process.stdin.write(run_input)
                                    process.stdin.flush()
                            except Exception:
                                pass
                            finally:
                                try:
                                    if process.stdin is not None:
                                        process.stdin.close()
                                except Exception:
                                    pass
                        reg_proc = getattr(agent, "_register_interruptible_process", None)
                        if callable(reg_proc):
                            reg_proc(process)
                        _abort_event_api = getattr(agent, "_register_process_abort_event", None)
                        if callable(_abort_event_api):
                            try:
                                _abort_event_api(process, worker_state["done"])
                            except Exception:
                                pass
                        t_out = threading.Thread(
                            target=_stream_and_capture,
                            args=(
                                process.stdout,
                                out_stream,
                                stdout_chunks,
                                stdout_completed_lines,
                                stdout_pending_line_state,
                            ),  # type: ignore[arg-type]
                            daemon=True,
                        )
                        t_out.start()
                        try:
                            code, timed = _wait_for_process_exit_or_interactive_timeout(
                                process,
                                agent,
                                activity_state,
                            )
                        finally:
                            # Abandoned rounds (user interrupt) must not wait for
                            # the pipe to drain: the main flow already returned
                            # and the residual output is being discarded.
                            if not abandoned.is_set():
                                _checker = getattr(agent, "_is_process_aborted", None)
                                _aborted_now = False
                                if callable(_checker):
                                    try:
                                        _aborted_now = bool(_checker(process))
                                    except Exception:
                                        _aborted_now = False
                                if not _aborted_now:
                                    t_out.join(timeout=_SHELL_DRAIN_TIMEOUT)
                        worker_state["return_code"] = int(code if code is not None else -1)
                        worker_state["timed_out"] = bool(timed)
                    except Exception:
                        worker_state["return_code"] = -1
                    finally:
                        worker_state["done"].set()

                worker_thread = threading.Thread(
                    target=_run_shell_worker,
                    name="codewood-shell-exec",
                    daemon=True,
                )
                worker_thread.start()

                # No polling: block on a single event that the worker sets on
                # completion, or that the interrupt path sets directly (via the
                # registered abort event) the moment it terminates the process.
                # A user stop therefore returns immediately — the worker keeps
                # cleaning up in the background and all residual output after
                # this point is abandoned.
                try:
                    worker_state["done"].wait()
                    consume_abort = getattr(agent, "_consume_process_aborted", None)
                    if callable(consume_abort):
                        try:
                            aborted_by_user = bool(consume_abort(process_ref.get("process")))
                        except Exception:
                            aborted_by_user = False
                    if aborted_by_user:
                        abandoned.set()
                        live_stream_state["suspend_desync_detection"] = True
                        try:
                            agent._suppress_next_prompt_chat_reload_once = True
                        except Exception:
                            pass
                        return_code = _abandoned_shell_return_code(process_ref.get("process"))
                    else:
                        # The round completed normally (or the idle watchdog
                        # auto-terminated it): the worker already drained the pipe.
                        return_code = int(worker_state.get("return_code", -1))
                        timed_out = bool(worker_state.get("timed_out", False))
                    with stream_chunks_lock:
                        out = "".join(stdout_chunks)
                    out = _collapse_cr_output(out)
                    _log.info("sandbox shell captured out: %r", out[:300])
                    _spawn_error = str(worker_state.get("spawn_error") or "")
                    if _spawn_error:
                        out = (
                            "⚠️ Sandboxed command could not be started: "
                            f"{_spawn_error}"
                        )
                    if aborted_by_user:
                        notice = _shell_abort_notice(agent, process_ref.get("process"))
                        # The user terminated the call: drop the partial output
                        # already read so only the abort notice is recorded.
                        out = notice
                        if notice == SHELL_PAUSE_ABORT_NOTICE:
                            pause_interrupt = True
                    if timed_out:
                        out = str(out) + (
                            "\n⚠️ Command was auto-terminated: it produced no output "
                            f"for {int(_SHELL_INTERACTIVE_IDLE_TIMEOUT)}s and was "
                            "treated as an interactive prompt waiting for input. "
                            "Supply the input non-interactively (e.g. flags, a "
                            "script, piped stdin) or use the interactive console "
                            "if a human must operate it.\n"
                        )
                finally:
                    # The main flow owns unregistration: consume must happen
                    # before the abort mark is discarded, and the worker may
                    # still be running in the background when we return.
                    try:
                        unreg_proc = getattr(agent, "_unregister_interruptible_process", None)
                        if callable(unreg_proc):
                            unreg_proc(process_ref.get("process"))
                    except Exception:
                        pass
            _stop_status_ticker()

            rg_error: Optional[str] = _rg_stderr_retry(
                command, out, return_code, execution_cwd, run_env,
            ) or None

            out = append_shell_merge_output_path(out, return_code, merge_path)
            out_tail_limit = _dynamic_tail_line_limit(sys.stdout)
            displayed_out = _build_tail_output_for_display(
                out,
                sys.stdout,
                out_tail_limit,
                language=getattr(agent, "display_language", None),
            )
            displayed_out_plain = _strip_console_color_controls(displayed_out)
            should_replay_out = True
            if interactive:
                # Interactive mode already streamed raw output to console.
                # Skip replay when output fully fits within the tail limit and
                # no post-processing changed the displayed text.
                if displayed_out and (_count_output_lines(out) <= out_tail_limit) and (displayed_out == out):
                    should_replay_out = False
            gui_mode = bool(getattr(agent, "_gui_no_wrap", False))
            gui_streamed = (
                bool(live_stream_state.get("_first_text_emitted_notified"))
                if gui_mode
                else False
            )
            if gui_mode:
                # The GUI streamed the full raw output live and cannot clear a
                # terminal window, so never replay the tail-truncated copy.
                should_replay_out = False
                # Close the command-output block opened by the raw stream so the
                # GUI can render it as a single padded node. The no-output case
                # is wrapped by the history-output replay path below instead.
                if gui_streamed:
                    try:
                        sys.stdout.write(GUI_CMD_OUTPUT_END)
                        sys.stdout.flush()
                    except Exception:
                        pass
            last_rendered_chunk = ""
            replay_out_text = displayed_out_plain if (displayed_out and should_replay_out) else ""
            if (
                (not replay_out_text)
                and int(return_code) == 0
                and (not aborted_by_user)
                and not (gui_mode and gui_streamed)
            ):
                replay_out_text = "(no output)\n"
            replay_rendered_lines = 0
            lock_obj = live_stream_state.get("_write_lock")
            lock_ctx = lock_obj if hasattr(lock_obj, "__enter__") and hasattr(lock_obj, "__exit__") else contextlib.nullcontext()
            with lock_ctx:
                if not gui_mode:
                    _clear_streamed_output_window(
                        sys.stdout,
                        int(live_stream_state.get("rendered_line_count", 0) or 0),
                        bool(live_stream_state.get("cursor_at_line_start", True)),
                    )
                if replay_out_text:
                    replay_direct = getattr(agent, "_print_direct_shell_history_output", None)
                    if callable(replay_direct):
                        try:
                            replay_rendered_lines = max(
                                0,
                                int(replay_direct(replay_out_text, "") or 0),
                            )
                        except Exception:
                            replay_rendered_lines = 0
                    else:
                        if replay_out_text:
                            _safe_console_write(_gray_shell_display_text(replay_out_text), sys.stdout, append_newline=False)
                        replay_rendered_lines = max(
                            0,
                            _count_output_lines(replay_out_text),
                        )
                    last_rendered_chunk = replay_out_text
                if (not last_rendered_chunk) and interactive:
                    if (not should_replay_out) and out:
                        last_rendered_chunk = out
                # Keep next assistant/status lines on a fresh line even when command
                # output does not end with newline. This avoids off-by-one over-clear
                # caused by mixing "Thinking..." into the output's last visual line.
                if last_rendered_chunk and not str(last_rendered_chunk).endswith("\n"):
                    _safe_console_write("\n", sys.stdout, append_newline=False)
                    replay_out_text = str(replay_out_text) + "\n"
                try:
                    agent._last_terminal_block_kind = "command_output"
                    agent._terminal_cursor_at_line_start = True
                except Exception:
                    pass
                try:
                    agent._last_shell_output_visible_lines = 0
                except Exception:
                    pass
                banner_lines = 0
                if aborted_by_user:
                    try:
                        banner_fn = getattr(agent, "_print_conversation_interrupted_banner", None)
                        if callable(banner_fn):
                            banner_lines = int(banner_fn() or 0)
                    except Exception:
                        banner_lines = 0
            # Determine the chat data directory for full output storage.
            _shell_output_path: Optional[Path] = None
            try:
                _chat_mgr = getattr(agent, "_chat_state_manager", None)
                _chat_id = str(getattr(agent, "active_chat_id", "") or "")
                if _chat_mgr is not None and _chat_id:
                    _data_dir = _chat_mgr.chat_data_dir_for_chat(_chat_id)
                    if _data_dir is not None:
                        _stem = datetime.datetime.now().strftime("shell_output_%Y%m%d_%H%M%S_%f")
                        _shell_output_path = _data_dir / f"{_stem}.txt"
            except Exception:
                _shell_output_path = None

            _shell_buf = CommandExecutionBuffer(out)
            _shell_rendered = _shell_buf.render(file_path=_shell_output_path)
            _shell_was_truncated = bool(_shell_output_path) and (_shell_rendered != _shell_buf.raw)

            base_out: Dict[str, Any] = {
                "output": _shell_rendered,
                "return_code": return_code,
                "timed_out": bool(timed_out),
                "interactive": interactive,
                "aborted_by_user": bool(aborted_by_user),
                "pause_interrupt": bool(pause_interrupt),
                "display_output": replay_out_text,
                "display_rendered_lines": int(replay_rendered_lines) + int(banner_lines),
            }
            # Surface sandbox context so the model knows this command ran
            # sandboxed (or that a one-time user-approved bypass was used).
            if _sandbox_plan_original is not None:
                base_out["sandbox_level"] = _sandbox_plan_original.level
                base_out["sandbox_network"] = bool(_sandbox_plan_original.network)
                if _sandbox_bypass_approved:
                    base_out["sandbox_bypassed"] = True
            if _shell_was_truncated:
                base_out["full_output_path"] = str(_shell_output_path)
            if is_file_read_shell_command(command):
                base_out["read_tool_hint"] = (
                    "Shell command was used to read file content. "
                    "Consider using the `read` tool instead for better file handling."
                )

            # ---- inline-code diagnostic: on Windows, cmd.exe does *not*
            #      understand \" as an escaped quote.  A python -c "...\""...\" "
            #      gets truncated at the first unescaped \" — the remaining
            #      code is silently discarded (exit 0, no output) or runs a
            #      truncated snippet that produces SyntaxErrors/Warnings.
            #      Log a warning so the model can see that something went wrong.
            if (
                return_code == 0
                and _is_inline_code_command(command)
                and os.name == "nt"
                and ("\\\"" in command or '\\"' in command)
                and (
                    not _shell_rendered.strip()
                    or "SyntaxWarning" in _shell_rendered
                    or "SyntaxError" in _shell_rendered
                )
            ):
                _warning = (
                    "\n\n⚠️  Command exited 0 but produced no normal output "
                    "(or only a SyntaxWarning/SyntaxError).  The sequence ``\\\"`` "
                    "is not an escape on Windows; cmd.exe treats the ``\"`` as "
                    "ending the quoted argument, which may have silently truncated "
                    "your script.  Remedy: use single quotes for Python string "
                    "literals inside a double-quoted -c argument "
                    "(e.g. ``'__main__'`` instead of ``\\\"__main__\\\"``), or "
                    "write the script to a temporary file and execute that "
                    "instead of using -c."
                )
                _shell_rendered = _shell_rendered + _warning
                base_out["output"] = _shell_rendered

            _shell_diff_entries: List[Dict[str, Any]] = []

            # Check for file deletions: compare snapshotted files against
            # current filesystem state, backup deleted content, and record
            # a delete change via the file_change_tracker.
            _log.info("delete_snapshots: %d entries, skip_monitoring=%s",
                      len(_delete_snapshots), _skip_file_monitoring)
            if not _skip_file_monitoring and _delete_snapshots:
                try:
                    _chat_mgr2 = getattr(agent, "_chat_state_manager", None)
                    _chat_id2 = str(getattr(agent, "active_chat_id", "") or "")
                    _backups_dir: Optional[Path] = None
                    if _chat_mgr2 is not None and _chat_id2:
                        _backups_dir = _chat_mgr2.chat_backups_dir_for_chat(_chat_id2)
                    _tracker = getattr(agent, "file_change_tracker", None)
                    for _path_str, _content in _delete_snapshots.items():
                        _p = Path(_path_str)
                        if not _p.exists():
                            _backup_name: Optional[str] = None
                            if _backups_dir is not None:
                                _backup_name = _backup_deleted_file(_content, _p, _backups_dir)
                            if _tracker is not None:
                                _tracker.record_delete(
                                    file_path=_path_str,
                                    source="shell",
                                    content_before=_content,
                                    backup_path=_backup_name,
                                )
                                _tracker.cancel_create_for_deleted_file(_path_str)
                            try:
                                from ..services.execution_policy_service import (
                                    freedom_remove_user_script_review_cache_entry,
                                )
                                freedom_remove_user_script_review_cache_entry(agent, _p)
                            except Exception:
                                pass
                            _del_rows = _build_all_del_diff_rows(_content)
                            _del_entry: Dict[str, Any] = {
                                "file": _path_str,
                                "changeType": "delete",
                                "diffRows": _del_rows,
                            }
                            if _backup_name:
                                _del_entry["backupPath"] = _backup_name
                            _shell_diff_entries.append(_del_entry)
                except Exception:
                    pass

            # Detect file creations and modifications by comparing the pre-
            # and post-execution workspace file listings.  Record create /
            # modify changes via the file_change_tracker and collect diff
            # preview data for GUI rendering.
            try:
                _after_file_list = _snapshot_workspace_file_list(execution_cwd) if not _skip_file_monitoring else {}
                _new, _modified, _ws_deleted = _diff_workspace_snapshots(
                    _before_file_list, _after_file_list,
                )
                # Only report file changes (creates, modifications, deletions)
                # for paths that the command explicitly references.  This prevents
                # false attribution of user edits and editor backup files that
                # happen during command execution.  When the command references
                # no files at all (e.g. "timeout 10"), the set is empty and all
                # three lists are naturally cleared.
                _cmd_paths = _extract_command_file_paths(command, execution_cwd)
                _cmd_paths_before_scan = set(_cmd_paths)
                _cmd_paths = expand_command_file_paths(
                    command, execution_cwd, _cmd_paths,
                )
                _scan_added = _cmd_paths - _cmd_paths_before_scan
                if _scan_added:
                    _log.info("scan added %d paths to cmd_paths: %s",
                              len(_scan_added),
                              ", ".join(Path(p).name for p in _scan_added))
                _new_before_filter = len(_new)
                _modified_before_filter = len(_modified)
                _ws_deleted_before_filter = len(_ws_deleted)
                # Detect renames BEFORE the cmd_paths filter: the old path no
                # longer exists on disk so ``_extract_command_file_paths``
                # cannot resolve it, and without pairing it would be dropped
                # as an unattributed deletion (leaving only a bogus "create"
                # record for the new path).  Only pair when the new path is
                # explicitly referenced by the command.
                _rename_pairs = _detect_rename_pairs(
                    _new, _ws_deleted, _before_content_snapshot, _repo_root,
                    _cmd_paths, _delete_snapshots,
                )
                _renamed_new = {_n for _, _n in _rename_pairs}
                _renamed_old = {_o for _o, _ in _rename_pairs}
                if _rename_pairs:
                    _log.info("detected %d rename pair(s): %s",
                              len(_rename_pairs),
                              ", ".join(f"{Path(o).name} -> {Path(n).name}"
                                        for o, n in _rename_pairs))
                _new = [
                    p for p in _new
                    if p not in _renamed_new
                    and p in _cmd_paths
                    and not policy.is_workspace_cache_path(Path(p))
                ]
                _modified = [
                    p for p in _modified
                    if p in _cmd_paths and not policy.is_workspace_cache_path(Path(p))
                ]
                _ws_deleted = [
                    p for p in _ws_deleted
                    if p not in _renamed_old
                    and p in _cmd_paths
                    and not policy.is_workspace_cache_path(Path(p))
                ]
                _log.info("cmd_paths filter: new %d→%d modified %d→%d ws_deleted %d→%d",
                          _new_before_filter, len(_new),
                          _modified_before_filter, len(_modified),
                          _ws_deleted_before_filter, len(_ws_deleted))
                if _ws_deleted:
                    _log.info("ws_deleted paths: %s",
                              ", ".join(Path(p).name for p in _ws_deleted))
                _tracker2 = getattr(agent, "file_change_tracker", None)
                for _old_path, _new_path in _rename_pairs:
                    try:
                        _rcontent = Path(_new_path).read_text(
                            encoding="utf-8", errors="replace",
                        )
                    except Exception:
                        continue
                    _rrows = _build_all_add_diff_rows(_rcontent)
                    if _tracker2 is not None:
                        _tracker2.record_change(
                            file_path=_new_path,
                            change_type="rename",
                            source="shell",
                            content_before=_before_content_snapshot.get(_old_path) or "",
                            content_after=_rcontent,
                            patch=_rrows,
                            old_path=_old_path,
                        )
                    _shell_diff_entries.append({
                        "file": _new_path,
                        "changeType": "rename",
                        "oldPath": _old_path,
                        "diffRows": _rrows,
                    })
                for _path_str in _new:
                    try:
                        _content = Path(_path_str).read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    # If the file existed before the shell ran (e.g. it was
                    # untracked or had unstaged changes), its original content
                    # is in the before-content snapshot.  Fall back to git
                    # (index → HEAD) for clean tracked files so we can show a
                    # real diff instead of marking the whole file as added.
                    _new_before: Optional[str] = None
                    _new_before = _before_content_snapshot.get(_path_str)
                    if _new_before is None and _repo_root is not None:
                        _new_before = _git_content_before(_repo_root, _path_str)
                    if _new_before is not None:
                        _diff_rows_new = _build_real_diff_rows(_new_before, _content)
                        _change_type = "modify"
                        _cb_new = _new_before
                        _log.info("new file got before from stash: %s", _path_str)
                    else:
                        _diff_rows_new = _build_all_add_diff_rows(_content)
                        _change_type = "create"
                        _cb_new = ""
                        _log.info("new file no before: %s", _path_str)
                    if _tracker2 is not None:
                        _tracker2.record_change(
                            file_path=_path_str,
                            change_type=_change_type,
                            source="shell",
                            content_before=_cb_new,
                            content_after=_content,
                            patch=_diff_rows_new,
                        )
                    _shell_diff_entries.append({
                        "file": _path_str,
                        "changeType": _change_type,
                        "diffRows": _diff_rows_new,
                    })
                for _path_str in _modified:
                    try:
                        _content = Path(_path_str).read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    _log.info("checking modified: %s (len=%d)", _path_str, len(_content))
                    # Detect binary files — diff rows are meaningless and
                    # we should back up the old content for recovery.
                    _is_binary = _is_binary_file(_path_str)
                    _backup_name: Optional[str] = None
                    if _is_binary:
                        _before_binary: Optional[str] = None
                        _backups_dir_mod: Optional[Path] = None
                        _before_binary = _before_content_snapshot.get(_path_str)
                        if _before_binary is None and _repo_root is not None:
                            _before_binary = _git_content_before(_repo_root, _path_str)
                        # Normalize line endings for comparison — git may
                        # convert CRLF↔LF during stash/apply.
                        if _before_binary is not None and _before_binary.replace("\r\n", "\n").replace("\r", "\n") == _content.replace("\r\n", "\n").replace("\r", "\n"):
                            continue
                        if _before_binary is not None:
                            try:
                                __chat_mgr = getattr(agent, "_chat_state_manager", None)
                                __chat_id = str(getattr(agent, "active_chat_id", "") or "")
                                if __chat_mgr is not None and __chat_id:
                                    _backups_dir_mod = __chat_mgr.chat_backups_dir_for_chat(__chat_id)
                            except Exception:
                                pass
                        if _backups_dir_mod is not None:
                            _backup_name = _backup_deleted_file(
                                _before_binary, Path(_path_str), _backups_dir_mod,
                            )
                        _diff_rows: List[Dict[str, Any]] = []
                        if _tracker2 is not None:
                            _tracker2.record_change(
                                file_path=_path_str,
                                change_type="modify",
                                source="shell",
                                content_before=None,
                                content_after=None,
                                patch=None,
                                backup_path=_backup_name,
                            )
                    else:
                        # Get pre-execution content from the before-content
                        # snapshot, falling back to git (index → HEAD).
                        _before_for_diff: Optional[str] = None
                        _before_for_diff = _before_content_snapshot.get(_path_str)
                        if _before_for_diff is None and _repo_root is not None:
                            _before_for_diff = _git_content_before(_repo_root, _path_str)
                        if _before_for_diff is not None:
                            _diff_rows = _build_real_diff_rows(_before_for_diff, _content)
                            _cb = _before_for_diff
                            _log.info("got before content (%d bytes) for %s", len(_before_for_diff), _path_str)
                            if _before_for_diff != _content:
                                _log.info("content changed, first 200 chars before: %r  after: %r",
                                          _before_for_diff[:200], _content[:200])
                        else:
                            _before_for_diff = None
                            _diff_rows = _build_all_add_diff_rows(_content)
                            _cb = ""
                            _log.info("no before content for %s, showing all as added", _path_str)
                        if _cb is not None and _cb.replace("\r\n", "\n").replace("\r", "\n") == _content.replace("\r\n", "\n").replace("\r", "\n"):
                            continue
                        if _tracker2 is not None:
                            _tracker2.record_change(
                                file_path=_path_str,
                                change_type="modify",
                                source="shell",
                                content_before=_cb,
                                content_after=_content,
                                patch=_diff_rows,
                            )
                    _shell_diff_entries.append({
                        "file": _path_str,
                        "changeType": "modify",
                        "diffRows": _diff_rows,
                    })
                    if _backup_name:
                        _shell_diff_entries[-1]["backupPath"] = _backup_name
                # Record deletions that were detected via filesystem diff
                # but NOT captured by the delete-target parser (e.g. files
                # deleted as side effects of a script).  Also skip files that
                # were removed by git stash (e.g. untracked files via
                # --include-untracked) to avoid false deletion reports.
                for _path_str in _ws_deleted:
                    if _path_str in _delete_snapshots:
                        continue
                    _ws_del_before: Optional[str] = None
                    _ws_del_before = _before_content_snapshot.get(_path_str)
                    if _ws_del_before is None and _repo_root is not None:
                        _ws_del_before = _git_content_before(
                            _repo_root, _path_str,
                        )
                    if _ws_del_before is None:
                        _ws_del_before = ""
                    _ws_del_backup: Optional[str] = None
                    if _ws_del_before:
                        try:
                            _chat_mgr_ws = getattr(agent, "_chat_state_manager", None)
                            _chat_id_ws = str(getattr(agent, "active_chat_id", "") or "")
                            if _chat_mgr_ws is not None and _chat_id_ws:
                                _backups_dir_ws = _chat_mgr_ws.chat_backups_dir_for_chat(_chat_id_ws)
                                _ws_del_backup = _backup_deleted_file(
                                    _ws_del_before, Path(_path_str), _backups_dir_ws,
                                )
                        except Exception:
                            pass
                    _ws_del_diff = _build_all_del_diff_rows(_ws_del_before)
                    if _tracker2 is not None:
                        _tracker2.record_delete(
                            file_path=_path_str,
                            source="shell",
                            content_before=_ws_del_before,
                            backup_path=_ws_del_backup,
                        )
                        _tracker2.cancel_create_for_deleted_file(_path_str)
                    try:
                        from ..services.execution_policy_service import (
                            freedom_remove_user_script_review_cache_entry,
                        )
                        freedom_remove_user_script_review_cache_entry(
                            agent, Path(_path_str)
                        )
                    except Exception:
                        pass
                    _ws_del_entry: Dict[str, Any] = {
                        "file": _path_str,
                        "changeType": "delete",
                        "diffRows": _ws_del_diff,
                    }
                    if _ws_del_backup:
                        _ws_del_entry["backupPath"] = _ws_del_backup
                    _shell_diff_entries.append(_ws_del_entry)
                _log.info("shell_diff_entries total: %d, types: %s",
                          len(_shell_diff_entries),
                          ", ".join(e.get("changeType", "?") for e in _shell_diff_entries))
            except Exception:
                _log.exception("shell_diff_entries build failed, clearing")
                _shell_diff_entries = []
            if _shell_diff_entries:
                base_out["_shell_diff_entries"] = _shell_diff_entries
                # Emit GUI diff blocks for live rendering (GUI mode only).
                # In GUI serve mode sys.stdout is the SSE output bridge, so the
                # write below already reaches the frontend. Re-emitting via
                # _gui_tool_output_emit would publish each preview twice and
                # duplicate the diff blocks in the expanded tool call.
                try:
                    _is_gui = bool(getattr(agent, "_gui_no_wrap", False))
                    if _is_gui:
                        for _entry in _shell_diff_entries:
                            _payload = json.dumps(_entry, ensure_ascii=False)
                            sys.stdout.write(f"{GUI_DIFF_BEGIN}{_payload}{GUI_DIFF_END}")
                            sys.stdout.flush()
                except Exception:
                    pass

        finally:
            _stop_status_ticker()
            if merge_path:
                try:
                    os.unlink(merge_path)
                except OSError:
                    pass

        if timed_out:
            return {
                "success": False,
                "error": rg_error or (
                    "Command was auto-terminated: it produced no output for "
                    f"{int(_SHELL_INTERACTIVE_IDLE_TIMEOUT)}s and was treated as "
                    "an interactive prompt waiting for input. Supply the input "
                    "non-interactively (flags, a script, piped stdin) or use the "
                    "interactive console if a human must operate it."
                ),
                **base_out,
            }
        if return_code == 0:
            register_outputs_from_shell_command(agent, command)
            if agent._is_workspace_skill_path(execution_cwd):
                agent._reload_skills_if_workspace_skill_changed([execution_cwd])
            removed = try_remove_ephemeral_script_after_shell(agent, command)
            if removed:
                agent._last_auto_removed_ephemeral = removed
                return {
                    "success": True,
                    "message": (
                        f"Command executed successfully; temporary script '«{removed}»' was auto-deleted."
                        " Please do not run delete on this file again."
                    ),
                    "auto_removed_ephemeral_script": removed,
                    **base_out,
                }
            if interactive:
                return {"success": True, "message": "Command executed successfully (interactive mode)", **base_out}
            return {"success": True, "message": "Command executed successfully", **base_out}

        combo = str(out)
        cmd_l = command.lower()
        is_skillhub_install = ("skillhub_installer.py" in cmd_l) and (" install " in f" {cmd_l} ")
        user_cancelled = ("installation aborted by user." in combo.lower()) or (return_code == 2)
        if is_skillhub_install and user_cancelled:
            return {
                "success": True,
                "cancelled": True,
                "terminal_state": "user_cancelled",
                "message": "Installation was cancelled by the user. Flow ended (should not auto-retry).",
                **base_out,
            }
        no_match_message = _classify_no_match_exit(command, return_code, combo)
        if no_match_message:
            register_outputs_from_shell_command(agent, command)
            return {
                "success": True,
                "no_matches": True,
                "message": no_match_message,
                **base_out,
            }
        if aborted_by_user:
            return {
                "success": False,
                "error": "Command aborted by user",
                **base_out,
            }
        # Include the command's own output in the error so the model can see
        # why it failed (e.g. an "Access is denied" from the sandbox) instead
        # of only an opaque exit code.
        _sb_fail = _sandbox_failure_analysis(
            command, return_code, str(out or ""), sandbox_plan
        )
        _failure_tail = str(_shell_rendered or "").strip()
        if _failure_tail and not rg_error:
            _failure_tail = _failure_tail[-1200:]
            _err_msg = (
                f"Command execution failed, exit code: {return_code}. "
                f"Output:\n{_failure_tail}"
            )
            if _sb_fail:
                _err_msg += (
                    "\n\n" + str(_sb_fail.get("sandbox_escalation_hint") or "")
                )
            return {
                "success": False,
                "error": _err_msg,
                **(_sb_fail or {}),
                **base_out,
            }
        _err_msg = rg_error or f"Command execution failed, exit code: {return_code}"
        if _sb_fail:
            _err_msg += (
                "\n\n" + str(_sb_fail.get("sandbox_escalation_hint") or "")
            )
        return {
            "success": False,
            "error": _err_msg,
            **(_sb_fail or {}),
            **base_out,
        }

    except Exception as e:
        return {"success": False, "error": f"System command execution error: {str(e)}"}
    finally:
        _reset_work_directory_to_startup_initial(agent)
        restore_app_console_title()


def action_project_context_search(agent: Any, params: Dict[str, Any]) -> dict:
    if not agent._project_context_tool_allowed():
        if agent._is_default_workspace():
            return {
                "success": False,
                "error": "project_context_search is not supported in the Default workspace. Please switch to a non-Default workspace and try again.",
            }
        return {
            "success": False,
            "error": "project_context_search has been disabled via configuration (project_context_search_enabled is false). Enable it in settings to use this tool.",
        }

    query = str(params.get("query") or "").strip()
    max_files = params.get("max_files", 12)
    refresh = params.get("refresh", None)
    refresh_async = bool(params.get("refresh_async", False))
    status_only = bool(params.get("status_only", False))
    force_rebuild = bool(params.get("force_rebuild", False))
    call_graph_symbol = str(params.get("call_graph") or "").strip()
    call_graph_direction = str(params.get("call_graph_direction") or "both").strip()

    try:
        max_files_i = int(max_files)
    except Exception:
        max_files_i = 12
    if max_files_i <= 0:
        max_files_i = 12
    if max_files_i > 50:
        max_files_i = 50

    agent._bind_project_index_workspace()
    if status_only:
        st = agent._project_context_index.status()
        st["message"] = "Project context index status"
        return st
    if call_graph_symbol:
        if force_rebuild:
            idx_res = agent._project_context_index.refresh_index(force=True)
            if not idx_res.get("success", False):
                return idx_res
        return agent._project_context_index.call_graph(
            symbol=call_graph_symbol,
            direction=call_graph_direction,
            max_results=max_files_i,
            auto_refresh=(
                ((True if refresh is None else bool(refresh)) or force_rebuild)
                and (not refresh_async)
            ),
            refresh_timeout_ms=(None if len(getattr(agent._project_context_index, "files", {})) == 0 else 10000),
        )
    if not query:
        return {"success": False, "error": "Missing required parameter: query for project_context_search"}

    if force_rebuild:
        idx_res = agent._project_context_index.refresh_index(force=True)
        if not idx_res.get("success", False):
            return idx_res
    elif refresh_async:
        agent._schedule_project_context_refresh_background(force=False, reason="project-context-search")

    result = agent._project_context_index.search(
        query=query,
        max_files=max_files_i,
        auto_refresh=(
            ((True if refresh is None else bool(refresh)) or force_rebuild) and (not refresh_async)
        ),
        refresh_timeout_ms=(None if len(getattr(agent._project_context_index, "files", {})) == 0 else 10000),
    )
    if refresh_async:
        result["refresh_scheduled"] = True
    return result


def register_outputs_from_shell_command(agent: Any, command: str) -> None:
    for pat in (
        r"to_excel\s*\(\s*['\"]([^'\"]+)['\"]",
        r"to_csv\s*\(\s*['\"]([^'\"]+)['\"]",
        r"ExcelWriter\s*\(\s*['\"]([^'\"]+)['\"]",
    ):
        for m in re.finditer(pat, command, re.I):
            agent._try_register_ai_output_literal(m.group(1))


_SCRIPT_SUFFIXES = (
    ".py",
    ".ps1",
    ".bat",
    ".cmd",
    ".sh",
    ".bash",
    ".zsh",
    ".ksh",
    ".fish",
    ".vbs",
    ".js",
    ".jse",
    ".wsf",
    ".rb",
    ".pl",
    ".php",
    ".lua",
    ".r",
    ".psm1",
)

_SCRIPT_INTERPRETER_EXES = {
    "python",
    "pythonw",
    "py",
    "node",
    "nodejs",
    "ruby",
    "perl",
    "php",
    "lua",
    "rscript",
    "cscript",
    "wscript",
    "bash",
    "sh",
    "zsh",
    "ksh",
    "dash",
    "fish",
    "pwsh",
    "powershell",
    "deno",
}

_OPTION_VALUE_FLAGS_BY_EXE = {
    "python": {"-x"},
    "pythonw": {"-x"},
    "py": set(),
    "node": set(),
    "nodejs": set(),
    "ruby": set(),
    "perl": set(),
    "php": {"-f"},
    "lua": set(),
    "rscript": {"-e"},
    "cscript": set(),
    "wscript": set(),
    "bash": set(),
    "sh": set(),
    "zsh": set(),
    "ksh": set(),
    "dash": set(),
    "fish": set(),
    "pwsh": {"-file", "-f"},
    "powershell": {"-file", "-f", "/f"},
}

_INLINE_EXEC_FLAGS_BY_EXE = {
    "python": {"-c", "-m"},
    "pythonw": {"-c", "-m"},
    "py": {"-c", "-m"},
    "node": {"-e", "-p"},
    "nodejs": {"-e", "-p"},
    "ruby": {"-e"},
    "perl": {"-e", "-m"},
    "php": {"-r"},
    "lua": {"-e"},
    "rscript": {"-e"},
    "pwsh": {"-command", "-c", "/c", "-encodedcommand", "-enc", "-e"},
    "powershell": {"-command", "-c", "/c", "-encodedcommand", "-enc", "-e"},
}


def _split_shell_like(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _token_exe_base(token: str) -> str:
    base = token.replace("\\", "/").split("/")[-1].lower()
    if base.endswith(".exe"):
        return base[:-4]
    return base


def _strip_wrapping_quotes(token: str) -> str:
    t = str(token or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    return t


def _find_option_value(parts: List[str], names: tuple[str, ...]) -> Optional[str]:
    wanted = {n.lower() for n in names}
    for i in range(1, len(parts) - 1):
        if parts[i].lower() in wanted:
            return parts[i + 1]
    return None


def _unwrap_windows_powershell_command(command: str) -> str:
    s = str(command or "").strip()
    if not s:
        return s
    m = re.match(
        r"(?is)^powershell(?:\.exe)?\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(.+)$",
        s,
    )
    if not m:
        m = re.match(
            r"(?is)^(?:powershell|pwsh)(?:\.exe)?\s+.*?(?:-Command|-C|/C)\s+(.+)$",
            s,
        )
        if not m:
            return s
    payload = m.group(1).strip()
    if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in ("'", '"'):
        quote = payload[0]
        payload = payload[1:-1]
        if quote == '"':
            payload = payload.replace('`"', '"')
        else:
            payload = payload.replace("''", "'")
    payload = payload.replace('\\"', '"')
    return payload.strip()


def _unwrap_shell_command_layers(command: str, max_depth: int = 8) -> str:
    s = str(command or "").strip()
    if not s:
        return s
    for _ in range(max(1, max_depth)):
        changed = False
        if s.lower().startswith("call "):
            s = s[5:].strip()
            changed = True
        ps_unwrapped = _unwrap_windows_powershell_command(s)
        if ps_unwrapped != s:
            s = ps_unwrapped
            changed = True
        parts = _split_shell_like(s)
        if not parts:
            return s
        exe = _token_exe_base(parts[0])
        if len(parts) >= 3 and exe == "cmd" and parts[1].lower() in ("/c", "/k"):
            s = " ".join(parts[2:]).strip()
            changed = True
        elif exe in ("bash", "sh", "zsh", "ksh", "dash", "fish"):
            payload = _find_option_value(parts, ("-c", "-lc"))
            if payload is not None:
                s = _strip_wrapping_quotes(payload).strip()
                changed = True
        elif exe == "env":
            payload = _find_option_value(parts, ("-s", "--split-string"))
            if payload is not None:
                s = _strip_wrapping_quotes(payload).strip()
                changed = True
            else:
                env_opts_need_value = {"-u", "--unset", "-c", "--chdir", "-p", "--path"}
                i = 1
                if i < len(parts) and parts[i] == "--":
                    i += 1
                while i < len(parts):
                    t = parts[i]
                    if t == "--":
                        i += 1
                        break
                    if t.startswith("-"):
                        if t.lower() in env_opts_need_value and i + 1 < len(parts):
                            i += 2
                            continue
                        if t.lower().startswith("--unset="):
                            i += 1
                            continue
                        i += 1
                        continue
                    if "=" in t and not t.startswith(("/", "\\", ".", "-")):
                        i += 1
                        continue
                    break
                if i > 1 and i < len(parts):
                    s = " ".join(parts[i:]).strip()
                    changed = True
        elif exe in ("sudo", "doas", "nohup", "setsid"):
            sudo_opts_need_value = {
                "-u",
                "-g",
                "-h",
                "-p",
                "-r",
                "-t",
                "-c",
                "--user",
                "--group",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--chdir",
                "--close-from",
            }
            doas_opts_need_value = {"-u", "-c"}
            i = 1
            while i < len(parts):
                t = parts[i]
                if t == "--":
                    i += 1
                    break
                if t.startswith("-"):
                    tl = t.lower()
                    if (
                        (exe == "sudo" and tl in sudo_opts_need_value)
                        or (exe == "doas" and tl in doas_opts_need_value)
                    ) and i + 1 < len(parts):
                        i += 2
                        continue
                    i += 1
                    continue
                break
            if i > 1 and i < len(parts):
                s = " ".join(parts[i:]).strip()
                changed = True
        if not changed:
            break
    return s


# Tools that document a non-zero exit code as "ran successfully but found
# no matches". For these, exit code 1 with empty stdout is a normal
# outcome, not a hard failure. We surface this as ``success=True`` with
# an explanatory message so the model doesn't think the tool itself
# broke and switch to a worse alternative.
_NO_MATCH_FRIENDLY_TOOLS: frozenset[str] = frozenset({
    "rg",
    "ripgrep",
    "ag",          # the_silver_searcher
    "ack",
    "grep",
    "egrep",
    "fgrep",
    "findstr",
})


def _shell_command_has_compound_operator(command: str) -> bool:
    """Return True when the command contains a pipe / boolean / sequencer.

    These operators (``|``, ``||``, ``&&``, ``;``, ``&``) make the final
    exit code reflect more than just the head tool's behavior, so the
    no-match-friendly relaxation must not apply.
    """
    s = str(command or "")
    in_single = False
    in_double = False
    backtick = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double and not backtick:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and not backtick:
            in_double = not in_double
            i += 1
            continue
        if ch == "`" and not in_single and not in_double:
            backtick = not backtick
            i += 1
            continue
        if not (in_single or in_double or backtick):
            if ch in ("|", ";"):
                return True
            if ch == "&":
                # Ignore the trailing ``&`` form only when we hit a single
                # ``&`` at end of string (rare); both ``&`` and ``&&``
                # affect exit-code semantics so we treat any unquoted
                # ``&`` as compound.
                return True
        i += 1
    return False


def _extract_last_and_segment(command: str) -> Optional[str]:
    """Extract the rightmost ``&&``-separated segment of a shell command.

    Returns ``None`` when the command contains compound operators other
    than ``&&`` (pipe, ``||``, ``;``, standalone ``&``), since those can
    change the exit code in ways that make the per-tool classification
    unreliable.  For a plain ``&&`` chain like ``cd /d dir && rg ...``,
    the trailing segment's exit code IS the final exit code.
    """
    s = str(command or "").strip()
    if not s:
        return None
    # Bail out if any non-&& compound operator is present.
    in_single = False
    in_double = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not (in_single or in_double):
            if ch in ("|", ";"):
                return None
            if ch == "&":
                # Two && in a row is ok (the operator we split on).
                if i + 1 < n and s[i + 1] == "&":
                    i += 2
                    continue
                # Standalone & is not ok.
                return None
        i += 1
    # Split on && tokens (outside quotes) and return the last segment.
    segments: list[str] = []
    in_single = False
    in_double = False
    start = 0
    i = 0
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not (in_single or in_double) and ch == "&" and i + 1 < n and s[i + 1] == "&":
            segments.append(s[start:i].strip())
            start = i + 2
            i += 2
            continue
        i += 1
    segments.append(s[start:].strip())
    segments = [seg for seg in segments if seg]
    if not segments:
        return None
    return segments[-1]


def _classify_no_match_exit(
    command: str,
    return_code: int,
    stdout_text: str,
) -> Optional[str]:
    """Classify ``return_code == 1`` from a single search-tool invocation.

    Returns a localized explanation string when the command should be
    treated as a successful "no matches found" result; returns ``None``
    when the original failure classification must stand.
    """
    if int(return_code) != 1:
        return None
    if str(stdout_text or "").strip():
        return None
    inner = _unwrap_shell_command_layers(command)
    if not inner.strip():
        return None
    # If the command chains with ``&&`` (e.g. ``cd /d dir && rg ...``),
    # extract the trailing segment so the classification still applies
    # when the chain is just a directory change prefix.
    if _shell_command_has_compound_operator(inner):
        segment = _extract_last_and_segment(inner)
        if not segment:
            return None
    else:
        segment = inner
    parts = _split_shell_like(segment)
    if not parts:
        return None
    base = _token_exe_base(_strip_wrapping_quotes(parts[0]))
    if base not in _NO_MATCH_FRIENDLY_TOOLS:
        return None
    return (
        f"`{base}` exited with code 1 because it found no matches. "
        "The tool ran successfully; this is the documented \"no matches\" exit code, "
        "not a real failure. Adjust the query or pattern if matches were expected."
    )


def parse_shell_invoked_script_path(agent: Any, command: str) -> Optional[Path]:
    s = _unwrap_shell_command_layers(command.strip())
    if not s:
        return None
    parts = _split_shell_like(s)
    if not parts:
        return None
    exe = _token_exe_base(parts[0])

    def _resolve_script_token(tok_raw: str) -> Optional[Path]:
        tok = _strip_wrapping_quotes(tok_raw).strip()
        if not tok:
            return None
        if tok.startswith(".\\") or tok.startswith("./"):
            tok = tok[2:]
        p = Path(tok)
        if not p.is_absolute():
            p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
            if p_wd.is_file():
                return p_wd
            if p_temp.is_file():
                return p_temp
            if p_ws.is_file():
                return p_ws
            return p_wd
        try:
            return p.resolve()
        except OSError:
            return p

    if exe in _SCRIPT_INTERPRETER_EXES and len(parts) >= 2:
        if exe == "deno":
            # deno run <script>
            if len(parts) >= 3 and parts[1].lower() == "run":
                i = 2
                while i < len(parts):
                    t = _strip_wrapping_quotes(parts[i]).lower()
                    if t == "--":
                        i += 1
                        break
                    if t.startswith("-"):
                        i += 1
                        continue
                    break
                if i >= len(parts):
                    return None
                return _resolve_script_token(parts[i])
            return None

        inline_flags = _INLINE_EXEC_FLAGS_BY_EXE.get(exe, set())
        value_flags = _OPTION_VALUE_FLAGS_BY_EXE.get(exe, set())
        i = 1
        while i < len(parts):
            t = _strip_wrapping_quotes(parts[i])
            tl = t.lower()
            if tl == "--":
                i += 1
                break
            if tl in inline_flags:
                return None
            if tl in value_flags:
                if tl in ("-file", "-f", "/f"):
                    if i + 1 >= len(parts):
                        return None
                    return _resolve_script_token(parts[i + 1])
                i += 2
                continue
            if t.startswith("-") or t.startswith("/"):
                i += 1
                continue
            break
        if i >= len(parts):
            return None
        return _resolve_script_token(parts[i])

    tok = _strip_wrapping_quotes(parts[0])
    if tok.lower().endswith(_SCRIPT_SUFFIXES):
        return _resolve_script_token(tok)
    return None


def rewrite_shell_command_script_arg_to_abs(agent: Any, command: str, resolved: Path) -> str:
    import subprocess

    s = str(command or "").strip()
    if not s:
        return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    base0 = _token_exe_base(parts[0])
    if base0 in ("powershell", "pwsh"):
        payload = _find_option_value(parts, ("-command", "-c", "/c"))
        if payload is not None:
            inner_re = rewrite_shell_command_script_arg_to_abs(agent, payload, resolved)
            if inner_re == payload:
                return command
            new_parts = list(parts)
            for i in range(1, len(new_parts) - 1):
                if new_parts[i].lower() in ("-command", "-c", "/c"):
                    new_parts[i + 1] = inner_re
                    break
            if os.name == "nt":
                return call_prefix + subprocess.list2cmdline(new_parts)
            return call_prefix + shlex.join(new_parts)
    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        inner = " ".join(parts[2:])
        inner_re = rewrite_shell_command_script_arg_to_abs(agent, inner, resolved)
        if inner_re == inner:
            return command
        if os.name == "nt":
            return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_re])
        return f"{call_prefix}{parts[0]} {parts[1]} {inner_re}"

    exe = base0
    if exe not in ("python", "pythonw", "py"):
        return command
    i = 1
    while i < len(parts):
        t = parts[i].strip('"').strip("'")
        if t in ("-m", "-c"):
            return command
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        break
    if i >= len(parts):
        return command
    tok = parts[i].strip('"').strip("'")
    if tok.startswith(".\\") or tok.startswith("./"):
        tok = tok[2:]
    p = Path(tok)
    if not p.is_absolute():
        p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
        if p_wd.is_file():
            cand = p_wd
        elif p_temp.is_file():
            cand = p_temp
        elif p_ws.is_file():
            cand = p_ws
        else:
            cand = p_wd
    else:
        try:
            cand = Path(tok).resolve()
        except OSError:
            return command
    if agent._ephemeral_path_key(cand) != agent._ephemeral_path_key(resolved):
        return command
    parts[i] = str(resolved.resolve())
    if os.name == "nt":
        return call_prefix + subprocess.list2cmdline(parts)
    return call_prefix + shlex.join(parts)


def ensure_absolute_script_for_shell_cwd(agent: Any, command: str) -> str:
    invoked = parse_shell_invoked_script_path(agent, command)
    if invoked is None or not invoked.is_file():
        return command
    try:
        invoked.resolve().relative_to(agent.workspace_config_dir.resolve())
    except ValueError:
        return command
    new_cmd = rewrite_shell_command_script_arg_to_abs(agent, command, invoked.resolve())
    return new_cmd


def _workspace_rg_executable_path(agent: Any) -> Optional[Path]:
    roots: List[Path] = []
    raw_repo_root = getattr(agent, "_self_repo_root", None)
    if raw_repo_root:
        try:
            roots.append(Path(str(raw_repo_root)).expanduser().resolve())
        except Exception:
            pass
    try:
        roots.append(Path(__file__).resolve().parents[2])
    except Exception:
        pass

    dedup: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold() if os.name == "nt" else str(root)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(root)

    names = ("rg.exe", "rg.cmd", "rg.bat", "rg") if os.name == "nt" else ("rg",)
    for root in dedup:
        bin_dir = root / "bin"
        for name in names:
            candidate = bin_dir / name
            try:
                if not candidate.is_file():
                    continue
                if os.name != "nt" and not os.access(str(candidate), os.X_OK):
                    continue
                return candidate.resolve()
            except Exception:
                continue
    return None


def _rewrite_shell_command_head_executable(
    command: str,
    *,
    target_exe_bases: set[str],
    replacement: str,
) -> str:
    import subprocess

    s = str(command or "").strip()
    if not s:
        return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    if len(parts) == 1:
        unwrapped_single = _strip_wrapping_quotes(parts[0])
        if unwrapped_single and unwrapped_single != parts[0]:
            reparsed = _split_shell_like(unwrapped_single)
            if len(reparsed) > 1:
                s = unwrapped_single
                parts = reparsed
    base0 = _token_exe_base(_strip_wrapping_quotes(parts[0]))

    if base0 in ("powershell", "pwsh"):
        payload = _find_option_value(parts, ("-command", "-c", "/c"))
        if payload is not None:
            inner_re = _rewrite_shell_command_head_executable(
                payload,
                target_exe_bases=target_exe_bases,
                replacement=replacement,
            )
            if inner_re != payload:
                new_parts = list(parts)
                for i in range(1, len(new_parts) - 1):
                    if new_parts[i].lower() in ("-command", "-c", "/c"):
                        new_parts[i + 1] = inner_re
                        break
                if os.name == "nt":
                    return call_prefix + subprocess.list2cmdline(new_parts)
                return call_prefix + shlex.join(new_parts)

    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        inner = " ".join(parts[2:])
        inner_re = _rewrite_shell_command_head_executable(
            inner,
            target_exe_bases=target_exe_bases,
            replacement=replacement,
        )
        if inner_re != inner:
            if os.name == "nt":
                return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_re])
            return f"{call_prefix}{parts[0]} {parts[1]} {inner_re}"

    if base0 not in target_exe_bases:
        return command
    parts[0] = replacement
    if os.name == "nt":
        return call_prefix + subprocess.list2cmdline(parts)
    return call_prefix + shlex.join(parts)


# git subcommands that may spawn the interactive pager (less / more) for
# long output.  Rewriting them with ``--no-pager`` keeps non-interactive
# shell execution from blocking on a pager prompt and returns the full output.
_GIT_PAGER_SUBCOMMANDS: Set[str] = {
    "show", "diff", "log", "grep", "blame", "annotate", "whatchanged",
    "shortlog", "reflog", "stash", "branch", "tag", "remote", "help",
    "notes", "fsck", "instaweb", "lfs",
}

# git global options that consume a separate value token (skip over the
# value when locating the subcommand).
_GIT_GLOBAL_OPT_WITH_VALUE: Set[str] = {
    "-c", "-C", "--git-dir", "--work-tree", "--exec-path", "--namespace",
    "--shallow-file", "--super-prefix", "--config-env", "--object-format",
}


def _git_subcommand_index(parts: List[str]) -> Optional[int]:
    """Index of the first git subcommand token, skipping global options."""
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok == "--":
            return i + 1 if i + 1 < len(parts) else None
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if name in _GIT_GLOBAL_OPT_WITH_VALUE and "=" not in tok:
                i += 2
            else:
                i += 1
            continue
        return i
    return None


def _enforce_git_no_pager_for_shell_command(command: str) -> str:
    """Rewrite ``git <subcommand>`` commands that may spawn a pager into
    ``git --no-pager <subcommand>`` so non-interactive shell execution
    returns the full output instead of blocking on an interactive pager."""
    import subprocess

    s = str(command or "").strip()
    if not s:
        return command
    call_prefix = ""
    if s.lower().startswith("call "):
        call_prefix = "call "
        s = s[5:].strip()
    parts = _split_shell_like(s)
    if not parts:
        return command
    if len(parts) == 1:
        unwrapped_single = _strip_wrapping_quotes(parts[0])
        if unwrapped_single and unwrapped_single != parts[0]:
            reparsed = _split_shell_like(unwrapped_single)
            if len(reparsed) > 1:
                s = unwrapped_single
                parts = reparsed
    base0 = _token_exe_base(_strip_wrapping_quotes(parts[0]))

    if base0 in ("powershell", "pwsh"):
        payload = _find_option_value(parts, ("-command", "-c", "/c"))
        if payload is not None:
            inner_re = _enforce_git_no_pager_for_shell_command(payload)
            if inner_re != payload:
                new_parts = list(parts)
                for i in range(1, len(new_parts) - 1):
                    if new_parts[i].lower() in ("-command", "-c", "/c"):
                        new_parts[i + 1] = inner_re
                        break
                if os.name == "nt":
                    return call_prefix + subprocess.list2cmdline(new_parts)
                return call_prefix + shlex.join(new_parts)

    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        inner = " ".join(parts[2:])
        inner_re = _enforce_git_no_pager_for_shell_command(inner)
        if inner_re != inner:
            if os.name == "nt":
                return call_prefix + subprocess.list2cmdline([parts[0], parts[1], inner_re])
            return f"{call_prefix}{parts[0]} {parts[1]} {inner_re}"

    if base0 != "git":
        return command
    sub_idx = _git_subcommand_index(parts)
    if sub_idx is None:
        return command
    sub = _strip_wrapping_quotes(parts[sub_idx]).lower()
    if sub not in _GIT_PAGER_SUBCOMMANDS:
        return command
    for tok in parts[1:sub_idx]:
        if _strip_wrapping_quotes(tok).lower() == "--no-pager":
            return command
    new_parts = [parts[0], "--no-pager", *parts[1:]]
    if os.name == "nt":
        return call_prefix + subprocess.list2cmdline(new_parts)
    return call_prefix + shlex.join(new_parts)


def enforce_workspace_rg_for_shell_command(agent: Any, command: str) -> str:
    rg_path = _workspace_rg_executable_path(agent)
    if rg_path is None:
        return command
    return _rewrite_shell_command_head_executable(
        command,
        target_exe_bases={"rg"},
        replacement=str(rg_path),
    )


_RG_STDERR_SUPPRESS_RE = re.compile(r"\s*2>\s*(?:nul|/dev/null)\s*", re.IGNORECASE)


def _is_rg_command(command: str) -> bool:
    parts = str(command or "").strip().split(None, 1)
    if not parts:
        return False
    first_token = parts[0].strip('"').strip("'")
    exe_name = Path(first_token).name.lower()
    return exe_name in ("rg", "rg.exe")


def _rg_stderr_retry(
    command: str,
    out: str,
    return_code: int,
    execution_cwd: Path,
    run_env,
) -> Optional[str]:
    if not _is_rg_command(command):
        return None
    if not _RG_STDERR_SUPPRESS_RE.search(command):
        return None
    is_failure = return_code != 0 or not str(out or "").strip()
    if not is_failure:
        _rg_stderr_retry._cache[command] = None
        return None
    cache_key = command
    cached = _rg_stderr_retry._cache.get(cache_key)
    if cached is not None:
        return cached if cached else None
    stripped = _RG_STDERR_SUPPRESS_RE.sub(" ", command).strip()
    if not stripped or stripped == command:
        _rg_stderr_retry._cache[cache_key] = None
        return None
    try:
        import subprocess as _rg_retry_subprocess
        proc = _rg_retry_subprocess.run(
            stripped,
            shell=True,
            cwd=str(execution_cwd.resolve()),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        err = str(proc.stderr or "").strip()
        if err:
            _rg_stderr_retry._cache[cache_key] = err
            return err
        out2 = str(proc.stdout or "").strip()
        if out2:
            _rg_stderr_retry._cache[cache_key] = out2
            return out2
    except Exception:
        pass
    _rg_stderr_retry._cache[cache_key] = None
    return None


_rg_stderr_retry._cache: Dict[str, Optional[str]] = {}

_FILE_READ_COMMAND_RE = re.compile(
    r"^(type|cat|head|tail|more|less|gc|get-content)(\.exe)?\s",
    re.IGNORECASE,
)


def is_file_read_shell_command(command: str) -> bool:
    """Return True when *command* is a file-content-reading shell command
    (cat, type, head, tail, more, less, gc, get-content) that the model
    should be advised to replace with the native ``read`` tool."""
    raw = str(command or "").strip()
    if not raw:
        return False
    if _FILE_READ_COMMAND_RE.search(raw):
        return True
    unwrapped = _unwrap_shell_command_layers(raw)
    if unwrapped and unwrapped != raw:
        if _FILE_READ_COMMAND_RE.search(unwrapped):
            return True
    return False


_CD_AND_DELIMITERS: list[tuple[str, int]] = [
    (pattern, len(pattern))
    for pattern in (
        "cd /d ",
        "cd /D ",
        "cd ",
        "pushd ",
    )
]
_CD_AND_DELIMITERS.sort(key=lambda x: -x[1])  # longest match first


def strip_redundant_cd_prefix(agent: Any, command: str) -> str:
    """Strip a leading ``cd <path> &&`` when <path> matches the shell cwd.

    The shell tool already sets the working directory to the workspace root.
    A ``cd /d workspace-root && actual-command`` prefix is therefore redundant
    and only adds visual noise in the GUI / TUI command summary.  Stripping it
    also lets ``_classify_no_match_exit`` recognise the trailing search tool
    when the model writes ``cd … && rg …``.
    """
    s = str(command or "")
    for cd_token, _cd_len in _CD_AND_DELIMITERS:
        if not s.startswith(cd_token):
            continue
        rest = s[len(cd_token):]
        # Find the path: everything up to the next ``&&`` (outside quotes).
        path_str, after_path = _split_cd_prefix_path_and_tail(rest)
        if path_str is None or after_path is None:
            continue
        if not after_path.lstrip().startswith("&&"):
            continue
        # Normalise both paths for comparison.
        try:
            target = Path(str(path_str).strip().strip('"').strip("'"))
            cwd = _resolve_shell_execution_cwd(agent)
            if not target.is_absolute():
                # Relative cd — resolve against the shell cwd.
                target = (cwd / target).resolve()
            else:
                target = target.resolve()
            cwd = cwd.resolve()
        except Exception:
            return command
        if _paths_equal(target, cwd):
            return after_path.lstrip()[2:].lstrip()  # skip ``&&``
        return command
    return command


def _split_cd_prefix_path_and_tail(s: str) -> tuple[Optional[str], Optional[str]]:
    """Extract the path from a ``cd <path> && ...`` prefix, respecting quotes."""
    s = str(s or "")
    in_quote = ""
    for i, ch in enumerate(s):
        if ch == "\\" and i + 1 < len(s):
            # skip next char
            continue
        if ch in ('"', "'"):
            if in_quote == ch:
                in_quote = ""
            elif not in_quote:
                in_quote = ch
            continue
        if not in_quote and ch == "&" and i + 1 < len(s) and s[i + 1] == "&":
            return s[:i], s[i:]
    return None, None


def _paths_equal(a: Path, b: Path) -> bool:
    if os.name == "nt":
        try:
            return a.resolve().as_posix().casefold() == b.resolve().as_posix().casefold()
        except Exception:
            pass
    return a.resolve() == b.resolve()


def normalize_shell_command_for_summary(command: str) -> str:
    """Normalize command string for concise tool-call summary display."""
    return _rewrite_shell_command_head_executable(
        command,
        target_exe_bases={"rg"},
        replacement="rg",
    )


def tune_7z_output_for_piped_terminal(command: str, agent: Any = None) -> str:
    if not command.strip():
        return command
    if not re.search(r'(^|[\\/\s"])7z(?:\.exe)?(?=\s|"|$)', command, re.IGNORECASE):
        return command
    tuned = command
    appended: List[str] = []
    lower = command.lower()
    if " -bsp" not in lower:
        tuned += " -bsp1"
        appended.append("-bsp1")
    if " -bb" not in lower:
        tuned += " -bb1"
        appended.append("-bb1")
    if " -bso" not in lower:
        tuned += " -bso1"
        appended.append("-bso1")
    if " -bse" not in lower:
        tuned += " -bse2"
        appended.append("-bse2")
    if appended:
        lang = getattr(agent, "display_language", None) or "en"
        print(translate("info.seven_z_compat_flags", lang, flags=" ".join(appended)))
    return tuned


def parse_shell_invoked_executable(agent: Any, command: str) -> Optional[Path]:
    s = _unwrap_shell_command_layers(command.strip())
    if not s:
        return None
    parts = _split_shell_like(s)
    if not parts:
        return None
    token = _strip_wrapping_quotes(parts[0])
    if not token:
        return None
    p = Path(token)
    if not p.is_absolute():
        if any(sep in token for sep in ("/", "\\")) or token.startswith("."):
            p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(p)
            if p_wd.is_file():
                return p_wd
            if p_temp.is_file():
                return p_temp
            if p_ws.is_file():
                return p_ws
            return p_wd
        return None
    try:
        return p.resolve()
    except OSError:
        return p


def is_dependency_install_command(command: str) -> bool:
    s = (command or "").strip().lower()
    if not s:
        return False
    install_patterns = [
        r"^(python(\d+(\.\d+)*)?\s+-m\s+pip)\s+install\b",
        r"^(pip(\d+(\.\d+)*)?)\s+install\b",
        r"^uv\s+pip\s+install\b",
        r"^poetry\s+add\b",
        r"^pipenv\s+install\b",
        r"^conda\s+install\b",
        r"^mamba\s+install\b",
        r"^npm\s+install\b",
        r"^pnpm\s+add\b",
        r"^yarn\s+add\b",
        r"^bun\s+add\b",
    ]
    return any(re.match(pat, s) for pat in install_patterns)


def is_ai_workspace_script_command(agent: Any, command: str) -> bool:
    invoked = parse_shell_invoked_script_path(agent, command or "")
    if invoked is None:
        return False
    return agent._is_path_under(invoked, agent.workspace_config_dir)


def try_remove_ephemeral_script_after_shell(agent: Any, command: str) -> Optional[str]:
    invoked = parse_shell_invoked_script_path(agent, command)
    if invoked is None:
        return None
    key = agent._ephemeral_path_key(invoked)
    if key not in agent._ephemeral_script_paths:
        return None
    lang = getattr(agent, "display_language", None) or "en"
    try:
        if invoked.is_file():
            name = invoked.name
            invoked.unlink()
            agent._ephemeral_script_paths.discard(key)
            agent._ai_created_path_keys.discard(key)
            print(translate("info.temp_script_auto_deleted", lang, name=name))
            return name
    except OSError as e:
        print(translate("warning.temp_script_delete_failed", lang, path=invoked, error=e))
    return None


def resolve_model_context_file_env(agent: Any, command: str) -> Optional[str]:
    invoked = parse_shell_invoked_script_path(agent, command or "")
    if invoked is None:
        return None
    try:
        ip = invoked.resolve()
    except OSError:
        ip = Path(invoked)
    best_len = -1
    best_env: Optional[str] = None
    for s in agent.skills or []:
        env = getattr(s, "model_context_file_env", None)
        if not env:
            continue
        try:
            root = Path(s.bundle_root).resolve()
            ip.relative_to(root)
        except (ValueError, OSError):
            continue
        ln = len(str(root))
        if ln > best_len:
            best_len = ln
            best_env = env
    return best_env


def append_shell_merge_output_path(stdout_text: str, return_code: int, merge_path: Optional[str]) -> str:
    if return_code != 0 or not merge_path:
        return stdout_text
    path = Path(merge_path)
    if not path.is_file():
        return stdout_text
    marker = "[Additional output (shell merge file)]"
    if marker in (stdout_text or ""):
        return stdout_text
    try:
        extra = path.read_text(encoding="utf-8")
    except OSError:
        return stdout_text
    if not extra.strip():
        return stdout_text
    head = (stdout_text or "").strip()
    if not head:
        return marker + "\n" + extra
    return head + "\n\n---\n" + marker + "\n" + extra


# ---------------------------------------------------------------------------
# File-deletion detection helpers
# ---------------------------------------------------------------------------

# Split a command line into segments at shell command separators so that path
# extraction only inspects the actual delete command(s) instead of swallowing
# unrelated arguments of later commands (e.g. ``del x && python -m pytest
# tests/`` must not treat ``tests/`` as a deletion target).
_DELETE_CMD_SEGMENT_RE = re.compile(
    r"(?:\s*(?:&&|\|\||;|\|)\s*|(?<!\S)&(?!\S)|\r?\n)"
)

# Unwrap a ``cmd /c "<command>"`` wrapper (common on Windows) so inner delete
# commands are still recognized despite the quote before the keyword.
_WIN_CMD_C_WRAPPER_RE = re.compile(
    r"(?is)^\s*cmd(?:\.exe)?\s+/[ck]\s+[\"']?(?P<payload>.*?)[\"']?\s*$"
)

# PowerShell ``-EncodedCommand <base64>`` (UTF-16-LE) payloads produced by the
# Windows compat normalizer for multiline scripts.
_WIN_POWERSHELL_ENCODED_RE = re.compile(
    r"(?is)-EncodedCommand\s+([A-Za-z0-9+/=]+)"
)

# Patterns that indicate a command may delete files.
# Group 1 captures the command keyword; group 2 captures the rest.
_DELETE_CMD_PATTERNS = [
    # Unix / Linux / macOS
    re.compile(r"(?<!\S)(rm)(?:\s+(-\S+(?:\s+-\S+)*)\s+)?(.+)", re.I),
    re.compile(r"(?<!\S)(unlink)(?:\s+)(.+)", re.I),
    # Windows CMD
    re.compile(r"(?<!\S)(del)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
    re.compile(r"(?<!\S)(erase)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
    # Windows / cross-platform PowerShell cmdlets
    re.compile(r"(?<!\S)(Remove-Item)(?:\s+(-\S+(?:\s+-\S+)*)\s+)?(.+)", re.I),
    # rmdir / rd (delete directory) — capture paths in case they point at files
    re.compile(r"(?<!\S)(rmdir|rd)(?:\s+(/\S+(?:\s+/\S+)*)\s+)?(.+)", re.I),
]


def _is_potential_delete_command(command: str) -> bool:
    """Quick check whether *command* may delete files so we can avoid the
    overhead of path extraction when the command is purely informational."""
    if not command:
        return False
    lowered = command.strip().lower()
    keywords = (
        "rm ", "rm\t", "del ", "del\t", "erase ", "erase\t",
        "remove-item ", "remove-item\t", "unlink ", "unlink\t",
        "rmdir ", "rd ",
    )
    for keyword in keywords:
        if keyword in lowered:
            return True
    # Also match PowerShell encoded commands that may contain Remove-Item
    if "remove-item" in lowered:
        return True
    # Decode ``-EncodedCommand`` payloads so delete keywords inside them are
    # still detected (the base64 blob itself contains no keywords).
    if "-encodedcommand" in lowered:
        enc_match = _WIN_POWERSHELL_ENCODED_RE.search(command)
        if enc_match:
            try:
                decoded = base64.b64decode(enc_match.group(1)).decode("utf-16-le")
            except Exception:
                decoded = ""
            dl = decoded.lower()
            if any(k in dl for k in keywords) or "remove-item" in dl:
                return True
    return False


def _extract_delete_file_paths(
    command: str, cwd: Path, include_missing: bool = False
) -> List[Path]:
    """Parse *command* for file paths that are likely deletion targets.

    Handles plain shell commands and PowerShell ``-Command`` wrappers.
    Resolves relative paths against *cwd* and returns paths that currently
    exist as regular files.  When a directory is targeted (e.g. ``rm -rf
    dir/``), all files under that directory are collected recursively.
    With *include_missing* the resolved target paths are appended even when
    they do not exist (useful for safety classification, e.g. deciding
    whether a cleanup command only touches the disposable workspace cache).
    """
    if not command:
        return []
    cwd = Path(cwd).resolve()
    paths: List[Path] = []
    cmd_stripped = command.strip()

    # If the command is wrapped in powershell -Command "...", unwrap one layer
    # so we can inspect the payload for Remove-Item / rm calls.
    ps_match = _WIN_POWERSHELL_COMMAND_RE.match(cmd_stripped)
    if ps_match:
        payload_raw = ps_match.group("payload").strip()
        payload, _ = _strip_powershell_payload_quotes(payload_raw)
        if payload:
            cmd_stripped = payload

    # Unwrap a ``cmd /c "..."`` wrapper (common on Windows) so inner delete
    # commands are still recognized.
    cmd_c_match = _WIN_CMD_C_WRAPPER_RE.match(cmd_stripped)
    if cmd_c_match:
        payload = cmd_c_match.group("payload").strip()
        if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in ('"', "'"):
            payload = payload[1:-1]
        if payload:
            cmd_stripped = payload

    # Try each delete-command pattern against both the original command and
    # any unwrapped PowerShell payload.
    candidates = [cmd_stripped]
    if cmd_stripped != command.strip():
        candidates.append(command.strip())
    # Also decode ``-EncodedCommand`` payloads (multiline PowerShell scripts
    # rewritten by the compat normalizer).
    enc_match = _WIN_POWERSHELL_ENCODED_RE.search(command)
    if enc_match:
        try:
            decoded = base64.b64decode(enc_match.group(1)).decode("utf-16-le")
        except Exception:
            decoded = ""
        if decoded.strip() and decoded.strip() not in candidates:
            candidates.append(decoded.strip())

    def _collect_file(p: Path) -> None:
        """Add *p* to paths if it is a regular file that isn't already
        tracked; if *p* is a directory, recurse into it."""
        try:
            if p.is_file():
                if p not in paths:
                    paths.append(p)
            elif p.is_dir():
                for entry in p.rglob("*"):
                    if entry.is_file() and entry not in paths:
                        paths.append(entry)
                if include_missing and p not in paths:
                    paths.append(p)
        except (OSError, PermissionError):
            pass

    for candidate in candidates:
        # Only inspect the delete command segments: arguments of commands
        # chained after ``&&`` / ``;`` / ``|`` are not deletion targets.
        for segment in _DELETE_CMD_SEGMENT_RE.split(candidate):
            segment = segment.strip()
            if not segment:
                continue
            m = _DELETE_CMD_PATTERNS[0].search(segment)
            if not m:
                # Fall back to the other patterns (rm/unlink/Remove-Item/...).
                for pattern in _DELETE_CMD_PATTERNS[1:]:
                    m = pattern.search(segment)
                    if m:
                        break
            if not m:
                continue
            rest = m.group(3) or ""
            # Split the rest by whitespace to get individual path tokens.
            # shlex may fail on mismatched quotes (e.g. when a trailing
            # quote from a PowerShell -Command wrapper leaks in).
            raw_tokens = rest.split() if rest else []
            try:
                tokens = shlex.split(rest) if rest else []
            except ValueError:
                tokens = rest.split()
            if os.name == "nt" and rest:
                # POSIX shlex treats backslashes as escapes, which mangles
                # Windows drive paths (``C:\x`` -> ``C:x``). Add a plain
                # whitespace split so backslash paths survive; duplicates are
                # skipped downstream (``_collect_file`` dedupes exact paths).
                tokens = list(tokens) + [
                    t for t in rest.split() if t not in tokens
                ]
            for token in tokens:
                # Skip flags/options
                if token.startswith("-") or token.startswith("/"):
                    continue
                # Strip surrounding quotes
                token = token.strip().strip("'\"")
                if not token or token in (".", ".."):
                    continue
                # Resolve path
                p = Path(token)
                if not p.is_absolute():
                    p = cwd / p
                try:
                    p = p.resolve()
                except OSError:
                    continue
                # Support glob expansion (e.g. rm *.log)
                if "*" in token or "?" in token:
                    parent = p.parent if not token.startswith("*") else cwd
                    glob_pattern = p.name if not token.startswith("*") else token
                    matched_any = False
                    try:
                        for matched in parent.glob(glob_pattern):
                            _collect_file(matched)
                            matched_any = True
                    except Exception:
                        pass
                    if include_missing and not matched_any and token in raw_tokens:
                        if p not in paths:
                            paths.append(p)
                else:
                    _collect_file(p)
                    if include_missing and token in raw_tokens and p not in paths:
                        paths.append(p)

    # Filter to only files within the workspace (or at least under cwd)
    workspace_paths = []
    for p in paths:
        try:
            p.relative_to(cwd)
        except ValueError:
            continue
        workspace_paths.append(p)

    return workspace_paths


def _snapshot_files_content(paths: List[Path]) -> Dict[str, str]:
    """Read the contents of *paths* into a dict mapping str(path) -> content."""
    snapshots: Dict[str, str] = {}
    for p in paths:
        try:
            if p.is_file():
                snapshots[str(p)] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return snapshots


def _snapshot_workspace_file_list(cwd: Path) -> Dict[str, Tuple[float, int]]:
    """Walk *cwd* recursively and return ``{path_str: (mtime, size)}`` for
    every regular file.  The snapshot is lightweight (metadata only).
    The app config directory and common build/scm directories are excluded
    to avoid picking up irrelevant data as spurious file changes."""
    _config_dirname = get_app_config_dirname()
    _skip_dirs = {_config_dirname, ".git", "node_modules", "__pycache__", ".pytest_cache", ".vite"}
    snapshot: Dict[str, Tuple[float, int]] = {}
    try:
        for entry in cwd.rglob("*"):
            try:
                if entry.is_file():
                    if any(p.name in _skip_dirs for p in entry.parents):
                        continue
                    stat = entry.stat()
                    snapshot[str(entry)] = (stat.st_mtime, stat.st_size)
            except OSError:
                pass
    except (OSError, PermissionError):
        pass
    return snapshot


# Guard rails for the before-content snapshot.  Without these, a command that
# references a workspace directory (e.g. ``git -C <repo> status``) would
# recursively expand every file beneath it and read them all into memory,
# including .git pack files, virtualenvs, node_modules, etc.  On a large
# repository that easily grows into hundreds of MB -- or tens of GB when
# .git/objects packs are decoded -- and stalls the machine.  The snapshot is
# best-effort: files skipped here still fall back to git (index/HEAD) when
# they are tracked, so only untracked-file diffs degrade.
_SKIP_DIR_NAMES: Set[str] = {
    ".git", "node_modules", ".venv", ".venv-windows",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", ".pnpm-store", ".codewood",
}
_SNAPSHOT_MAX_FILES = 4000
_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
_SNAPSHOT_MAX_FILE_BYTES = 1 * 1024 * 1024


def _snapshot_workspace_before_content(
    command: str,
    execution_cwd: Path,
    repo_root: Optional[Path],
) -> Dict[str, str]:
    """Snapshot file contents that git cannot recover after a shell command
    overwrites them.

    The previous implementation captured this state via a temporary
    ``git stash push --keep-index`` round-trip, which could leave the working
    tree in a conflicted state (3-way merge conflicts in files that had both
    staged and unstaged changes).  Reading the contents up-front avoids any
    repository mutation and guarantees the pre-execution bytes are available
    for diff construction.

    The following files are captured:

    1. untracked files (``git ls-files --others``) -- git has no other copy
       of these.
    2. tracked files with unstaged modifications (``git diff --name-only``)
       -- the unstaged portion is not recoverable from the index or HEAD.
    3. existing files explicitly referenced by the command (covers non-git
       workspaces and files that are clean in git but may still be rewritten
       by the command).

    Returns ``{abs_path_str: content}``.  Individual read failures are
    skipped silently.
    """
    snapshot: Dict[str, str] = {}
    _snapshot_bytes = 0

    def _read_if_file(abs_path: Path) -> None:
        nonlocal _snapshot_bytes
        key = str(abs_path)
        if key in snapshot:
            return
        if len(snapshot) >= _SNAPSHOT_MAX_FILES:
            return
        try:
            if abs_path.is_file():
                size = abs_path.stat().st_size
                if size > _SNAPSHOT_MAX_FILE_BYTES:
                    return
                if _snapshot_bytes + size > _SNAPSHOT_MAX_BYTES:
                    return
                snapshot[key] = abs_path.read_text(
                    encoding="utf-8", errors="replace",
                )
                _snapshot_bytes += size
        except Exception:
            pass

    if repo_root is not None:
        # Untracked files: git has no pre-execution copy of these.
        try:
            result = _subprocess_mod.run(
                ["git", "-C", str(repo_root), "ls-files", "--others",
                 "--exclude-standard", "-z"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\0"):
                    line = line.strip()
                    if not line:
                        continue
                    _read_if_file((repo_root / line).resolve())
        except Exception as e:
            _log.info("ls-files error: %s", e)
        # Tracked files with unstaged modifications.
        try:
            result = _subprocess_mod.run(
                ["git", "-C", str(repo_root), "-c", "core.quotepath=false",
                 "diff", "--name-only", "-z"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\0"):
                    line = line.strip()
                    if not line:
                        continue
                    _read_if_file((repo_root / line).resolve())
        except Exception as e:
            _log.info("git diff --name-only error: %s", e)

    # Command-referenced existing files: covers non-git workspaces.
    try:
        cmd_paths = _extract_command_file_paths(command, execution_cwd)
        cmd_paths = expand_command_file_paths(
            command, execution_cwd, cmd_paths,
        )
        for p in cmd_paths:
            _read_if_file(Path(p))
    except Exception as e:
        _log.info("command path snapshot error: %s", e)

    return snapshot


def _extract_command_file_paths(command: str, cwd: Path) -> Set[str]:
    """Extract absolute file paths that are referenced in a shell command.

    Only returns paths that actually exist as files or directories in the
    workspace.  Directory paths are expanded to all files beneath them so
    that commands like ``rm -rf src/`` are scoped correctly.

    This is intentionally conservative — many indirect side-effects are
    missed — so that modification/deletion detection only fires for files
    the command *explicitly* names, avoiding false attribution of changes
    made by the user during execution."""

    # Shell builtins and common utility commands that don't operate on files
    # (or only read the filesystem).  Their arguments should not be treated
    # as file-operand paths.
    _skip_tokens = {
        "cd", "chdir", "pushd", "popd",
        "export", "set", "unset", "env",
        "echo", "printf", "type", "which", "where",
        "&&", "||", "|", ";",
        "cmd", "powershell",
        "exit",
    }
    # Commands whose arguments are arguments, not file operand paths.
    _skip_cmd_keywords = {
        "timeout", "sleep",
    }
    # Windows: DIR, TYPE, CD etc. are case-insensitive.
    _skip_tokens = _skip_tokens | {t.upper() for t in _skip_tokens}
    _skip_cmd_keywords = _skip_cmd_keywords | {t.upper() for t in _skip_cmd_keywords}

    # Unwrap common Windows wrappers (``powershell -Command "<payload>"``,
    # ``powershell -EncodedCommand <base64>``, ``cmd /c "<payload>"``) so
    # rename / move / create / delete commands inside the payload are
    # attributed.  Mirrors the delete-target parser's unwrapping.
    ps_match = _WIN_POWERSHELL_COMMAND_RE.match(command.strip())
    if ps_match:
        payload_raw = ps_match.group("payload").strip()
        payload, _ = _strip_powershell_payload_quotes(payload_raw)
        if payload:
            command = payload
    cmd_c_match = _WIN_CMD_C_WRAPPER_RE.match(command.strip())
    if cmd_c_match:
        payload = cmd_c_match.group("payload").strip()
        if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in ('"', "'"):
            payload = payload[1:-1]
        if payload:
            command = payload
    enc_match = _WIN_POWERSHELL_ENCODED_RE.search(command)
    if enc_match:
        try:
            decoded = base64.b64decode(enc_match.group(1)).decode("utf-16-le")
        except Exception:
            decoded = ""
        if decoded.strip():
            command = decoded.strip()

    paths: Set[str] = set()
    try:
        import shlex
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    redirect_ops = {">", ">>", "<", "<<", "2>", "2>>", "1>", "1>>", "&>"}

    def _resolve(token: str) -> Optional[Path]:
        """Resolve *token* to an absolute path inside *cwd*."""
        p = Path(token)
        if not p.is_absolute():
            p = cwd / p
        try:
            p = p.resolve()
        except (OSError, ValueError):
            return None
        if p.exists():
            return p
        return None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in redirect_ops:
            i += 1
            if i < len(tokens):
                resolved = _resolve(tokens[i])
                if resolved is not None:
                    paths.add(str(resolved))
            i += 1
            continue

        if tok and not tok.startswith("-"):
            tok_lower = tok.lower()
            if tok_lower in _skip_tokens:
                # Skip the entire token (e.g. "cd"), and also skip its NEXT
                # argument (e.g. the directory path after "cd").
                if tok_lower in {"cd", "chdir", "pushd"}:
                    i += 1  # skip the argument too
                i += 1
                continue
            if tok_lower in _skip_cmd_keywords:
                # These commands take non-file arguments → skip the command
                # and its next argument entirely.
                i += 1  # skip the first argument
                while i < len(tokens) and tokens[i].startswith("-"):
                    i += 1  # skip flag arguments
                i += 1
                continue
            resolved = _resolve(tok)
            if resolved is not None:
                if resolved.is_dir():
                    try:
                        for _root, _dirs, _files in os.walk(resolved):
                            _dirs[:] = [d for d in _dirs if d not in _SKIP_DIR_NAMES]
                            for _name in _files:
                                paths.add(str(Path(_root) / _name))
                    except (OSError, PermissionError):
                        pass
                elif resolved.is_file():
                    paths.add(str(resolved))

        i += 1

    # ---- inline-code interpreters: scan code-string arguments for path-
    #      like string literals that resolve inside the workspace -----------
    if tokens:
        _first = tokens[0].lower()
        _first_base = Path(_first).name
        _flag_set = _INLINE_CODE_INTERPRETERS.get(_first_base)
        if _flag_set is not None:
            for j in range(1, len(tokens) - 1):
                if tokens[j].lower() in _flag_set:
                    code_str = tokens[j + 1]
                    _add_paths_from_inline_code(code_str, cwd, paths)
                    break

    return paths


# Patterns to extract quoted path-like strings from inline script code.
# The negative lookahead skips strings that are clearly dict keys / enum
# members (followed by ":" or "=" or ")" immediately), but accepts
# strings followed by "," or " " — those are common in function arguments
# like open('path', 'mode').
_INLINE_PATH_PATTERNS = [
    re.compile(r"'((?:[^'\\]|\\.)*)'(?!\s*[;:=(})\]])", re.DOTALL),
    re.compile(r'"((?:[^"\\]|\\.)*)"(?!\s*[;:=(})\]])', re.DOTALL),
]


def _add_paths_from_inline_code(
    code: str,
    cwd: Path,
    paths: Set[str],
) -> None:
    """Scan *code* (a ``-c``/``-e`` inline-script argument) for quoted
    string literals that look like file paths and add those that plausibly
    resolve inside (or relative to) *cwd*."""
    for pat in _INLINE_PATH_PATTERNS:
        for m in pat.finditer(code):
            lit = m.group(1)
            if not lit or len(lit) > 500 or len(lit) < 2:
                continue
            if lit.isdigit():
                continue
            if "\n" in lit or "\r" in lit:
                continue
            # Normalise common escape sequences.
            try:
                decoded = lit.encode("latin1", errors="replace").decode("unicode_escape")
            except (UnicodeDecodeError, UnicodeEncodeError):
                decoded = lit
            p = Path(decoded)
            if not p.is_absolute():
                p = cwd / p
            try:
                p = p.resolve()
            except (OSError, ValueError):
                continue
            # For inline-code paths we accept both existing paths AND paths
            # whose parent directory exists (the file may be about to be
            # created by the command).
            if p.exists():
                if p.is_dir():
                    try:
                        for _root, _dirs, _files in os.walk(p):
                            _dirs[:] = [d for d in _dirs if d not in _SKIP_DIR_NAMES]
                            for _name in _files:
                                paths.add(str(Path(_root) / _name))
                    except (OSError, PermissionError):
                        pass
                elif p.is_file():
                    paths.add(str(p))
            else:
                parent = p.parent
                if parent.exists() and parent.is_dir():
                    paths.add(str(p))


# ---- inline-code interpreter helpers shared by the diagnostic and path
#      extraction code ----------------------------------------------------
_INLINE_CODE_INTERPRETERS: Dict[str, Set[str]] = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "--eval"},
    "ruby": {"-e"},
    "perl": {"-e"},
    "php": {"-r"},
    "bash": {"-c"},
    "sh": {"-c"},
    "pwsh": {"-command", "-c"},
    "powershell": {"-command", "-c"},
}


def _is_inline_code_command(command: str) -> bool:
    """Return True when *command* runs an inline-code interpreter and
    embeds its file-operand knowledge inside a code-string argument."""
    stripped = command.strip()
    if not stripped:
        return False
    first_token = stripped.split(None, 1)[0].lower()
    first_base = Path(first_token).name
    flag_set = _INLINE_CODE_INTERPRETERS.get(first_base)
    if flag_set is None:
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    return any(tok in flag_set for tok in tokens[1:])


def _diff_workspace_snapshots(
    before: Dict[str, Tuple[float, int]],
    after: Dict[str, Tuple[float, int]],
) -> Tuple[List[str], List[str], List[str]]:
    """Return ``(new_files, modified_files, deleted_files)`` by comparing the
    pre- and post-execution workspace snapshots."""
    new_files = [p for p in after if p not in before]
    deleted_files = [p for p in before if p not in after]
    modified_files = [
        p for p in before
        if p in after and before[p] != after[p]
    ]
    return new_files, modified_files, deleted_files


def _normalize_line_endings(text: str) -> str:
    """Normalize CRLF/CR line endings to LF for content comparison."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _detect_rename_pairs(
    new_files: List[str],
    ws_deleted: List[str],
    before_content_snapshot: Dict[str, str],
    repo_root: Optional[Path],
    cmd_paths: Set[str],
    delete_snapshots: Optional[Dict[str, str]] = None,
) -> List[Tuple[str, str]]:
    """Pair workspace-deleted files with newly-created files whose content
    matches exactly (e.g. ``mv a b`` / ``ren a b`` / ``git mv a b``).

    Only pairs whose *new* path is explicitly referenced by the command are
    returned, so renames performed as side effects by unrelated processes are
    not falsely attributed to the command.  Old paths already captured by the
    delete-target parser (``delete_snapshots``) keep their dedicated delete
    treatment instead of being reclassified as a rename.

    Returns ``[(old_path, new_path), ...]``.
    """
    if not new_files or not ws_deleted:
        return []
    delete_snapshots = delete_snapshots or {}
    new_by_key: Dict[str, str] = {}
    for p in new_files:
        if p not in cmd_paths:
            continue
        try:
            content = Path(p).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        new_by_key.setdefault(_normalize_line_endings(content), p)
    pairs: List[Tuple[str, str]] = []
    for old in ws_deleted:
        if old in delete_snapshots:
            continue
        content = before_content_snapshot.get(old)
        if content is None and repo_root is not None:
            content = _git_content_before(repo_root, old)
        if not content:
            continue
        new_path = new_by_key.get(_normalize_line_endings(content))
        if new_path is not None and new_path != old:
            pairs.append((old, new_path))
    return pairs


def _build_all_add_diff_rows(content: str) -> List[Dict[str, Any]]:
    """Build ``DiffRow[]`` representing the entire *content* as added lines
    (used for newly-created files or modified files where we lack the
    pre-modification content)."""
    if not content:
        return []
    lines = content.splitlines()
    return [
        {
            "type": "add",
            "oldNo": None,
            "newNo": i + 1,
            "oldText": "",
            "newText": line,
        }
        for i, line in enumerate(lines)
    ]


def _build_all_del_diff_rows(content_before: str) -> List[Dict[str, Any]]:
    """Build ``DiffRow[]`` representing the entire *content_before* as deleted lines
    (used for files that were deleted during command execution)."""
    if not content_before:
        return []
    lines = content_before.splitlines()
    return [
        {
            "type": "del",
            "oldNo": i + 1,
            "newNo": None,
            "oldText": line,
            "newText": "",
        }
        for i, line in enumerate(lines)
    ]


def _is_binary_file(file_path: str) -> bool:
    """Return True if *file_path* looks like binary data by checking the
    first 8 KB of raw bytes for null characters."""
    try:
        with open(file_path, "rb") as fh:
            chunk = fh.read(8192)
        return b"\0" in chunk
    except Exception:
        return False


def cleanup_codewood_shell_pre_stashes(cwd: Path) -> Dict[str, Any]:
    """Delete stale ``codewood_shell_pre`` stashes for the git repo at *cwd*.

    Returns a small result dictionary so callers can log or test behavior
    without parsing command output.
    """
    repo_root = _git_repo_root(cwd)
    if repo_root is None:
        return {"checked": False, "repo_root": None, "removed": 0, "failed": []}
    try:
        result = _subprocess_mod.run(
            ["git", "-C", str(repo_root), "stash", "list", "--format=%H%x09%gs%x09%gd"],
            capture_output=True, text=True,
            timeout=15,
        )
        if result.returncode != 0:
            _log.info(
                "stash cleanup list failed: repo=%s rc=%d stdout=%s stderr=%s",
                repo_root,
                result.returncode,
                (result.stdout or "").strip()[:200],
                (result.stderr or "").strip()[:200],
            )
            return {"checked": True, "repo_root": str(repo_root), "removed": 0, "failed": ["stash-list"]}
        refs_to_drop: List[str] = []
        for raw_line in (result.stdout or "").splitlines():
            parts = raw_line.split("\t")
            if len(parts) != 3:
                continue
            _stash_hash, message, stash_ref = parts
            if str(message).strip() == "On master: codewood_shell_pre" or str(message).strip().endswith(": codewood_shell_pre"):
                refs_to_drop.append(str(stash_ref).strip())
        removed = 0
        failed: List[str] = []
        for stash_ref in refs_to_drop:
            drop_result = _subprocess_mod.run(
                ["git", "-C", str(repo_root), "stash", "drop", stash_ref],
                capture_output=True, text=True,
                timeout=10,
            )
            if drop_result.returncode == 0:
                removed += 1
            else:
                failed.append(stash_ref)
                _log.info(
                    "stash cleanup drop failed: repo=%s ref=%s rc=%d stdout=%s stderr=%s",
                    repo_root,
                    stash_ref,
                    drop_result.returncode,
                    (drop_result.stdout or "").strip()[:200],
                    (drop_result.stderr or "").strip()[:200],
                )
        return {
            "checked": True,
            "repo_root": str(repo_root),
            "removed": removed,
            "failed": failed,
        }
    except Exception as e:
        _log.info("stash cleanup error: repo=%s err=%s", repo_root, e)
        return {"checked": True, "repo_root": str(repo_root), "removed": 0, "failed": ["exception"]}


def _git_repo_root(cwd: Path) -> Optional[Path]:
    """Return the git repository root for *cwd*, or None."""
    try:
        result = _subprocess_mod.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _git_content_before(
    repo_root: Path, file_path: str,
) -> Optional[str]:
    """Return the pre-execution content of *file_path* from git.

    Resolution order:
    1. ``:<rel>`` -- staged (index) version
    2. ``HEAD:<rel>`` -- last commit

    Callers consult the on-disk before-content snapshot
    (``_snapshot_workspace_before_content``) first; this is the git fallback
    for files that were clean before the command ran.

    Captures raw bytes from git and decodes as UTF-8.
    """
    try:
        rel = Path(file_path).resolve().relative_to(repo_root)
        rel_str = str(rel).replace("\\", "/")
    except (ValueError, OSError) as e:
        _log.warning("path resolve failed: file=%s repo=%s err=%s", file_path, repo_root, e)
        return None
    for _git_ref, _label in [
        (f":{rel_str}", "index"),
        (f"HEAD:{rel_str}", "HEAD"),
    ]:
        if _git_ref is None:
            continue
        try:
            result = _subprocess_mod.run(
                ["git", "-C", str(repo_root), "show", _git_ref],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
            _log.debug("%s miss: %s rc=%s stderr=%s", _label, rel_str,
                        result.returncode, result.stderr.decode("utf-8", errors="replace")[:200])
        except Exception as e:
            _log.warning("%s error: %s err=%s", _label, rel_str, e)
    _log.debug("all sources miss: file=%s rel=%s", file_path, rel_str)
    return None


def _build_real_diff_rows(content_before: str, content_after: str) -> List[Dict[str, Any]]:
    """Compute frontend ``DiffRow[]`` from two content strings via difflib."""
    import difflib
    before_lines = content_before.splitlines()
    after_lines = content_after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    rows: List[Dict[str, Any]] = []
    old_no = 1
    new_no = 1
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append({
                    "type": "context",
                    "oldNo": old_no + k, "newNo": new_no + k,
                    "oldText": before_lines[i1 + k],
                    "newText": after_lines[j1 + k],
                })
            old_no += i2 - i1
            new_no += j2 - j1
        elif tag == "replace":
            shared = min(i2 - i1, j2 - j1)
            for k in range(shared):
                rows.append({
                    "type": "change",
                    "oldNo": old_no + k,
                    "newNo": new_no + k,
                    "oldText": before_lines[i1 + k],
                    "newText": after_lines[j1 + k],
                })
            for k in range(shared, i2 - i1):
                rows.append({
                    "type": "del",
                    "oldNo": old_no + k,
                    "newNo": None,
                    "oldText": before_lines[i1 + k],
                    "newText": "",
                })
            for k in range(shared, j2 - j1):
                rows.append({
                    "type": "add",
                    "oldNo": None,
                    "newNo": new_no + k,
                    "oldText": "",
                    "newText": after_lines[j1 + k],
                })
            old_no += i2 - i1
            new_no += j2 - j1
        elif tag == "delete":
            for k in range(i2 - i1):
                rows.append({
                    "type": "del",
                    "oldNo": old_no + k, "newNo": None,
                    "oldText": before_lines[i1 + k], "newText": "",
                })
            old_no += i2 - i1
        elif tag == "insert":
            for k in range(j2 - j1):
                rows.append({
                    "type": "add",
                    "oldNo": None, "newNo": new_no + k,
                    "oldText": "", "newText": after_lines[j1 + k],
                })
            new_no += j2 - j1
    return rows
    """Try to read the pre-modification content of *file_path* from git.

    Returns the HEAD version of the file (relative to *cwd*) if the
    workspace is a git repository and the file is tracked, or ``None``
    if git is unavailable or the file isn't tracked."""
    try:
        repo_root = _git_repo_root(cwd)
        if repo_root is None:
            return None
        rel = Path(file_path).resolve().relative_to(repo_root)
        rel_str = str(rel).replace("\\", "/")
        result = _subprocess_mod.run(
            ["git", "-C", str(repo_root), "show", f"HEAD:{rel_str}"],
            capture_output=True, text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def _backup_deleted_file(
    content: str,
    original_path: Path,
    backups_dir: Path,
) -> Optional[str]:
    """Write *content* to a backup file under *backups_dir*.

    Returns the short backup filename (without the directory prefix) on
    success, or ``None`` on failure.  The filename includes a timestamp
    and random suffix to avoid collisions.
    """
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = secrets.token_hex(4)
        name = original_path.name
        backup_name = f"{name}_{ts}_{suffix}.bak"
        backup_path = backups_dir / backup_name
        tmp = backup_path.with_suffix(backup_path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(content.encode("utf-8"))
        tmp.replace(backup_path)
        return backup_name
    except Exception:
        return None


# ---------------------------------------------------------------------------

from .base import BaseTool  # noqa: E402
from ..core.security.git_guard import guard_git_clone_precheck  # noqa: E402


class ShellTool(BaseTool):
    name = "shell"
    description = "Run a shell command."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "force": {"type": "boolean"},
            "bypass_sandbox": {
                "type": "boolean",
                "description": "Request user-approved one-time sandbox bypass (full permissions).",
            },
        },
        "required": ["command"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        shell_cmd = params.get("command")
        if not shell_cmd:
            return {"success": False, "error": "missing command"}

        # Strip redundant ``cd <workspace-root> &&`` prefixes so the
        # GUI/TUI summary is clean and the classifier can recognise
        # trailing search tools.  Do NOT mutate params — the caller may
        # hold a reference to the original model-provided arguments.
        shell_cmd = str(shell_cmd)
        shell_cmd = strip_redundant_cd_prefix(agent, shell_cmd)

        lowered_shell = shell_cmd.lower()
        if agent._mcp_pending_user_input:
            promptish = (
                ("token" in lowered_shell)
                or ("auth" in lowered_shell)
                or ("credential" in lowered_shell)
                or ("set /p" in lowered_shell)
            )
            mcpish = ("mcp" in lowered_shell) or ("figma" in lowered_shell)
            echoish = ("echo " in lowered_shell) or ("set /p" in lowered_shell)
            if promptish and mcpish and echoish:
                waiting = ", ".join(sorted(agent._mcp_pending_user_input.keys()))
                return {
                    "success": False,
                    "retryable": False,
                    "blocked_by_guard": True,
                    "needs_user_input": True,
                    "input_type": "token",
                    "error": (
                        f"detected repeated token prompt loop for server={waiting}; "
                        "wait for fresh user token then retry /mcp reconnect"
                    ),
                }

        if " mcp start" in lowered_shell or ("helper.exe" in lowered_shell and " mcp " in lowered_shell):
            return {
                "success": False,
                "error": (
                    "manual MCP server start via shell is blocked; "
                    "use MCP tools directly (mcp__server__tool)"
                ),
            }

        shell_force = bool(params.get("force", False))
        shell_interactive = bool(params.get("interactive", False))
        shell_bypass_sandbox = bool(params.get("bypass_sandbox", False))
        clone_guard = guard_git_clone_precheck(agent.work_directory, str(shell_cmd), shell_force)
        if isinstance(clone_guard, dict):
            return clone_guard

        if not shell_force:
            for item in reversed(agent.operation_results[-6:]):
                prev_cmd = item.get("command") or {}
                prev_res = item.get("result") or {}
                if prev_cmd.get("action") != "shell":
                    continue
                prev_params = prev_cmd.get("params") or {}
                if str(prev_params.get("command", "")).strip() == str(shell_cmd).strip():
                    if prev_res.get("success", False):
                        msg = "duplicate shell command skipped; set force=true to rerun"
                        return {
                            "success": True,
                            "message": msg,
                            "skipped_duplicate": True,
                            "interactive": shell_interactive,
                            "output": "",
                            "stderr": "",
                            "return_code": 0,
                        }
                    break

        shell_cmd_dict = {
            "action": "shell",
            "params": {
                "command": shell_cmd,
                "interactive": shell_interactive,
                "force": shell_force,
                "input": params.get("input") if isinstance(params.get("input"), str) else None,
                "bypass_sandbox": bool(params.get("bypass_sandbox", False)),
            },
        }
        if shell_bypass_sandbox and _sandbox_active_for_agent(agent):
            # Model-initiated privilege escalation: this call goes through the
            # user's escalation-approval prompt instead of the AI auto-review,
            # so skip the AI review entirely. ``confirmed`` stays False: the
            # generic confirmation prompt is still suppressed afterwards by
            # the approved-escalation flag inside action_shell_command (and a
            # rejected escalation returns before any execution).
            confirmed = False
            _log.info("bypass_sandbox requested: skipping AI auto-review")
        else:
            confirmed = agent._freedom_auto_confirm(shell_cmd_dict)
        return agent.action_shell_command(
            shell_cmd,
            confirmed=confirmed,
            interactive=shell_interactive,
            input_data=None,
            bypass_sandbox=shell_bypass_sandbox,
        )
