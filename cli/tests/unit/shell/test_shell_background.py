"""Background shell task tests for the ``shell background=true`` path.

Covers: immediate return with a background_task_id equal to the tool call id,
no idle/total auto-termination, completion -> hidden internal user message
(injected exactly once), kill / status / wait tools, and the localized
"Ran in background" verb.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.background import (
    BackgroundTaskKillTool,
    BackgroundTaskStatusTool,
    WaitTool,
)
from cli.tools.background_tasks import BackgroundTaskManager
from cli.tools.shell import action_shell_command
from cli.tests.unit.shell.test_shell_command_execution_guards import (
    _DummyAgent,
    _FakePipe,
)


class _FakeBackgroundPopen:
    """Popen stand-in controllable via finish()/kill().

    ``poll()`` returns None until the process finishes, so the shell worker's
    wait loop keeps polling without ever auto-terminating the task.
    """

    def __init__(self, *args, auto_finish: bool = False, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = _FakePipe([b"bg-line-1\n"])
        self.stderr = _FakePipe([])
        self.pid = 424242
        self._exit_code = None
        self._finish = threading.Event()
        self.killed = False
        if auto_finish:
            self.finish(0)

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        self._finish.wait(timeout)
        return self._exit_code if self._exit_code is not None else 0

    def kill(self):
        self.killed = True
        self._exit_code = 1
        self._finish.set()

    def finish(self, code: int = 0):
        self._exit_code = code
        self._finish.set()


def _wait_for_status(mgr, task_id, want, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(task_id)
        if st.get("status") == want:
            return st
        time.sleep(0.02)
    return mgr.status(task_id)


def _wait_for_notification(mgr, want_status, timeout: float = 5.0):
    """Poll drain_notifications until a notification for the wanted terminal
    status appears.  The finalizer sets the terminal ``status`` slightly before
    ``notification_pending``, so a single immediate drain can race it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = mgr.drain_notifications()
        if any(p.get("status") == want_status for p in pending):
            return pending
        time.sleep(0.02)
    return mgr.drain_notifications()


def _start_background(popen, command: str = "echo hi", task_id: str = "call_bg_1"):
    agent = _DummyAgent()
    agent._next_tool_call_id = lambda: task_id
    # Mimic the real Agent's interrupt/kill bookkeeping so kill() and the
    # finalizer can mark a task as killed.
    agent._bg_aborted = False

    def _mark_process_aborted(process):
        agent._bg_aborted = True

    def _terminate_single_process_tree(process):
        if hasattr(process, "kill"):
            try:
                process.kill()
            except Exception:
                pass
        return True

    def _consume_process_aborted(process):
        if getattr(agent, "_bg_aborted", False):
            agent._bg_aborted = False
            return True
        return False

    def _unregister_interruptible_process(process):
        return None

    agent._mark_process_aborted = _mark_process_aborted
    agent._terminate_single_process_tree = _terminate_single_process_tree
    agent._consume_process_aborted = _consume_process_aborted
    agent._unregister_interruptible_process = _unregister_interruptible_process
    with patch("subprocess.Popen", return_value=popen), patch(
        "cli.tools.shell._git_repo_root", return_value=None,
    ), patch(
        "cli.tools.shell._snapshot_workspace_file_list", return_value={},
    ):
        result = action_shell_command(
            agent, command, confirmed=False, interactive=False,
            input_data=None, background=True,
        )
    return agent, result


