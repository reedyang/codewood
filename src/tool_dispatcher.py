from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .tool_handlers.core_handlers import dispatch_core_tool
from .tool_handlers.file_shell_handlers import dispatch_file_shell_tool
from .tool_handlers.mcp_handlers import dispatch_mcp_tool
from .tool_handlers.memory_handlers import dispatch_memory_tool
from .tool_handlers.agent_state_handlers import dispatch_agent_state_tool


class ToolDispatcher:
    """New modular tool dispatcher. Returns None when action should fallback to legacy."""

    def __init__(self, agent: Any, legacy_executor: Callable[[str, Dict[str, Any]], Dict[str, Any]]):
        self._agent = agent
        self._legacy_executor = legacy_executor

    def dispatch(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action = (tool_name or "").strip()
        args = arguments if isinstance(arguments, dict) else {}

        core = dispatch_core_tool(self._agent, action, args)
        if core is not None:
            return core

        file_shell = dispatch_file_shell_tool(self._agent, action, args)
        if file_shell is not None:
            return file_shell

        mcp = dispatch_mcp_tool(self._agent, action, args)
        if mcp is not None:
            return mcp

        memory = dispatch_memory_tool(self._agent, action, args)
        if memory is not None:
            return memory

        agent_state = dispatch_agent_state_tool(self._agent, action, args)
        if agent_state is not None:
            return agent_state

        return None

    def dispatch_or_fallback(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self.dispatch(tool_name, arguments)
        if result is not None:
            return result
        return self._legacy_executor(tool_name, arguments)
