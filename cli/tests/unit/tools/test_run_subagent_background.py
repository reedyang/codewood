"""run_subagent background=true tests.

Covers: immediate return with a background_task_id equal to the tool call id,
completion -> status/output via the SHARED BackgroundTaskManager pipeline
(status / wait / kill tools, notification injection, tool-round write-back),
cancel via background_task_kill, and the default background=False sync path.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.background import BackgroundTaskKillTool, BackgroundTaskStatusTool, WaitTool
from cli.tools.background_tasks import BackgroundTaskManager
from cli.tools.run_subagent import RunSubagentTool


class _FakeAgent:
    def __init__(self) -> None:
        self.work_directory = Path.cwd()
        self.config_dir = None
        self._next_tool_call_id = lambda: "call_sa_1"
        self._current_session_chat_key = lambda: "ws::chat"
        self._subagent_depth = 0
        self.conversation_history = []
        self._accumulated_tool_rounds_raw = []


def _fake_run_subagent(
    agent,
    subagent_name,
    prompt,
    image=None,
    topic="",
    cancel_check=None,
    suppress_session_marker=False,
    on_session_created=None,
    _calls=None,
    _delay=0.0,
    _success=True,
    _output=None,
    _wait_cancel=False,
    _emit_session_created=False,
):
    _calls.append(
        {
            "subagent": subagent_name,
            "topic": topic,
            "prompt": prompt,
            "image": image,
            "cancel_check": cancel_check,
            "suppress_session_marker": suppress_session_marker,
        }
    )
    if _emit_session_created and on_session_created is not None:
        on_session_created("sa_live_marker")
    if _wait_cancel and cancel_check is not None:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if cancel_check():
                return {
                    "success": False,
                    "cancelled": True,
                    "output": "cancelled by user",
                    "subagent": subagent_name,
                    "sessionId": "sa_test",
                }
            time.sleep(0.01)
    if _delay:
        time.sleep(_delay)
    return {
        "success": _success,
        "output": _output if _output is not None else f"result:{topic}",
        "subagent": subagent_name,
        "sessionId": "sa_test",
    }


def _wait_for_status(mgr, task_id, want, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(task_id)
        if st.get("status") == want:
            return st
        time.sleep(0.02)
    return mgr.status(task_id)


class RunSubagentBackgroundTests(unittest.TestCase):
    def _run_background(self, agent, calls, **extra):
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(*a, _calls=calls, **kw),
        ):
            return RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t1",
                    "prompt": "do something",
                    "background": True,
                    **extra,
                },
            )

    def test_background_returns_immediately_with_task_id(self):
        agent = _FakeAgent()
        calls = []
        result = self._run_background(agent, calls)
        self.assertTrue(result.get("success"), str(result))
        self.assertTrue(result.get("background"))
        self.assertEqual(result.get("background_task_id"), "call_sa_1")
        self.assertEqual(result.get("status"), "running")
        self.assertIsNone(result.get("return_code"))
        self.assertIsNotNone(getattr(agent, "_background_task_manager", None))
        st = _wait_for_status(agent._background_task_manager, "call_sa_1", "completed")
        self.assertEqual(st.get("status"), "completed")
        self.assertEqual(st.get("return_code"), 0)
        self.assertIn("result:t1", str(st.get("output", "")))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["suppress_session_marker"])
        self.assertTrue(callable(calls[0]["cancel_check"]))

    def test_background_failure_sets_failed_status(self):
        agent = _FakeAgent()
        calls = []
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(
                *a, _calls=calls, _success=False, _output="boom"
            ),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        self.assertTrue(result.get("success"), str(result))
        st = _wait_for_status(agent._background_task_manager, "call_sa_1", "failed")
        self.assertEqual(st.get("status"), "failed")
        self.assertEqual(st.get("return_code"), 1)
        self.assertIn("boom", str(st.get("output", "")))

    def test_kill_cancels_background_subagent(self):
        agent = _FakeAgent()
        calls = []
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(
                *a, _calls=calls, _wait_cancel=True, **kw
            ),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        mgr = agent._background_task_manager
        self.assertEqual(mgr.status("call_sa_1").get("status"), "running")
        kill_res = BackgroundTaskKillTool().execute(
            agent, {"background_task_id": "call_sa_1"}
        )
        self.assertTrue(kill_res.get("success"), str(kill_res))
        st = _wait_for_status(mgr, "call_sa_1", "killed")
        self.assertEqual(st.get("status"), "killed")
        # The worker observed the cancel and returned the cancelled payload.
        self.assertTrue(calls[0]["cancel_check"]())

    def test_status_tool_unknown_id(self):
        agent = _FakeAgent()
        res = BackgroundTaskStatusTool().execute(agent, {"background_task_id": "nope"})
        self.assertFalse(res.get("success"))
        self.assertEqual(res.get("status"), "not_found")

    def test_wait_tool_returns_early_when_subagent_finishes(self):
        agent = _FakeAgent()
        calls = []
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(
                *a, _calls=calls, _delay=0.15
            ),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        res = WaitTool().execute(
            agent, {"seconds": 5, "background_task_id": result["background_task_id"]}
        )
        self.assertTrue(res.get("success"), str(res))
        self.assertEqual(res.get("task_status"), "completed")
        self.assertLess(res.get("waited_seconds", 99), 5)

    def test_drain_notifications_injects_internal_user_message_once(self):
        from cli.agent import Agent, BG_TASK_RESULT_HISTORY_PREFIX

        agent = _FakeAgent()
        agent._inject_pending_background_task_results = (
            Agent._inject_pending_background_task_results.__get__(agent, Agent)
        )
        agent._parse_background_task_result_history_content = (
            Agent._parse_background_task_result_history_content.__get__(agent, Agent)
        )
        agent._build_background_task_result_history_content = (
            Agent._build_background_task_result_history_content.__get__(agent, Agent)
        )
        agent.conversation_history = []
        agent._append_chat_message = (
            lambda role, content, **kw: agent.conversation_history.append(
                {"role": role, "content": content, "_internal": kw.get("_internal", False)}
            )
        )
        mgr = BackgroundTaskManager(agent)
        agent._background_task_manager = mgr
        rec = mgr.register_task(
            task_id="call_sa_x",
            agent=agent,
            chat_key="ws::chat",
            command="run_subagent(...)",
            cwd=".",
            process_ref={},
            worker_state={
                "done": threading.Event(),
                "return_code": 0,
                "aborted": False,
            },
            stdout_chunks=["final answer"],
            stream_chunks_lock=threading.Lock(),
            merge_path=None,
            sink=None,
            kind="subagent",
        )
        rec.status = "completed"
        rec.final_out = "final answer"
        rec.return_code = 0
        rec.notification_pending = True

        agent._inject_pending_background_task_results()
        self.assertEqual(len(agent.conversation_history), 1)
        msg = agent.conversation_history[0]
        self.assertEqual(msg["role"], "user")
        self.assertTrue(msg["_internal"])
        self.assertTrue(str(msg["content"]).startswith(BG_TASK_RESULT_HISTORY_PREFIX))
        parsed = agent._parse_background_task_result_history_content(msg["content"])
        self.assertEqual(parsed["background_task_id"], "call_sa_x")
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["return_code"], 0)

        agent._inject_pending_background_task_results()
        self.assertEqual(len(agent.conversation_history), 1)

    def test_update_round_output_matches_run_subagent_entry(self):
        agent = _FakeAgent()
        mgr = BackgroundTaskManager(agent)
        rec = mgr.register_task(
            task_id="call_sa_y",
            agent=agent,
            chat_key="ws::chat",
            command="run_subagent(...)",
            cwd=".",
            process_ref={},
            worker_state={"done": threading.Event()},
            stdout_chunks=[],
            stream_chunks_lock=threading.Lock(),
            merge_path=None,
            sink=None,
            kind="subagent",
        )
        rec.final_out = "final result text"
        rec.session_marker = "\ue008sa_abc\ue009"
        agent.conversation_history.append(
            {
                "role": "assistant",
                "content": "",
                "_tool_rounds_raw": [
                    {
                        "tool": "run_subagent",
                        "bgTaskId": "call_sa_y",
                        "output": "Background sub-agent started ...",
                    }
                ],
            }
        )
        self.assertTrue(mgr._update_round_output(rec))
        entry = agent.conversation_history[0]["_tool_rounds_raw"][0]
        self.assertEqual(entry["output"], "final result text")
        self.assertEqual(entry["marker"], "\ue008sa_abc\ue009")

    def test_marker_written_back_to_raw_round_after_background_completion(self):
        """Repro: after a background sub-agent finishes, the GUI must be able
        to open its sub-session from the run_subagent tool-call block.  The
        session id only exists once the worker created it, so the finalizer
        must write the ``_guiSessionMarker`` back into the raw tool round."""
        from cli.agent import Agent

        td = tempfile.mkdtemp(prefix="sa_mk_")
        agent = Agent(
            model_name="gpt-4.1",
            work_directory=td,
            provider="openai",
            config_dir=str(Path(td) / "cfg"),
            chats_root_override=str(Path(td) / "chats"),
        )
        agent.conversation_history = []
        agent.conversation_history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_sa_marker",
                        "type": "function",
                        "function": {
                            "name": "run_subagent",
                            "arguments": (
                                '{"subagent": "image-analyzer", "topic": "t", '
                                '"prompt": "p", "background": true}'
                            ),
                        },
                    }
                ],
                "created_at": "2026-01-01 00:00:00",
            }
        )
        agent._last_tool_issuing_assistant = agent.conversation_history[0]

        def _fake_with_marker(*a, **kw):
            result = _fake_run_subagent(*a, _calls=[], _output="done", **kw)
            result["_guiSessionMarker"] = "\ue008sa_bg_marker\ue009"
            result["sessionId"] = "sa_bg_marker"
            return result

        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=_fake_with_marker,
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        self.assertTrue(result.get("success"), str(result))
        task_id = result["background_task_id"]
        agent._record_model_tool_execution_history(
            "run_subagent",
            {"subagent": "image-analyzer", "topic": "t", "prompt": "p", "background": True},
            result,
        )
        _wait_for_status(agent._background_task_manager, task_id, "completed")

        deadline = time.time() + 5.0
        marker = None
        while time.time() < deadline:
            raw = agent._accumulated_tool_rounds_raw or []
            if raw and raw[0].get("marker"):
                marker = raw[0].get("marker")
                break
            hist_raw = agent.conversation_history[0].get("_tool_rounds_raw") or []
            if hist_raw and hist_raw[0].get("marker"):
                marker = hist_raw[0].get("marker")
                break
            time.sleep(0.05)
        self.assertEqual(marker, "\ue008sa_bg_marker\ue009")

    def test_session_marker_pushed_live_while_subagent_running(self):
        """The worker must push the session marker into the GUI tool-call
        block the moment the session is created, so the \"open sub-session\"
        button exists while the sub-agent is still running (not only after the
        finalize write-back)."""
        from cli.agent import Agent

        td = tempfile.mkdtemp(prefix="sa_live_")
        agent = Agent(
            model_name="gpt-4.1",
            work_directory=td,
            provider="openai",
            config_dir=str(Path(td) / "cfg"),
            chats_root_override=str(Path(td) / "chats"),
        )
        emitted = []
        agent._gui_bg_task_output_emit = lambda task_id, text, end=False, status="", return_code=None: emitted.append(
            {
                "task_id": task_id,
                "text": text,
                "end": end,
                "status": status,
            }
        )
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(
                *a, _calls=[], _output="done", _emit_session_created=True, **kw
            ),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        self.assertTrue(result.get("success"), str(result))
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if any("\ue008sa_live_marker\ue009" in e["text"] for e in emitted):
                break
            time.sleep(0.02)
        self.assertTrue(
            any("\ue008sa_live_marker\ue009" in e["text"] for e in emitted),
            f"live marker not pushed; emitted={emitted}",
        )
        live = next(e for e in emitted if "\ue008sa_live_marker\ue009" in e["text"])
        self.assertFalse(live["end"])
        self.assertEqual(live["status"], "running")

    def test_background_false_uses_sync_path(self):
        agent = _FakeAgent()
        calls = []
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(*a, _calls=calls, **kw),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                },
            )
        self.assertTrue(result.get("success"))
        self.assertFalse(result.get("background", False))
        self.assertEqual(result.get("output"), "result:t")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["suppress_session_marker"])
        self.assertIsNone(calls[0]["cancel_check"])
        self.assertIsNone(getattr(agent, "_background_task_manager", None))

    def test_real_agent_round_trip_records_bg_task_id(self):
        """End-to-end: the real Agent records the run_subagent background call
        with ``bgTaskId`` on its raw tool round (the GUI block binding), and
        the shared manager finalizes the task with the fake result."""
        from cli.agent import Agent

        td = tempfile.mkdtemp(prefix="sa_bg_")
        agent = Agent(
            model_name="gpt-4.1",
            work_directory=td,
            provider="openai",
            config_dir=str(Path(td) / "cfg"),
            chats_root_override=str(Path(td) / "chats"),
        )
        agent.conversation_history = []
        agent.conversation_history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_sa_real",
                        "type": "function",
                        "function": {
                            "name": "run_subagent",
                            "arguments": (
                                '{"subagent": "image-analyzer", "topic": "t", '
                                '"prompt": "p", "background": true}'
                            ),
                        },
                    }
                ],
                "created_at": "2026-01-01 00:00:00",
            }
        )
        agent._last_tool_issuing_assistant = agent.conversation_history[0]
        with patch(
            "cli.subagents.executor.run_subagent",
            side_effect=lambda *a, **kw: _fake_run_subagent(*a, _calls=[], **kw),
        ):
            result = RunSubagentTool().execute(
                agent,
                {
                    "subagent": "image-analyzer",
                    "topic": "t",
                    "prompt": "p",
                    "background": True,
                },
            )
        self.assertTrue(result.get("success"), str(result))
        task_id = result["background_task_id"]
        self.assertEqual(task_id, "call_sa_real")
        agent._record_model_tool_execution_history(
            "run_subagent",
            {"subagent": "image-analyzer", "topic": "t", "prompt": "p", "background": True},
            result,
        )
        raw = agent._accumulated_tool_rounds_raw or []
        self.assertTrue(raw, "raw tool rounds missing")
        self.assertEqual(raw[0].get("bgTaskId"), task_id)
        st = _wait_for_status(agent._background_task_manager, task_id, "completed")
        self.assertEqual(st.get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