class ShellBackgroundTests(unittest.TestCase):
    def test_background_returns_immediately_with_task_id(self):
        popen = _FakeBackgroundPopen()
        agent, result = _start_background(popen)
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("background"))
        self.assertEqual(result.get("background_task_id"), "call_bg_1")
        self.assertEqual(result.get("status"), "running")
        self.assertIsNone(result.get("return_code"))
        mgr = getattr(agent, "_background_task_manager", None)
        self.assertIsNotNone(mgr)
        self.assertEqual(mgr.status("call_bg_1").get("status"), "running")
        mgr.kill("call_bg_1")

    def test_background_completes_and_captures_output(self):
        popen = _FakeBackgroundPopen(auto_finish=True)
        agent, result = _start_background(popen)
        mgr = agent._background_task_manager
        st = _wait_for_status(mgr, "call_bg_1", "completed")
        self.assertEqual(st.get("status"), "completed")
        self.assertEqual(st.get("return_code"), 0)
        self.assertIn("bg-line-1", str(st.get("output", "")))

    def test_background_not_killed_by_idle_or_total_timeout(self):
        class _SilentSlowPopen(_FakeBackgroundPopen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.stdout = _FakePipe([])
                self._timer = threading.Timer(0.25, self.finish, args=(0,))
                self._timer.daemon = True
                self._timer.start()

        popen = _SilentSlowPopen()
        agent = _DummyAgent()
        agent._next_tool_call_id = lambda: "call_bg_slow"
        with patch("subprocess.Popen", return_value=popen), patch(
            "cli.tools.shell._git_repo_root", return_value=None,
        ), patch(
            "cli.tools.shell._snapshot_workspace_file_list", return_value={},
        ), patch(
            "cli.tools.shell._SHELL_INTERACTIVE_IDLE_TIMEOUT", 0.01,
        ), patch(
            "cli.tools.shell._SHELL_MAX_TOTAL_TIMEOUT", 0.02,
        ):
            result = action_shell_command(
                agent, "sleep 5", confirmed=False, interactive=False,
                input_data=None, background=True,
            )
        self.assertTrue(result.get("success"))
        st = _wait_for_status(agent._background_task_manager, "call_bg_slow", "completed")
        self.assertEqual(st.get("status"), "completed")
        self.assertFalse(st.get("timed_out", False))
        self.assertNotIn("auto-terminated", str(st.get("output", "")))

    def test_background_kill_marks_killed_and_notifies(self):
        popen = _FakeBackgroundPopen()
        agent, result = _start_background(popen)
        mgr = agent._background_task_manager
        self.assertEqual(mgr.status("call_bg_1").get("status"), "running")
        kill_res = mgr.kill("call_bg_1")
        self.assertTrue(kill_res.get("success"))
        st = _wait_for_status(mgr, "call_bg_1", "killed")
        self.assertEqual(st.get("status"), "killed")
        pending = _wait_for_notification(mgr, "killed")
        self.assertTrue(any(p.get("status") == "killed" for p in pending))

    def test_background_task_kill_tool_unknown_id(self):
        agent = _DummyAgent()
        res = BackgroundTaskKillTool().execute(
            agent, {"background_task_id": "nope"},
        )
        self.assertFalse(res.get("success"))
        self.assertEqual(res.get("status"), "not_found")

    def test_background_task_status_tool_unknown_id(self):
        agent = _DummyAgent()
        res = BackgroundTaskStatusTool().execute(
            agent, {"background_task_id": "nope"},
        )
        self.assertFalse(res.get("success"))
        self.assertEqual(res.get("status"), "not_found")

    def test_wait_tool_returns_early_when_task_finishes(self):
        popen = _FakeBackgroundPopen()
        agent, result = _start_background(popen)
        task_id = result["background_task_id"]
        threading.Timer(0.15, popen.finish, args=(0,)).start()
        res = WaitTool().execute(
            agent, {"seconds": 5, "background_task_id": task_id},
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("task_status"), "completed")
        self.assertLess(res.get("waited_seconds", 99), 5)

    def test_wait_plain_sleep(self):
        agent = _DummyAgent()
        mgr = BackgroundTaskManager(agent)
        agent._background_task_manager = mgr
        t0 = time.time()
        res = mgr.wait(None, 0.2)
        elapsed = time.time() - t0
        self.assertTrue(res.get("success"))
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertAlmostEqual(res.get("waited_seconds"), elapsed, delta=0.2)

    def test_wait_tool_zero_seconds(self):
        agent = _DummyAgent()
        res = WaitTool().execute(agent, {"seconds": 0})
        self.assertTrue(res.get("success"))
        self.assertLessEqual(res.get("waited_seconds", 99), 1.0)

    def test_drain_notifications_injects_internal_user_message_once(self):
        from cli.agent import BG_TASK_RESULT_HISTORY_PREFIX
        from cli.agent import Agent

        agent = _DummyAgent()
        agent._inject_pending_background_task_results = (
            Agent._inject_pending_background_task_results.__get__(agent, Agent)
        )
        agent._parse_background_task_result_history_content = (
            Agent._parse_background_task_result_history_content.__get__(agent, Agent)
        )
        agent._build_background_task_result_history_content = (
            Agent._build_background_task_result_history_content.__get__(agent, Agent)
        )
        mgr = BackgroundTaskManager(agent)
        agent._background_task_manager = mgr
        agent.conversation_history = []
        agent._append_chat_message = (
            lambda role, content, **kw: agent.conversation_history.append(
                {"role": role, "content": content, "_internal": kw.get("_internal", False)}
            )
        )
        rec = mgr.register_task(
            task_id="call_x",
            agent=agent,
            chat_key="",
            command="echo x",
            cwd=".",
            process_ref={},
            worker_state={
                "done": threading.Event(),
                "return_code": 0,
                "timed_out": False,
            },
            stdout_chunks=["hello\n"],
            stream_chunks_lock=threading.Lock(),
            merge_path=None,
            sink=None,
        )
        rec.status = "completed"
        rec.final_out = "hello\n"
        rec.return_code = 0
        rec.notification_pending = True

        agent._inject_pending_background_task_results()
        self.assertEqual(len(agent.conversation_history), 1)
        msg = agent.conversation_history[0]
        self.assertEqual(msg["role"], "user")
        self.assertTrue(msg["_internal"])
        self.assertTrue(str(msg["content"]).startswith(BG_TASK_RESULT_HISTORY_PREFIX))
        parsed = agent._parse_background_task_result_history_content(msg["content"])
        self.assertEqual(parsed["background_task_id"], "call_x")
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["return_code"], 0)

        # Second drain injects nothing (exactly-once).
        agent._inject_pending_background_task_results()
        self.assertEqual(len(agent.conversation_history), 1)

    def test_background_result_parse_rejects_other_content(self):
        agent = _DummyAgent()
        from cli.agent import Agent

        agent._parse_background_task_result_history_content = (
            Agent._parse_background_task_result_history_content.__get__(
                agent, Agent
            )
        )
        self.assertIsNone(agent._parse_background_task_result_history_content("nope"))
        self.assertIsNone(agent._parse_background_task_result_history_content(""))

    def test_ran_background_locale_key(self):
        from cli.config.i18n import translate

        self.assertEqual(translate("status.ran_background", "zh-CN"), "后台执行")
        self.assertEqual(translate("status.ran_background", "en"), "Ran in background")
        self.assertEqual(translate("status.ran", "zh-CN"), "执行")

    def test_background_failure_notifies_with_error(self):
        popen = _FakeBackgroundPopen(auto_finish=True)
        popen._exit_code = 3
        agent, result = _start_background(popen, command="false")
        mgr = agent._background_task_manager
        st = _wait_for_status(mgr, "call_bg_1", "failed")
        self.assertEqual(st.get("status"), "failed")
        self.assertEqual(st.get("return_code"), 3)
        pending = _wait_for_notification(mgr, "failed")
        self.assertTrue(any(p.get("status") == "failed" for p in pending))

    def test_real_agent_shell_tool_background_round_trip(self):
        """End-to-end guard against the Agent.action_shell_command wrapper
        signature drifting from cli.tools.shell.action_shell_command: a
        missing ``background`` kwarg raised TypeError inside the runtime loop,
        which left no role:tool result and ended the turn."""
        import tempfile

        from cli.agent import Agent
        from cli.tools.shell import ShellTool

        td = tempfile.mkdtemp(prefix="bg_real_")
        agent = Agent(
            model_name="gpt-4.1",
            work_directory=td,
            provider="openai",
            config_dir=str(Path(td) / "cfg"),
            chats_root_override=str(Path(td) / "chats"),
        )
        agent._freedom_auto_confirm = lambda cmd: True

        class _AllowPolicy:
            def can_run_shell_in_workdir(self, **_kw):
                return {"allowed": True}

            def is_workspace_cache_path(self, _p):
                return False

            def is_app_protected_path(self, _p):
                return False

        agent._get_path_policy = lambda: _AllowPolicy()
        with patch("cli.tools.shell._git_repo_root", return_value=None), patch(
            "cli.tools.shell._snapshot_workspace_file_list", return_value={},
        ):
            result = ShellTool().execute(
                agent, {"background": True, "command": "echo hi"},
            )
        self.assertTrue(result.get("success"), str(result))
        self.assertTrue(result.get("background"))
        self.assertTrue(result.get("background_task_id"))
        self.assertIsNotNone(getattr(agent, "_background_task_manager", None))

    def test_background_output_written_back_to_tool_rounds_raw(self):
        """The finalizer must rewrite the persisted tool round (raw entry) of
        the issuing shell call with the final output so the GUI block shows the
        command's real output after expand/reload."""
        import tempfile

        from cli.agent import Agent
        from cli.tools.shell import ShellTool

        td = tempfile.mkdtemp(prefix="bg_raw_")
        agent = Agent(
            model_name="gpt-4.1",
            work_directory=td,
            provider="openai",
            config_dir=str(Path(td) / "cfg"),
            chats_root_override=str(Path(td) / "chats"),
        )
        agent._freedom_auto_confirm = lambda cmd: True
        agent.sandbox_level = "full_access"
        agent.sandbox_network = None
        fake_popen = _FakeBackgroundPopen(auto_finish=True)
        agent._get_path_policy = lambda: type(
            "Allow", (), {
                "can_run_shell_in_workdir": lambda self, **kw: {"allowed": True},
                "is_workspace_cache_path": lambda self, p: False,
                "is_app_protected_path": lambda self, p: False,
            },
        )()
        agent.conversation_history = []
        agent.conversation_history.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_bg_raw",
                "type": "function",
                "function": {
                    "name": "shell",
                    "arguments": '{"background": true, "command": "echo hi"}',
                },
            }],
            "created_at": "2026-01-01 00:00:00",
        })
        agent._last_tool_issuing_assistant = agent.conversation_history[0]
        with patch("subprocess.Popen", return_value=fake_popen), patch(
            "cli.tools.shell._WINPTY_PTYPROCESS", None,
        ), patch("cli.tools.shell._git_repo_root", return_value=None), patch(
            "cli.tools.shell._snapshot_workspace_file_list", return_value={},
        ):
            result = ShellTool().execute(
                agent, {"background": True, "command": "echo hi"},
            )
        self.assertTrue(result.get("success"), str(result))
        task_id = result["background_task_id"]
        agent._record_model_tool_execution_history(
            "shell",
            {"command": "echo hi", "background": True},
            result,
        )
        st = _wait_for_status(agent._background_task_manager, task_id, "completed")
        if st.get("status") != "completed":
            rec = agent._background_task_manager.get(task_id)
            print("DIAG worker_state=", dict(rec.worker_state) if rec else None)
            print("DIAG rec status=", rec.status if rec else None, "finalized=", rec.finalized if rec else None)
        self.assertEqual(st.get("status"), "completed")

        deadline = time.time() + 5.0
        entry_out = None
        while time.time() < deadline:
            raw = agent.conversation_history[0].get("_tool_rounds_raw") or []
            if raw and "echo hi" in str(raw[0].get("output", "")):
                break
            raw = agent._accumulated_tool_rounds_raw or []
            if raw and "echo hi" in str(raw[0].get("output", "")):
                break
            time.sleep(0.05)
        raw = agent.conversation_history[0].get("_tool_rounds_raw") or agent._accumulated_tool_rounds_raw or []
        self.assertTrue(raw, "raw tool rounds missing")
        entry_out = str(raw[0].get("output") or "")
        self.assertIn("bg-line-1", entry_out)
        self.assertNotIn("Background task started", entry_out)


if __name__ == "__main__":
    unittest.main()
