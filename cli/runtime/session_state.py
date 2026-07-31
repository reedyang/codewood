"""Per-chat execution state so multiple chat loops can run concurrently.

Historically the :class:`Agent` kept the live conversation and per-turn
bookkeeping as single-valued instance attributes. To let several chats run
their own ``run_agent_loop`` at the same time (GUI "serve" mode), those fields
are moved into a per-chat :class:`SessionState` held in a registry on the
agent. Each worker thread is *bound* to a chat id; the agent's per-session
properties resolve to that thread's bound session. HTTP handler threads (which
run on a different thread than the agent loop under ``ThreadingHTTPServer``)
temporarily bind to the chat they need to read via ``Agent._session_scope``.

The registry is keyed by chat id and guarded by a re-entrant lock. The TUI and
any single-loop path are unaffected: there is exactly one bound chat and one
session, so behavior matches the previous single-valued attributes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class SessionState:
    """Mutable per-chat execution state for one running conversation."""

    __slots__ = (
        "conversation_history",
        "active_chat_id",
        "active_chat_name",
        "operation_results",
        "active_chat_plan",
        "active_chat_plan_pending",
        "session_summary_llm",
        "session_summary_rolling",
        "last_llm_summary_pair_count",
        "last_context_usage_percent",
        "last_context_input_tokens",
        "last_context_window",
        "in_task_execution",
        "queued_user_input",
        "last_shell_output_visible_lines",
        "tool_call_feedback_interstitial_lines",
        "force_current_input_as_requirement_once",
        "last_cancelled_task",
        "call_provider",
        "call_model_name",
        "call_model_params",
        "call_openai_conf",
        "call_model_set",
        "reasoning_effort",
        # Session-level injection tracking: which skill bodies and MCP prompt
        # contents have been injected into this chat's history at least once.
        "session_injected_skills",
        "session_injected_mcp_prompts",
        # Per-chat interrupt flags. In serve mode a GUI interrupt (stop button,
        # ``/chat edit``) targets ONE chat: the request lands on that chat's
        # session, so only that chat's loop thread consumes it and a different
        # chat's running task / subprocess is never aborted. The agent-global
        # ``_task_interrupt_requested`` / ``_process_interrupt_requested`` flags
        # remain the TUI / ESC legacy path and are untouched.
        "task_interrupt_requested",
        "process_interrupt_requested",
    )

    def __init__(self) -> None:
        self.conversation_history: List[Any] = []
        self.active_chat_id: str = ""
        self.active_chat_name: str = "New Chat"
        self.operation_results: List[Any] = []
        self.active_chat_plan: Optional[Dict[str, Any]] = None
        self.active_chat_plan_pending: bool = False
        self.session_summary_llm: str = ""
        self.session_summary_rolling: str = ""
        self.last_llm_summary_pair_count: int = 0
        self.last_context_usage_percent: int = 0
        self.last_context_input_tokens: int = 0
        self.last_context_window: int = 0
        self.in_task_execution: bool = False
        self.queued_user_input: Optional[str] = None
        self.last_shell_output_visible_lines: int = 0
        self.tool_call_feedback_interstitial_lines: int = 0
        self.force_current_input_as_requirement_once: bool = False
        self.last_cancelled_task: str = ""
        # Per-chat model snapshot. The agent's ``provider``/``model_name`` are a
        # single shared global; pinning the selection a chat was activated with
        # onto its session lets that chat's ``call_ai`` keep using its own model
        # even after a concurrent chat's activation/switch clobbers the globals.
        self.call_provider: Any = None
        self.call_model_name: str = ""
        self.call_model_params: Any = None
        self.call_openai_conf: Any = None
        self.call_model_set: bool = False
        # Selected reasoning effort level for this chat ("" = none/unsupported).
        self.reasoning_effort: str = ""
        self.session_injected_skills: set = set()
        self.session_injected_mcp_prompts: set = set()
        self.task_interrupt_requested: bool = False
        self.process_interrupt_requested: bool = False


# Maps each public Agent attribute name to the SessionState slot backing it.
# Names with a leading underscore on the agent drop it on the session slot.
SESSION_FIELD_MAP: Dict[str, str] = {
    "conversation_history": "conversation_history",
    "active_chat_id": "active_chat_id",
    "active_chat_name": "active_chat_name",
    "operation_results": "operation_results",
    "_active_chat_plan": "active_chat_plan",
    "_active_chat_plan_pending": "active_chat_plan_pending",
    "_session_summary_llm": "session_summary_llm",
    "_session_summary_rolling": "session_summary_rolling",
    "_last_llm_summary_pair_count": "last_llm_summary_pair_count",
    "_last_context_usage_percent": "last_context_usage_percent",
    "_last_context_input_tokens": "last_context_input_tokens",
    "_last_context_window": "last_context_window",
    "_in_task_execution": "in_task_execution",
    "_queued_user_input": "queued_user_input",
    "_last_shell_output_visible_lines": "last_shell_output_visible_lines",
    "_tool_call_feedback_interstitial_lines": "tool_call_feedback_interstitial_lines",
    "_force_current_input_as_requirement_once": "force_current_input_as_requirement_once",
    "_last_cancelled_task": "last_cancelled_task",
    "reasoning_level": "reasoning_effort",
    "_session_injected_skills": "session_injected_skills",
    "_session_injected_mcp_prompts": "session_injected_mcp_prompts",
}


def install_session_properties(agent_cls: type) -> None:
    """Install per-session properties on the Agent class (idempotent).

    Each mapped attribute becomes a property whose getter/setter resolve to the
    calling thread's bound :class:`SessionState` via ``agent._session()``. This
    is what makes ``self.conversation_history`` (and friends) per-chat without
    touching the thousands of call sites that read/write those attributes.
    """
    if getattr(agent_cls, "_session_props_installed", False):
        return
    for agent_attr, slot in SESSION_FIELD_MAP.items():

        def _make(slot_name: str):
            def getter(self):  # type: ignore[no-untyped-def]
                return getattr(self._session(), slot_name)

            def setter(self, value):  # type: ignore[no-untyped-def]
                setattr(self._session(), slot_name, value)

            return property(getter, setter)

        setattr(agent_cls, agent_attr, _make(slot))
    agent_cls._session_props_installed = True  # type: ignore[attr-defined]

