"""Legacy tool execution fallback.

With tools migrated to registry-backed classes, this fallback only runs when
``ToolDispatcher.dispatch`` returns ``None`` (an unknown action). It resolves
known tools through the registry, handles the non-model ``execution_policy_set``
action, and otherwise surfaces the skill-mistaken-as-tool guard.
"""

from __future__ import annotations

from typing import Any, Dict


def execute_tool_call_legacy(agent: Any, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool command (legacy fallback)."""
    self = agent
    action = (tool_name or "").strip()
    params = arguments if isinstance(arguments, dict) else {}

    from ..tools.registry import tool_by_name

    tool = tool_by_name(action)
    if tool is not None:
        return tool.execute(self, params)

    # Non-model actions (e.g. execution_policy_set) still flow through the
    # agent_state handler group.
    from .handlers.agent_state_handlers import dispatch_agent_state_tool

    delegated = dispatch_agent_state_tool(self, action, params)
    if delegated is not None:
        return delegated

    # Model often emits {"tool":"<skill_id>"} (e.g. weather) — skill folders are not tool names.
    sid_guess = (action or "").strip()
    if sid_guess and self.skills:
        for s in self.skills:
            if str(getattr(s, "skill_id", "")).strip().lower() == sid_guess.lower():
                canon = str(getattr(s, "skill_id", "") or sid_guess)
                return {
                    "success": False,
                    "error": (
                        f"\"{sid_guess}\" is the directory name (skill_id) of a loaded Agent Skill, not a built-in tool."
                        f' Call {{"tool":"request_skill_prompt","args":{{"skill_id":"{canon}"}}}} first'
                        " to inject the full SKILL text, then follow its instructions to execute shell or other allowed tools."
                    ),
                    "mistake_skill_as_tool": True,
                    "skill_id": canon,
                }

    return {"success": False, "error": "Unknown operation type"}
