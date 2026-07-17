"""Central registry of built-in tool classes.

`ALL_TOOLS` lists the tool classes in their canonical order (the same order
previously encoded in ``tools.jsonc``). The registry generates the model-facing
tool spec (applying gating) and resolves a tool by name for dispatch.

MCP tools from connected servers are dynamically injected as
``mcp__<server>__<tool>`` specs via :func:`iter_mcp_specs` with simplified
(deferred-loading) schemas.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Type, Set

from ..core.config.model_providers import is_small_model_context_window
from .base import BaseTool

from .shell import ShellTool
from .apply_patch import ApplyPatchTool
from .read import ReadTool
from .project_context_search import ProjectContextSearchTool
from .mcp_read_resource import McpReadResourceTool
from .mcp_list_resources import McpListResourcesTool
from .mcp_list_prompts import McpListPromptsTool
from .mcp_get_prompt import McpGetPromptTool
from .memory_search import MemorySearchTool
from .memory_add import MemoryAddTool
from .memory_delete import MemoryDeleteTool
from .user_preferences_read import UserPreferencesReadTool
from .user_preferences_patch import UserPreferencesPatchTool
from .request_skill_prompt import RequestSkillPromptTool
from .request_user_input import RequestUserInputTool
from .plan import UpdatePlanTool
from .run_subagent import RunSubagentTool
from .browser import (
    BrowserOpenTool,
    BrowserPreviewFileTool,
    BrowserCloseTool,
    BrowserRefreshTool,
    BrowserGetUrlTool,
    BrowserReadDomTool,
    BrowserReadConsoleTool,
    BrowserEvalTool,
)
from .console import (
    ConsoleExecTool,
    ConsoleReadTool,
    ConsoleInfoTool,
)
from .webfetch import WebFetchTool


_MCP_PREFIX_RE = re.compile(r"^mcp__(.+?)__(.+)$")

ALL_TOOLS: List[Type[BaseTool]] = [
    ShellTool,
    ApplyPatchTool,
    ReadTool,
    ProjectContextSearchTool,
    McpReadResourceTool,
    McpListResourcesTool,
    McpListPromptsTool,
    McpGetPromptTool,
    MemorySearchTool,
    MemoryAddTool,
    MemoryDeleteTool,
    UserPreferencesReadTool,
    UserPreferencesPatchTool,
    RequestSkillPromptTool,
    RequestUserInputTool,
    UpdatePlanTool,
    RunSubagentTool,
    BrowserOpenTool,
    BrowserPreviewFileTool,
    BrowserCloseTool,
    BrowserRefreshTool,
    BrowserGetUrlTool,
    BrowserReadDomTool,
    BrowserReadConsoleTool,
    BrowserEvalTool,
    ConsoleExecTool,
    ConsoleReadTool,
    ConsoleInfoTool,
    WebFetchTool,
]

_BY_NAME: Dict[str, Type[BaseTool]] = {t.name: t for t in ALL_TOOLS}
_INSTANCES: Dict[str, BaseTool] = {}

# Tool-name groups derived from class gating flags, kept as module-level
# frozensets for callers that gate tool *injection* at runtime (not just spec
# generation), e.g. prompt_composer and the runtime loop.
IMAGE_INPUT_TOOLS = frozenset(t.name for t in ALL_TOOLS if t.requires_multimodal)
MEMORY_TOOLS = frozenset(t.name for t in ALL_TOOLS if t.name.startswith("memory_"))
#: Tools available only while Plan mode is active (filtered out in Agent mode).
PLAN_MODE_ONLY_TOOLS = frozenset(t.name for t in ALL_TOOLS if t.requires_plan_mode)
#: Tools hidden while Plan mode is active (e.g. update_plan / mutating helpers).
PLAN_MODE_EXCLUDED_TOOLS = frozenset(t.name for t in ALL_TOOLS if t.excluded_in_plan_mode)

#: Tools excluded for small-context-window models (< 64k).
SMALL_MODEL_EXCLUDED_TOOLS: FrozenSet[str] = frozenset()


def _is_mcp_direct_tool(name: str) -> bool:
    return bool(_MCP_PREFIX_RE.match(str(name or "").strip()))


def _parse_mcp_direct_name(name: str) -> Optional[Tuple[str, str]]:
    """Parse ``mcp__server__tool`` → ``(server, tool)`` or return None."""
    m = _MCP_PREFIX_RE.match(str(name or "").strip())
    if m is None:
        return None
    return m.group(1), m.group(2)


def tool_class_by_name(name: str) -> Optional[Type[BaseTool]]:
    return _BY_NAME.get(str(name or "").strip())


def tool_by_name(name: str) -> Optional[BaseTool]:
    """Return a (cached) tool instance by name, or None if unknown.

    For ``mcp__*`` prefixed names a light-weight :class:`McpDispatchTool`
    wrapper is returned so callers can execute it transparently.
    """
    raw = str(name or "").strip()
    if _is_mcp_direct_tool(raw):
        return McpDispatchTool(raw)
    cls = tool_class_by_name(raw)
    if cls is None:
        return None
    inst = _INSTANCES.get(cls.name)
    if inst is None:
        inst = cls()
        _INSTANCES[cls.name] = inst
    return inst


def _gating_flags(agent: Any) -> Dict[str, bool]:
    has_subagents = bool(list(getattr(agent, "subagents", []) or []))
    checker = getattr(agent, "_multimodal_enabled_for_current_model", None)
    multimodal_enabled = True
    if callable(checker):
        try:
            multimodal_enabled = bool(checker())
        except Exception:
            multimodal_enabled = True
    plan_mode = bool(getattr(agent, "_plan_mode_sticky", False))
    gui_enabled = callable(getattr(agent, "_browser_dispatch", None))
    small_model = is_small_model_context_window(
        (getattr(agent, "params", None) or {}).get("context_window")
    )
    return {
        "multimodal_enabled": multimodal_enabled,
        "has_subagents": has_subagents,
        "plan_mode": plan_mode,
        "gui_enabled": gui_enabled,
        "small_model": small_model,
    }


# ---------------------------------------------------------------------------
# Deferred-loading helpers for dynamically injected MCP tool specs
# ---------------------------------------------------------------------------

#: Parameter names that carry authentication / credential values and must
#: never be exposed to the model.  When the MCP tool schema declares such a
#: parameter, it is stripped from the model-facing spec and automatically
#: injected by :class:`McpDispatchTool` from the server connection config
#: (e.g. the ``Authorization`` header of a URL-based server).
_MCP_CREDENTIAL_PARAMS: FrozenSet[str] = frozenset({
    "user_token", "api_key", "access_token", "secret",
})


def _simplify_mcp_schema(schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce a minimal (deferred-loading) parameter schema from a full MCP
    ``inputSchema``.

    The simplified schema preserves only top-level parameter names and the
    ``required`` list, setting every property type to ``"string"``.  Nested
    objects, enums, format constraints, and detailed descriptions are stripped
    to keep token consumption low.  Parameters whose names match
    :const:`_MCP_CREDENTIAL_PARAMS` are removed entirely from the model-facing
    spec; :class:`McpDispatchTool` will inject them at call time.
    """
    simplified: Dict[str, Any] = {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        return simplified
    props = schema.get("properties")
    if not isinstance(props, dict):
        return simplified
    for key in props:
        if key in _MCP_CREDENTIAL_PARAMS:
            continue
        simplified["properties"][key] = {"type": "string"}
    required = schema.get("required")
    if isinstance(required, list) and required:
        simplified["required"] = [
            str(k) for k in required
            if str(k).strip() and k not in _MCP_CREDENTIAL_PARAMS
        ]
    return simplified


def iter_mcp_specs(agent: Any) -> List[Dict[str, Any]]:
    """Return OpenAI-format function specs for every enabled MCP tool across
    all connected servers.

    Each tool receives a simplified (deferred-loading) parameter schema.
    """
    mcp_manager = getattr(agent, "mcp_manager", None)
    if mcp_manager is None:
        return []
    try:
        aggregated = mcp_manager.list_all_tools_aggregated()
    except Exception:
        return []
    specs: List[Dict[str, Any]] = []
    for server_key, tools in aggregated.items():
        if not isinstance(tools, list):
            continue
        for t in tools:
            if not isinstance(t, dict):
                continue
            raw_name = str(t.get("name", "")).strip()
            if not raw_name:
                continue
            prefixed = f"mcp__{server_key}__{raw_name}"
            desc = str(t.get("description", "")).strip()
            if len(desc) > 200:
                desc = desc[:197] + "..."
            full_schema = _extract_schema_from_mcp_tool(t)
            simplified_params = _simplify_mcp_schema(full_schema)
            specs.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": desc,
                    "parameters": simplified_params,
                },
            })
    return specs


