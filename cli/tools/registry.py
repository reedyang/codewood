"""Central registry of built-in tool classes.

`ALL_TOOLS` lists the tool classes in their canonical order (the same order
previously encoded in ``tools.jsonc``). The registry generates the model-facing
tool spec (applying gating) and resolves a tool by name for dispatch.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .base import BaseTool

from .shell import ShellTool
from .apply_patch import ApplyPatchTool
from .read_image import ReadImageTool
from .project_context_search import ProjectContextSearchTool
from .mcp_server_info import McpServerInfoTool
from .mcp_reload_config import McpReloadConfigTool
from .mcp_disable_tools import McpDisableToolsTool
from .mcp_enable_tools import McpEnableToolsTool
from .mcp_list_disabled_tools import McpListDisabledToolsTool
from .mcp_list_tools import McpListToolsTool
from .mcp_call_tool import McpCallToolTool
from .mcp_call_tool_batch import McpCallToolBatchTool
from .mcp_list_resources import McpListResourcesTool
from .mcp_read_resource import McpReadResourceTool
from .mcp_list_resource_templates import McpListResourceTemplatesTool
from .mcp_list_prompts import McpListPromptsTool
from .mcp_get_prompt import McpGetPromptTool
from .mcp_sampling_create_message import McpSamplingCreateMessageTool
from .mcp_completion_complete import McpCompletionCompleteTool
from .mcp_status import McpStatusTool
from .mcp_status_refresh import McpStatusRefreshTool
from .mcp_reconnect import McpReconnectTool
from .memory_search import MemorySearchTool
from .memory_add import MemoryAddTool
from .memory_list import MemoryListTool
from .memory_stats import MemoryStatsTool
from .memory_delete import MemoryDeleteTool
from .user_preferences_read import UserPreferencesReadTool
from .user_preferences_patch import UserPreferencesPatchTool
from .request_skill_prompt import RequestSkillPromptTool
from .ask_more_info import AskMoreInfoTool
from .update_plan import UpdatePlanTool
from .run_subagent import RunSubagentTool


ALL_TOOLS: List[Type[BaseTool]] = [
    ShellTool,
    ApplyPatchTool,
    ReadImageTool,
    ProjectContextSearchTool,
    McpServerInfoTool,
    McpReloadConfigTool,
    McpDisableToolsTool,
    McpEnableToolsTool,
    McpListDisabledToolsTool,
    McpListToolsTool,
    McpCallToolTool,
    McpCallToolBatchTool,
    McpListResourcesTool,
    McpReadResourceTool,
    McpListResourceTemplatesTool,
    McpListPromptsTool,
    McpGetPromptTool,
    McpSamplingCreateMessageTool,
    McpCompletionCompleteTool,
    McpStatusTool,
    McpStatusRefreshTool,
    McpReconnectTool,
    MemorySearchTool,
    MemoryAddTool,
    MemoryListTool,
    MemoryStatsTool,
    MemoryDeleteTool,
    UserPreferencesReadTool,
    UserPreferencesPatchTool,
    RequestSkillPromptTool,
    AskMoreInfoTool,
    UpdatePlanTool,
    RunSubagentTool,
]

_BY_NAME: Dict[str, Type[BaseTool]] = {t.name: t for t in ALL_TOOLS}
_INSTANCES: Dict[str, BaseTool] = {}


def tool_class_by_name(name: str) -> Optional[Type[BaseTool]]:
    return _BY_NAME.get(str(name or "").strip())


def tool_by_name(name: str) -> Optional[BaseTool]:
    """Return a (cached) tool instance by name, or None if unknown."""
    cls = tool_class_by_name(name)
    if cls is None:
        return None
    inst = _INSTANCES.get(cls.name)
    if inst is None:
        inst = cls()
        _INSTANCES[cls.name] = inst
    return inst


def _gating_flags(agent: Any) -> Dict[str, bool]:
    mcp_enabled = bool(getattr(agent, "mcp_tools_enabled", False))
    has_subagents = bool(list(getattr(agent, "subagents", []) or []))
    checker = getattr(agent, "_multimodal_enabled_for_current_model", None)
    multimodal_enabled = True
    if callable(checker):
        try:
            multimodal_enabled = bool(checker())
        except Exception:
            multimodal_enabled = True
    return {
        "mcp_enabled": mcp_enabled,
        "multimodal_enabled": multimodal_enabled,
        "has_subagents": has_subagents,
    }


def iter_specs(agent: Any) -> List[Dict[str, Any]]:
    """Return the gated, ordered list of tool specs for the given agent."""
    flags = _gating_flags(agent)
    specs: List[Dict[str, Any]] = []
    for cls in ALL_TOOLS:
        if cls.is_available(**flags):
            specs.append(cls.schema())
    return specs
