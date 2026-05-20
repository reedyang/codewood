import unittest
from pathlib import Path
import threading
import time
from unittest.mock import MagicMock, patch

from src.services.session_memory_service import SessionMemoryService


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

    def _reload_skills(self):
        return None

    def _compose_system_prompt_snapshot(self, include_tools: bool = True):
        _ = include_tools
        return "SYSTEM PROMPT " + ("X" * 1200) + " [SYSTEM_PROMPT_END_MARK]"

    def _ensure_memory_service(self):
        return False


class SessionMemoryBudgetingTests(unittest.TestCase):
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
