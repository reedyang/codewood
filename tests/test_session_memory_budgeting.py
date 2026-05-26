import unittest
import json
from pathlib import Path
import threading
import time
from unittest.mock import MagicMock, patch

from src.services.session_memory_service import SessionMemoryService

DIRECT_SHELL_USER_HISTORY_PREFIX = "[DIRECT_SHELL_USER_COMMAND]"
DIRECT_SHELL_RESULT_HISTORY_PREFIX = "[DIRECT_SHELL_RESULT]"
CONVERSATION_INTERRUPTED_HISTORY_PREFIX = "[CONVERSATION_INTERRUPTED]"
INTERNAL_SLASH_USER_HISTORY_PREFIX = "[INTERNAL_SLASH_USER_COMMAND]"
INTERNAL_SLASH_RESULT_HISTORY_PREFIX = "[INTERNAL_SLASH_RESULT]"
TASK_WORKED_SUMMARY_HISTORY_PREFIX = "[TASK_WORKED_SUMMARY]"


class _FakeAgent:
    def __init__(self):
        self.conversation_history = []
        self.operation_results = []
        self._session_summary_llm = ""
        self._session_summary_rolling = ""
        self.session_summary_llm_enabled = False
        self._last_llm_summary_pair_count = 0
        self._skills_routing_prefix = ""
        self._active_skill_full_prompt = ""
        self._self_repo_root = Path("D:/SourceCode/opensource/smart-shell")
        self.config_dir = Path("D:/Users/fake/.smartshell")
        self.workspace_name = "Default"
        self.active_chat_name = "New Chat"
        self.ai_workspace_dir = Path("D:/Users/fake/.smartshell/workspace/default")
        self.work_directory = Path("D:/SourceCode/opensource/smart-shell")
        self.system_prompt = ""
        self.params = {"context_window": 800}
        self._force_current_input_as_requirement_once = False
        self._last_cancelled_task = ""
        self.active_chat_id = "chat-1"
        self._chat_state = {"chats": []}
        self._active_runtime_task_id = ""
        self._active_runtime_task_domains = []
        self.sync_active_chat_messages_calls = 0

    def _reload_skills(self):
        return None

    def _compose_system_prompt_snapshot(self, include_tools: bool = True):
        _ = include_tools
        return "SYSTEM PROMPT " + ("X" * 1200) + " [SYSTEM_PROMPT_END_MARK]"

    def _ensure_memory_service(self):
        return False

    def _find_chat_by_id(self, chat_id: str):
        for c in self._chat_state.get("chats", []):
            if str(c.get("id") or "") == str(chat_id or ""):
                return c
        return None

    def _domain_specific_system_prompt_append(self):
        domains = list(getattr(self, "_active_runtime_task_domains", None) or [])
        if "software_development" in domains:
            return "\n\n【领域强化：软件开发】\n硬性要求...\n"
        return ""

    def _sync_active_chat_messages(self):
        self.sync_active_chat_messages_calls += 1

    def _build_direct_shell_user_history_content(self, raw_user_command: str) -> str:
        return f"{DIRECT_SHELL_USER_HISTORY_PREFIX}{str(raw_user_command or '').strip()}"

    def _build_direct_shell_result_history_content(
        self,
        raw_user_command: str,
        executed_command: str,
        cwd: str,
        return_code: int,
        stdout_text: str,
        stderr_text: str,
        aborted_by_user: bool = False,
    ) -> str:
        payload = {
            "kind": "direct_shell_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "executed_command": str(executed_command or ""),
            "cwd": str(cwd or ""),
            "return_code": int(return_code),
            "stdout": str(stdout_text or ""),
            "stderr": str(stderr_text or ""),
            "aborted_by_user": bool(aborted_by_user),
        }
        return f"{DIRECT_SHELL_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_direct_shell_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_USER_HISTORY_PREFIX):
            return ""
        return text[len(DIRECT_SHELL_USER_HISTORY_PREFIX):].strip()

    def _parse_direct_shell_result_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(DIRECT_SHELL_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "direct_shell_result" else None

    def _is_direct_shell_result_aborted(self, result):
        if not isinstance(result, dict):
            return False
        if bool(result.get("aborted_by_user", False)):
            return True
        return "command aborted by user" in str(result.get("stdout") or "").lower()

    def _normalize_aborted_direct_shell_stdout_for_history(self, stdout_text: str) -> str:
        text = str(stdout_text or "")
        marker = "command aborted by user"
        if marker not in text.lower():
            return text
        lines = text.splitlines(keepends=True)
        kept = []
        found = False
        for line in lines:
            low = line.lower()
            if marker not in low:
                kept.append(line)
                continue
            found = True
            idx = low.find(marker)
            rebuilt = line[:idx] + line[idx + len(marker):]
            if rebuilt.endswith("\n"):
                rebuilt = rebuilt.rstrip(" \t\r\n") + "\n"
            if rebuilt.strip():
                kept.append(rebuilt)
        merged = "".join(kept)
        if found:
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += "command aborted by user\n"
        return merged

    def _build_conversation_interrupted_history_content(
        self, interrupted_kind: str = "task", reason: str = "user_interrupt", detail: str = ""
    ) -> str:
        payload = {
            "kind": "conversation_interrupted",
            "interrupted_kind": str(interrupted_kind or "task"),
            "reason": str(reason or "user_interrupt"),
            "detail": str(detail or ""),
        }
        return f"{CONVERSATION_INTERRUPTED_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_conversation_interrupted_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):
            return None
        body = text[len(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "conversation_interrupted" else None

    def _build_internal_slash_user_history_content(self, raw_user_command: str) -> str:
        return f"{INTERNAL_SLASH_USER_HISTORY_PREFIX}{str(raw_user_command or '').strip()}"

    def _parse_internal_slash_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_USER_HISTORY_PREFIX):
            return ""
        return text[len(INTERNAL_SLASH_USER_HISTORY_PREFIX):].strip()

    def _build_internal_slash_result_history_content(self, raw_user_command: str, output_text: str) -> str:
        payload = {
            "kind": "internal_slash_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "output": str(output_text or ""),
        }
        return f"{INTERNAL_SLASH_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_internal_slash_result_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "internal_slash_result" else None

    def _build_task_worked_summary_history_content(self, elapsed_seconds: int) -> str:
        payload = {
            "kind": "task_worked_summary",
            "elapsed_seconds": max(0, int(elapsed_seconds or 0)),
        }
        return f"{TASK_WORKED_SUMMARY_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_task_worked_summary_history_content(self, content: str):
        text = str(content or "")
        if not text.startswith(TASK_WORKED_SUMMARY_HISTORY_PREFIX):
            return None
        body = text[len(TASK_WORKED_SUMMARY_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        return payload if isinstance(payload, dict) and str(payload.get("kind") or "") == "task_worked_summary" else None


class SessionMemoryBudgetingTests(unittest.TestCase):
    def test_refresh_context_usage_snapshot_persists_chat_state_immediately(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "请实现功能A"},
            {"role": "assistant", "content": "收到，我先看下结构"},
        ]
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(user_input_hint="继续", context_hint="ctx")

        self.assertGreater(agent.sync_active_chat_messages_calls, 0)

    def test_domain_prompt_is_appended_for_specific_domain(self):
        agent = _FakeAgent()
        agent._active_runtime_task_domains = ["software_development"]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("请修复构建错误")
        system_content = str(messages[0].get("content") or "")
        self.assertIn("【领域强化：软件开发】", system_content)

    def test_domain_filtered_history_collects_all_matching_tasks_from_last_context_message_domain(self):
        agent = _FakeAgent()
        agent._active_runtime_task_id = "task-a"
        agent._active_runtime_task_domains = ["software_development"]
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "tasks": [
                        {"id": "task-a", "domains": ["software_development"]},
                        {"id": "task-b", "domains": ["software_development", "documentation_writing"]},
                        {"id": "task-c", "domains": ["visual_design"]},
                    ],
                    "messages": [
                        {"role": "user", "content": "C1", "task_id": "task-c"},
                        {"role": "user", "content": "A1", "task_id": "task-a"},
                        {"role": "assistant", "content": "A2", "task_id": "task-a"},
                        {"role": "user", "content": "B1", "task_id": "task-b"},
                        {"role": "assistant", "content": "B2", "task_id": "task-b"},
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        filtered = svc._domain_filtered_history()
        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("B1", joined)
        self.assertNotIn("C1", joined)

    def test_domain_filtered_history_ignores_slash_builtin_when_resolving_anchor_domain(self):
        agent = _FakeAgent()
        agent._active_runtime_task_id = "task-c"
        agent._active_runtime_task_domains = ["visual_design"]
        agent._chat_state = {
            "chats": [
                {
                    "id": "chat-1",
                    "tasks": [
                        {"id": "task-a", "domains": ["software_development"]},
                        {"id": "task-c", "domains": ["visual_design"]},
                    ],
                    "messages": [
                        {"role": "user", "content": "A1", "task_id": "task-a"},
                        {"role": "assistant", "content": "A2", "task_id": "task-a"},
                        {"role": "user", "content": "/chat list", "task_id": "task-c"},
                    ],
                }
            ]
        }
        agent.conversation_history = list(agent._chat_state["chats"][0]["messages"])
        svc = SessionMemoryService(agent)
        filtered = svc._domain_filtered_history()
        joined = " | ".join(str(x.get("content") or "") for x in filtered)
        self.assertIn("A1", joined)
        self.assertIn("A2", joined)
        self.assertNotIn("/chat list", joined)

    def test_context_window_budget_prefers_recent_history(self):
        agent = _FakeAgent()
        # First requirement should be preserved explicitly.
        agent.conversation_history.append({"role": "user", "content": "最初需求：做一个任务规划器"})
        # Create long history to trigger budget trimming.
        for i in range(1, 24):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append(
                {"role": role, "content": f"msg-{i} " + ("z" * 180)}
            )
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("现在请继续实现第2步", context="ctx-" + ("c" * 400))

        self.assertGreaterEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        # Latest history should be present (reverse fill by budget).
        joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("msg-23", joined)
        # User original requirement must be explicitly injected.
        self.assertIn("用户原始需求: 最初需求：做一个任务规划器", messages[-1]["content"])
        self.assertIn("用户输入: 现在请继续实现第2步", messages[-1]["content"])

    def test_regular_task_messages_include_recent_aborted_command_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_direct_shell_user_history_content("d:/tmp/builds/install-zr.bat")},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!d:/tmp/builds/install-zr.bat",
                    "d:/tmp/builds/install-zr.bat",
                    "D:/ws",
                    137,
                    "02:20:33 [Info] Searching artifacts...\ncommand aborted by user\n",
                    "",
                    aborted_by_user=True,
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续处理后续步骤")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[命令执行结果]", history_joined)
        self.assertIn("executed_success=false", history_joined)
        self.assertIn("interrupted_by_user=true", history_joined)
        self.assertIn("最近一次直接命令执行被用户强制终止", str(messages[-1]["content"]))

    def test_regular_task_messages_include_direct_command_success_status(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_direct_shell_user_history_content("echo hello")},
            {
                "role": "assistant",
                "content": agent._build_direct_shell_result_history_content(
                    "!echo hello",
                    "echo hello",
                    "D:/ws",
                    0,
                    "hello\n",
                    "",
                    aborted_by_user=False,
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[命令执行结果]", history_joined)
        self.assertIn("executed_success=true", history_joined)
        self.assertIn("interrupted_by_user=false", history_joined)

    def test_regular_task_messages_include_recent_interrupted_task_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "请继续修复构建"},
            {
                "role": "assistant",
                "content": agent._build_conversation_interrupted_history_content(
                    interrupted_kind="task",
                    reason="user_interrupt",
                    detail="修复构建脚本",
                ),
            },
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertIn("[会话中断事件]", history_joined)
        self.assertIn("最近一次任务执行被用户中断（ESC）", str(messages[-1]["content"]))

    def test_internal_slash_history_is_excluded_from_model_context_and_requirement(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": agent._build_internal_slash_user_history_content("/chat reload")},
            {
                "role": "assistant",
                "content": agent._build_internal_slash_result_history_content(
                    "/chat reload",
                    "reloaded\n",
                ),
            },
            {"role": "user", "content": "最初需求：修复构建"},
            {"role": "assistant", "content": "收到"},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续执行")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("/chat reload", history_joined)
        self.assertNotIn("reloaded", history_joined)
        self.assertIn("用户原始需求: 最初需求：修复构建", str(messages[-1]["content"]))

    def test_raw_slash_builtin_user_message_is_excluded_from_model_context_and_requirement(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "/chat reload"},
            {"role": "assistant", "content": "reloaded"},
            {"role": "user", "content": "最初需求：修复构建"},
            {"role": "assistant", "content": "收到"},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续执行")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("/chat reload", history_joined)
        self.assertIn("用户原始需求: 最初需求：修复构建", str(messages[-1]["content"]))

    def test_task_worked_summary_history_is_excluded_from_model_context(self):
        agent = _FakeAgent()
        agent.conversation_history = [
            {"role": "user", "content": "最初需求：修复构建"},
            {"role": "assistant", "content": "收到"},
            {"role": "assistant", "content": agent._build_task_worked_summary_history_content(95)},
        ]
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续执行")
        history_joined = "\n".join(str(m.get("content") or "") for m in messages[1:-1])
        self.assertNotIn("TASK_WORKED_SUMMARY", history_joined)
        self.assertNotIn("Worked for", history_joined)

    def test_original_requirement_falls_back_to_current_input(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("请写一个脚本")
        self.assertIn("用户原始需求: 请写一个脚本", messages[-1]["content"])
        self.assertIn("用户输入: 请写一个脚本", messages[-1]["content"])

    def test_cancelled_previous_task_forces_new_requirement_once(self):
        agent = _FakeAgent()
        agent.conversation_history.append({"role": "user", "content": "旧任务：修复A模块"})
        agent._force_current_input_as_requirement_once = True
        agent._last_cancelled_task = "旧任务：修复A模块"
        svc = SessionMemoryService(agent)

        messages, _ = svc.build_regular_task_messages("新任务：实现B功能")
        user_block = messages[-1]["content"]
        self.assertIn("用户原始需求: 新任务：实现B功能", user_block)
        self.assertIn("最近被取消的任务: 旧任务：修复A模块", user_block)
        self.assertIn("禁止主动恢复或重做被取消任务", user_block)
        self.assertFalse(bool(getattr(agent, "_force_current_input_as_requirement_once", True)))

        messages2, _ = svc.build_regular_task_messages("继续")
        self.assertIn("用户原始需求: 旧任务：修复A模块", messages2[-1]["content"])

    def test_degradation_adds_history_summary_before_full_drop(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 2200}
        agent.conversation_history.append({"role": "user", "content": "最初需求：做一个任务规划器"})
        for i in range(1, 36):
            role = "assistant" if i % 2 == 0 else "user"
            content = f"msg-{i} " + ("assistant-long " * 60 if role == "assistant" else ("user-long " * 30))
            agent.conversation_history.append({"role": role, "content": content})
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续", context="ctx")
        history_messages = messages[1:-1]
        joined = "\n".join(str(m.get("content") or "") for m in history_messages)
        self.assertIn("[历史摘要]", joined)
        self.assertIn("msg-35", joined)

    def test_large_context_keeps_more_history_than_small_context(self):
        small = _FakeAgent()
        large = _FakeAgent()
        small.params = {"context_window": 8000}
        large.params = {"context_window": 128000}
        for i in range(1, 28):
            role = "assistant" if i % 2 == 0 else "user"
            item = {"role": role, "content": f"round-{i} " + ("z" * 200)}
            small.conversation_history.append(dict(item))
            large.conversation_history.append(dict(item))
        sm_svc = SessionMemoryService(small)
        lg_svc = SessionMemoryService(large)
        small_messages, _ = sm_svc.build_regular_task_messages("继续")
        large_messages, _ = lg_svc.build_regular_task_messages("继续")
        small_hist_count = len(small_messages[1:-1])
        large_hist_count = len(large_messages[1:-1])
        self.assertGreaterEqual(large_hist_count, small_hist_count)

    def test_custom_token_estimator_is_used(self):
        base = _FakeAgent()
        custom = _FakeAgent()
        base.params = {"context_window": 2000}
        custom.params = {"context_window": 2000}
        custom.token_estimator = lambda s: len(str(s or ""))
        for i in range(1, 18):
            role = "assistant" if i % 2 == 0 else "user"
            item = {"role": role, "content": f"msg-{i} " + ("q" * 140)}
            base.conversation_history.append(dict(item))
            custom.conversation_history.append(dict(item))
        base_svc = SessionMemoryService(base)
        custom_svc = SessionMemoryService(custom)
        base_messages, _ = base_svc.build_regular_task_messages("继续")
        custom_messages, _ = custom_svc.build_regular_task_messages("继续")
        self.assertLessEqual(len(custom_messages[1:-1]), len(base_messages[1:-1]))

    def test_token_counter_resolution_is_non_blocking_before_warmup_ready(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        svc._builtin_token_counter_init_done = False
        svc._builtin_token_counter = None
        called = {"n": 0}

        def _fake_warmup():
            called["n"] += 1

        svc._start_token_counter_warmup = _fake_warmup  # type: ignore[assignment]
        out = svc._resolve_token_counter()
        self.assertIsNone(out)
        self.assertEqual(called["n"], 1)
        # Falls back to heuristic immediately instead of blocking.
        self.assertGreater(svc._estimate_text_tokens("hello world"), 0)

    def test_schedule_context_usage_refresh_async_is_non_blocking_and_debounced(self):
        agent = _FakeAgent()
        svc = SessionMemoryService(agent)
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1.0)

        svc.refresh_context_usage_snapshot = _slow_refresh  # type: ignore[assignment]
        first = svc.schedule_context_usage_refresh_async()
        self.assertTrue(first)
        self.assertTrue(entered.wait(timeout=0.5))
        # Inflight refresh should reject duplicate scheduling.
        second = svc.schedule_context_usage_refresh_async()
        self.assertFalse(second)
        release.set()
        # allow worker to exit
        time.sleep(0.05)
        third = svc.schedule_context_usage_refresh_async()
        self.assertTrue(third)

    def test_refresh_context_usage_snapshot_skips_when_expected_chat_mismatch(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-2"
        agent._last_context_usage_percent = 12
        agent._last_context_input_tokens = 111
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(
            user_input_hint="new",
            context_hint="ctx",
            expected_chat_id="chat-1",
        )

        self.assertEqual(agent._last_context_usage_percent, 12)
        self.assertEqual(agent._last_context_input_tokens, 111)

    def test_refresh_context_usage_snapshot_uses_composed_prompt_snapshot(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 100000}
        agent.system_prompt = "MUTATED-" + ("X" * 6000)
        compose_calls = {"n": 0}

        def _compose(include_tools=True):
            _ = include_tools
            compose_calls["n"] += 1
            return "STABLE_PROMPT"

        agent._compose_system_prompt_snapshot = _compose
        svc = SessionMemoryService(agent)

        svc.refresh_context_usage_snapshot(user_input_hint="继续", context_hint="ctx")
        first = int(getattr(agent, "_last_context_input_tokens", 0) or 0)

        # Mutate global system_prompt again; refresh should stay stable because it uses composed snapshot.
        agent.system_prompt = "MUTATED-2-" + ("Y" * 8000)
        svc.refresh_context_usage_snapshot(user_input_hint="继续", context_hint="ctx")
        second = int(getattr(agent, "_last_context_input_tokens", 0) or 0)

        self.assertGreaterEqual(compose_calls["n"], 2)
        self.assertEqual(first, second)

    def test_refresh_context_usage_snapshot_skips_when_state_key_mismatch(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-1"
        agent._last_context_usage_percent = 9
        agent._last_context_input_tokens = 999
        svc = SessionMemoryService(agent)

        stale_key = svc._context_usage_state_key()
        agent.conversation_history.append({"role": "user", "content": "state changed"})

        svc.refresh_context_usage_snapshot(
            user_input_hint="继续",
            context_hint="ctx",
            expected_chat_id="chat-1",
            expected_state_key=stale_key,
        )

        self.assertEqual(agent._last_context_usage_percent, 9)
        self.assertEqual(agent._last_context_input_tokens, 999)

    def test_schedule_context_usage_refresh_async_captures_chat_id_at_schedule_time(self):
        agent = _FakeAgent()
        agent.active_chat_id = "chat-1"
        svc = SessionMemoryService(agent)
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh(*_args, **kwargs):
            calls.append(dict(kwargs))
            entered.set()
            release.wait(timeout=1.0)

        svc.refresh_context_usage_snapshot = _slow_refresh  # type: ignore[assignment]
        scheduled = svc.schedule_context_usage_refresh_async()
        self.assertTrue(scheduled)
        self.assertTrue(entered.wait(timeout=0.5))
        agent.active_chat_id = "chat-2"
        release.set()
        time.sleep(0.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("expected_chat_id"), "chat-1")

    def test_logs_budget_and_keeps_hard_anchors(self):
        agent = _FakeAgent()
        agent.operation_results = [{"ok": True, "detail": "tool done", "blob": "x" * 400}]
        agent.conversation_history.append({"role": "user", "content": "最初需求：修复构建脚本"})
        svc = SessionMemoryService(agent)
        with patch("src.services.session_memory_service.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            messages, _ = svc.build_regular_task_messages("现在执行修复", context="操作上下文-" + ("c" * 300))
        self.assertTrue(logger.info.called)
        user_block = messages[-1]["content"]
        self.assertIn("【关键约束】", user_block)
        self.assertIn("最近的操作结果:", user_block)
        self.assertIn("用户原始需求: 最初需求：修复构建脚本", user_block)
        self.assertIn("用户输入: 现在执行修复", user_block)
        self.assertTrue(hasattr(agent, "_last_context_usage_percent"))
        self.assertGreaterEqual(int(getattr(agent, "_last_context_usage_percent", -1)), 0)

    def test_aggressive_compression_triggers_over_80_percent(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 700}
        agent.token_estimator = lambda s: len(str(s or ""))  # deterministic pressure
        agent.conversation_history.append({"role": "user", "content": "最初需求：完成复杂重构"})
        for i in range(1, 30):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append(
                {"role": role, "content": f"round-{i} " + ("x" * 260)}
            )
        svc = SessionMemoryService(agent)
        _messages, _ = svc.build_regular_task_messages("继续推进", context="ctx-" + ("y" * 1200))
        pre = int(getattr(agent, "_last_context_usage_percent_precompression", 0))
        post = int(getattr(agent, "_last_context_usage_percent", 0))
        self.assertGreater(pre, 80)
        self.assertTrue(bool(getattr(agent, "_last_context_aggressive_compression_applied", False)))
        self.assertLess(post, pre)

    def test_system_prompt_core_not_clipped_under_aggressive_compress(self):
        agent = _FakeAgent()
        agent.params = {"context_window": 700}
        agent.token_estimator = lambda s: len(str(s or ""))
        for i in range(1, 24):
            role = "assistant" if i % 2 == 0 else "user"
            agent.conversation_history.append({"role": role, "content": f"round-{i} " + ("z" * 220)})
        svc = SessionMemoryService(agent)
        messages, _ = svc.build_regular_task_messages("继续推进", context="ctx-" + ("q" * 1200))
        system_content = str(messages[0].get("content") or "")
        self.assertIn("[SYSTEM_PROMPT_END_MARK]", system_content)
        self.assertIn(f"当前 smart-shell 根目录（绝对路径）：{agent._self_repo_root}", system_content)
        self.assertIn(f"当前 config 目录（绝对路径）：{agent.config_dir}", system_content)
        self.assertIn("当前 workspace 名称：Default", system_content)
        self.assertIn(f"当前 workspace 目录（绝对路径）：{agent.ai_workspace_dir}", system_content)


if __name__ == "__main__":
    unittest.main()
