"""Builtin command implementations."""

from __future__ import annotations

from typing import Any, Dict

from .mcp_reload_config import run_mcp_reload_config
from .mcp_status import run_mcp_status
from .mcp_status_refresh import run_mcp_status_refresh
from .mcp_reconnect import run_mcp_reconnect
from .mcp_server_info import run_mcp_server_info
from .mcp_disable_tools import run_mcp_disable_tools
from .mcp_enable_tools import run_mcp_enable_tools
from .mcp_list_disabled_tools import run_mcp_list_disabled_tools
from .mcp_list_tools import run_mcp_list_tools
from .memory_list import run_memory_list
from .memory_stats import run_memory_stats

_COMMAND_MAP: Dict[str, Any] = {
    "mcp_reload_config": run_mcp_reload_config,
    "mcp_status": run_mcp_status,
    "mcp_status_refresh": run_mcp_status_refresh,
    "mcp_reconnect": run_mcp_reconnect,
    "mcp_server_info": run_mcp_server_info,
    "mcp_disable_tools": run_mcp_disable_tools,
    "mcp_enable_tools": run_mcp_enable_tools,
    "mcp_list_disabled_tools": run_mcp_list_disabled_tools,
    "mcp_list_tools": run_mcp_list_tools,
    "memory_list": run_memory_list,
    "memory_stats": run_memory_stats,
}


def is_command(name: str) -> bool:
    return name in _COMMAND_MAP


def run_command(agent: Any, command_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    fn = _COMMAND_MAP.get(command_name)
    if fn is None:
        return {"success": False, "error": f"unknown command: {command_name}"}
    return fn(agent, args)


__all__ = [
    "is_command",
    "run_command",
    "run_mcp_reload_config",
    "run_mcp_status",
    "run_mcp_status_refresh",
    "run_mcp_reconnect",
    "run_mcp_server_info",
    "run_mcp_disable_tools",
    "run_mcp_enable_tools",
    "run_mcp_list_disabled_tools",
    "run_mcp_list_tools",
    "run_memory_list",
    "run_memory_stats",
]
