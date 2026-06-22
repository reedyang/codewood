"""Transitional delegation shims for tool classes.

During the tool-class migration, each generated tool's ``execute`` delegates to
the legacy handler-group dispatch functions so behavior is unchanged. As tools
are migrated to own their logic, these shims are removed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def delegate_core(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.core_handlers import dispatch_core_tool

    return dispatch_core_tool(agent, name, params)


def delegate_file_shell(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.file_shell_handlers import dispatch_file_shell_tool

    return dispatch_file_shell_tool(agent, name, params)


def delegate_mcp(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.mcp_handlers import dispatch_mcp_tool

    return dispatch_mcp_tool(agent, name, params)


def delegate_memory(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.memory_handlers import dispatch_memory_tool

    return dispatch_memory_tool(agent, name, params)


def delegate_agent_state(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.agent_state_handlers import dispatch_agent_state_tool

    return dispatch_agent_state_tool(agent, name, params)


def delegate_subagent(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ..tooling.handlers.subagent_handlers import dispatch_subagent_tool

    return dispatch_subagent_tool(agent, name, params)


def delegate_skill(agent: Any, name: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # request_skill_prompt is handled specially in the runtime loop, not via
    # tool.execute. This shim should not normally be reached.
    return {"success": False, "error": "request_skill_prompt is handled by the runtime loop"}