def _extract_schema_from_mcp_tool(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract ``inputSchema`` (or legacy ``parameters``) from an MCP tool dict.
    Duplicates the logic of ``_extract_tool_schema`` in manager.py to avoid a
    cross-module import dependency.
    """
    schema = t.get("inputSchema")
    if isinstance(schema, dict):
        return schema
    params = t.get("parameters")
    if isinstance(params, dict):
        return params
    return None


# ---------------------------------------------------------------------------
# McpDispatchTool — lightweight wrapper for dynamically injected MCP tools
# ---------------------------------------------------------------------------

class McpDispatchTool:
    """Non-``BaseTool`` wrapper that routes ``mcp__server__tool`` calls to
    the MCP manager.

    The dispatcher and ``tool_by_name`` callers treat it as a duck-typed tool:
    it has ``execute(agent, args)`` and ``name``.
    """

    def __init__(self, prefixed_name: str) -> None:
        self.name = str(prefixed_name or "").strip()

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ..integrations.mcp import McpError

        params = params if isinstance(params, dict) else {}
        parsed = _parse_mcp_direct_name(self.name)
        if parsed is None:
            return {"success": False, "error": f"Invalid MCP tool name: {self.name}"}
        server_key, tool_name = parsed

        # Map sanitized server_key back to the original server name in config.
        mcp_manager = getattr(agent, "mcp_manager", None)
        if mcp_manager is None:
            return {"success": False, "error": "No MCP manager available"}
        original_server = self._resolve_server_name(mcp_manager, server_key)
        if original_server is None:
            return {"success": False, "error": f"MCP server not found: {server_key}"}

        # Check server state
        try:
            st = mcp_manager.get_status().get("servers", {}).get(original_server, {})
            state_raw = str(st.get("state", "pending") or "pending").lower()
            if state_raw != "success":
                return {
                    "success": False,
                    "error": (
                        f"server={original_server} is not ready (state={state_raw})"
                    ),
                }
        except Exception:
            pass

        # JIT: ensure we have the tool schema cached for validation
        full_schema = mcp_manager.get_tool_schema(original_server, tool_name)
        if full_schema is None:
            try:
                mcp_manager.list_tools(original_server, timeout_s=8.0, use_cache=False)
                full_schema = mcp_manager.get_tool_schema(original_server, tool_name)
            except Exception:
                pass

        # Inject credential params (e.g. user_token) from the server connection
        # config before validation so the model never needs to supply them.
        # Use a private copy for injection/validation/execution so the original
        # ``params`` (the model-supplied args) stays clean — it is later
        # recorded in ``_tool_rounds_raw`` and must not contain internal params.
        internal_params = dict(params)
        self._inject_internal_params(mcp_manager, original_server, tool_name, internal_params, full_schema)

        # Validate arguments against the full schema
        if full_schema is not None:
            errs = _validate_params_against_schema(full_schema, internal_params)
            if errs:
                return {
                    "success": False,
                    "error": (
                        f"Tool '{tool_name}' (server: {original_server}) parameter "
                        f"validation failed: {'; '.join(errs[:8])}. "
                        f"Full schema: {full_schema}"
                    ),
                    "requested_tool": tool_name,
                    "requested_server": original_server,
                }

        try:
            result = mcp_manager.call_tool(
                original_server, tool_name, internal_params, timeout_s=20.0,
            )
            return {
                "success": True,
                "server": original_server,
                "tool": tool_name,
                "result": result,
                "message": f"MCP tool called ({original_server}/{tool_name})",
            }
        except McpError as e:
            return {"success": False, "error": f"MCP tool call failed: {e}"}
        except Exception as e:
            return {"success": False, "error": f"MCP tool call exception: {e}"}

    @staticmethod
    def _resolve_server_name(mcp_manager: Any, sanitized_key: str) -> Optional[str]:
        """Reverse-map a sanitized server key to the original config name."""
        servers = getattr(mcp_manager, "mcp_config", {}).get("mcpServers", {})
        for raw_name in servers:
            if mcp_manager.sanitize_server_name(str(raw_name)) == sanitized_key:
                return str(raw_name)
        return None

    @staticmethod
    def _inject_internal_params(
        mcp_manager: Any,
        server: str,
        tool_name: str,
        params: Dict[str, Any],
        full_schema: Optional[Dict[str, Any]],
    ) -> None:
        """Inject credential params that the tool expects (e.g. ``user_token``)
        from the server connection config into ``params``.

        Currently supports URL-based servers with an ``Authorization`` header:
        the Bearer token value is injected for any credential parameter that
        the tool's schema declares as a property.  Params that the model
        already supplied with a non-empty value are left untouched.
        """
        if not isinstance(full_schema, dict):
            return
        props = full_schema.get("properties")
        if not isinstance(props, dict):
            return
        # Determine which internal params the tool actually expects.
        expected = [k for k in _MCP_CREDENTIAL_PARAMS if k in props]
        if not expected:
            return

        # Read Authorization header from the server config.
        servers = getattr(mcp_manager, "mcp_config", {}).get("mcpServers", {})
        conf = servers.get(server)
        if not isinstance(conf, dict):
            return
        auth_header = str(conf.get("headers", {}).get("Authorization", "") or "").strip()
        if not auth_header:
            return
        # Strip the "Bearer " prefix (case-insensitive).
        token = re.sub(r"(?i)^bearer\s+", "", auth_header).strip()
        if not token:
            return

        for key in expected:
            existing = params.get(key)
            # Only inject when the model did NOT provide a usable value.
            if existing is not None and (not isinstance(existing, str) or existing.strip()):
                continue
            params[key] = token


def _validate_params_against_schema(
    schema: Dict[str, Any], params: Dict[str, Any]
) -> List[str]:
    """Light-weight JSON schema validation for MCP tool parameters.

    Returns a list of human-readable error messages (empty = valid).
    """
    errs: List[str] = []
    if not isinstance(schema, dict):
        return errs
    if not isinstance(params, dict):
        return ["arguments must be an object"]

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            val = params.get(str(key))
            if val is None:
                errs.append(f"missing required parameter '{key}'")
            elif isinstance(val, str) and not val.strip():
                errs.append(f"required parameter '{key}' is empty")

    props = schema.get("properties")
    if not isinstance(props, dict):
        return errs

    for key, val in params.items():
        if key not in props:
            continue
        prop_schema = props[key]
        if not isinstance(prop_schema, dict):
            continue
        prop_type = str(prop_schema.get("type", "")).strip()
        if not prop_type or prop_type == "object":
            continue
        if prop_type == "string":
            if val is not None and not isinstance(val, (str, type(None))):
                errs.append(f"parameter '{key}' expects a string, got {type(val).__name__}")
        elif prop_type == "number":
            if val is not None and not isinstance(val, (int, float)):
                errs.append(f"parameter '{key}' expects a number, got {type(val).__name__}")
        elif prop_type == "integer":
            if val is not None and not isinstance(val, int):
                errs.append(f"parameter '{key}' expects an integer, got {type(val).__name__}")
        elif prop_type == "boolean":
            if val is not None and not isinstance(val, bool):
                errs.append(f"parameter '{key}' expects a boolean, got {type(val).__name__}")
        elif prop_type == "array":
            if val is not None and not isinstance(val, list):
                errs.append(f"parameter '{key}' expects an array, got {type(val).__name__}")
    return errs


# ---------------------------------------------------------------------------
# Public spec generation entry point (called by prompt_composer / agent)
# ---------------------------------------------------------------------------

def iter_specs(agent: Any) -> List[Dict[str, Any]]:
    """Return the gated, ordered list of tool specs for the given agent.

    Built-in tools are emitted first (subject to gating flags), followed by
    dynamically injected MCP tool specs from connected servers.
    """
    flags = _gating_flags(agent)
    small_model = flags.pop("small_model", False)
    specs: List[Dict[str, Any]] = []
    for cls in ALL_TOOLS:
        if not cls.is_available(**flags):
            continue
        if small_model and cls.name in SMALL_MODEL_EXCLUDED_TOOLS:
            continue
        specs.append(cls.schema())
    # Append dynamic MCP tool specs (always injected; small-model gating is
    # skipped because MCP tools are essential for server interaction).
    try:
        mcp_specs = iter_mcp_specs(agent)
        specs.extend(mcp_specs)
    except Exception:
        pass
    return specs
