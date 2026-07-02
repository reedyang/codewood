"""Base class shared by all built-in tools.

Each built-in tool is modeled as a :class:`BaseTool` subclass living in its own
file under ``cli/tools/``. A tool carries its own schema (name, description,
parameters) plus optional gating metadata, and implements :meth:`execute` to
run the call against the agent. The :mod:`cli.tools.registry` module exposes the
ordered set of tools; spec generation and dispatch are driven from there.
"""

from __future__ import annotations

from typing import Any, Dict


class BaseTool:
    """One built-in tool: schema + execution.

    Subclasses set :attr:`name`, :attr:`description`, :attr:`parameters` and
    implement :meth:`execute`. Gating flags let the registry omit a tool from
    the model-visible spec when the corresponding capability is unavailable.
    """

    #: Tool name as exposed to the model (must be unique across the registry).
    name: str = ""

    #: One-line, model-facing description of what the tool does.
    description: str = ""

    #: JSON schema for the tool's arguments (the ``function.parameters`` block).
    parameters: Dict[str, Any] = {}

    #: Only expose this tool when the model supports multimodal/image input.
    requires_multimodal: bool = False

    #: Only expose this tool when at least one sub-agent is configured.
    requires_subagents: bool = False

    #: Only expose this tool while Plan (collaboration) mode is active.
    requires_plan_mode: bool = False

    #: Hide this tool while Plan mode is active (e.g. mutating/checklist tools).
    excluded_in_plan_mode: bool = False

    #: Only expose this tool when running under the desktop GUI (a frontend
    #: WebView is available to service the request, e.g. the embedded browser).
    requires_gui: bool = False

    @classmethod
    def schema(cls) -> Dict[str, Any]:
        """Return the OpenAI-style function spec for this tool."""
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.parameters,
            },
        }

    @classmethod
    def is_available(
        cls,
        *,
        multimodal_enabled: bool,
        has_subagents: bool,
        plan_mode: bool = False,
        gui_enabled: bool = False,
    ) -> bool:
        """Whether this tool should appear in the model-visible spec."""
        if cls.requires_multimodal and not multimodal_enabled:
            return False
        if cls.requires_subagents and not has_subagents:
            return False
        if cls.requires_plan_mode and not plan_mode:
            return False
        if cls.excluded_in_plan_mode and plan_mode:
            return False
        if cls.requires_gui and not gui_enabled:
            return False
        return True

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run the tool call. Subclasses must override."""
        raise NotImplementedError
