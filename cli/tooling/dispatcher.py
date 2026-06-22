from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .handlers.agent_state_handlers import dispatch_agent_state_tool


class ToolDispatcher:
    """New modular tool dispatcher. Returns None when action should fallback to legacy."""

    def __init__(self, agent: Any, legacy_executor: Callable[[str, Dict[str, Any]], Dict[str, Any]]):
        self._agent = agent
        self._legacy_executor = legacy_executor

    def dispatch(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action = (tool_name or "").strip()
        args = arguments if isinstance(arguments, dict) else {}

        from ..tools.registry import tool_by_name

        tool = tool_by_name(action)
        if tool is not None:
            return tool.execute(self._agent, args)

        # Actions that are not model-facing tools (e.g. execution_policy_set)
        # are still handled by the legacy handler groups.
        agent_state = dispatch_agent_state_tool(self._agent, action, args)
        if agent_state is not None:
            return agent_state

        return None

    def dispatch_or_fallback(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self.dispatch(tool_name, arguments)
        if result is not None:
            return result
        return self._legacy_executor(tool_name, arguments)
