"""Interactive-command auto-termination tests for the shell tool.

These tests exercise the idle-timeout watchdog that auto-terminates a
non-interactive shell process blocked on interactive input.  They are
excluded from the default ``run_all_tests.py`` suite because the watchdog
waits on real wall-clock time; run them directly with::

    python -m unittest cli.tests.unit.shell.test_shell_interactive_timeout -v
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import cli.tools.shell as shell_module
from cli.tools.shell import action_shell_command
from cli.tests.unit.shell.test_shell_command_execution_guards import (
    _DummyAgent,
    _FakePipe,
    _FakePopenResult,
)


class _FakePopenHung:
    """Popen mock that stays alive forever (interactive wait) until killed."""

    def __init__(self, stdout_text: str = ""):
        self.stdout = _FakePipe([stdout_text.encode("utf-8")]) if stdout_text else _FakePipe([])
        self.stderr = _FakePipe([])
        self.stdin = None
        self.pid = 424242
        self._alive = True
        self._return_code = 1

    def poll(self):
        return None if self._alive else self._return_code

    def wait(self, timeout=None):
        return None if self._alive else self._return_code

    def kill(self):
        self._alive = False


class _FakePopenExitAfter(_FakePopenResult):
    """Popen mock that exits after ``delay`` seconds (with poll support)."""

    def __init__(self, delay: float, stdout_text: str = "x"):
        super().__init__(stdout_text=stdout_text)
        self._delay = float(delay)
        self._started = time.time()
        self.pid = 12345

    def poll(self):
        if time.time() - self._started >= self._delay:
            return 0
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._started = 0.0


class ShellInteractiveTimeoutTests(unittest.TestCase):
    def test_wait_auto_terminates_silent_interactive_process(self):
        proc = _FakePopenHung("prompt> ")
        agent = _DummyAgent()
        killed = []
        agent._terminate_single_process_tree = lambda p: killed.append(p)
        activity = {"last_activity": time.time() - 999}

        code, timed_out = shell_module._wait_for_process_exit_or_interactive_timeout(
            proc, agent, activity, idle_timeout=0.05, max_total_timeout=1.0,
        )

        self.assertTrue(timed_out)
        self.assertEqual(killed, [proc])
        self.assertEqual(code, 1)

    def test_wait_returns_normally_when_process_exits(self):
        proc = _FakePopenExitAfter(0.0, "ok\n")
        agent = _DummyAgent()
        killed = []
        agent._terminate_single_process_tree = lambda p: killed.append(p)

        code, timed_out = shell_module._wait_for_process_exit_or_interactive_timeout(
            proc, agent, {"last_activity": time.time()},
            idle_timeout=0.05, max_total_timeout=1.0,
        )

        self.assertFalse(timed_out)
        self.assertEqual(killed, [])
        self.assertEqual(code, 0)

    def test_wait_ongoing_activity_defers_idle_termination(self):
        proc = _FakePopenExitAfter(0.3)
        agent = _DummyAgent()
        killed = []
        agent._terminate_single_process_tree = lambda p: killed.append(p)
        activity = {"last_activity": time.time()}
        stop = threading.Event()

        def _poke():
            while not stop.is_set():
                time.sleep(0.02)
                activity["last_activity"] = time.time()

        t = threading.Thread(target=_poke, daemon=True)
        t.start()
        try:
            code, timed_out = shell_module._wait_for_process_exit_or_interactive_timeout(
                proc, agent, activity, idle_timeout=0.2, max_total_timeout=5.0,
            )
        finally:
            stop.set()
            t.join(timeout=1)

        self.assertFalse(timed_out)
        self.assertEqual(killed, [])
        self.assertEqual(code, 0)

    def test_wait_falls_back_to_blocking_wait_without_poll(self):
        proc = _FakePopenResult("ok\n")
        agent = _DummyAgent()

        code, timed_out = shell_module._wait_for_process_exit_or_interactive_timeout(
            proc, agent, {"last_activity": time.time()},
        )

        self.assertFalse(timed_out)
        self.assertEqual(code, 0)

    def test_action_shell_command_auto_terminates_interactive_hang(self):
        agent = _DummyAgent()
        agent._terminate_single_process_tree = lambda p: p.kill()
        proc = _FakePopenHung()

        with patch("subprocess.Popen", return_value=proc), patch(
            "cli.tools.shell._git_repo_root", return_value=None,
        ), patch(
            "cli.tools.shell._snapshot_workspace_file_list", return_value={},
        ), patch(
            "cli.tools.shell._SHELL_INTERACTIVE_IDLE_TIMEOUT", 0.05,
        ), patch(
            "cli.tools.shell._SHELL_MAX_TOTAL_TIMEOUT", 1.0,
        ):
            result = action_shell_command(
                agent, "python -m pip install", confirmed=False,
                interactive=False, input_data=None,
            )

        self.assertFalse(result.get("success", True))
        self.assertTrue(result.get("timed_out", False))
        self.assertNotEqual(result.get("return_code"), 0)
        self.assertIn("auto-terminated", str(result.get("output", "")))
        self.assertIn("auto-terminated", str(result.get("error", "")))

    def test_action_shell_command_happy_path_has_no_timed_out_flag(self):
        agent = _DummyAgent()

        with patch("subprocess.Popen", return_value=_FakePopenResult("ok\n")), patch(
            "cli.tools.shell._git_repo_root", return_value=None,
        ), patch(
            "cli.tools.shell._snapshot_workspace_file_list", return_value={},
        ):
            result = action_shell_command(
                agent, "echo hi", confirmed=False, interactive=False, input_data=None,
            )

        self.assertTrue(result.get("success", False))
        self.assertFalse(result.get("timed_out", False))


if __name__ == "__main__":
    unittest.main()
