"""Tool: shell."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class ShellTool(BaseTool):
    name = "shell"
    description = "Execute a system shell command in the current working directory."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
            },
            "force": {
                "type": "boolean",
            },
        },
        "required": [
            "command",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_file_shell

        return delegate_file_shell(agent, "shell", params if isinstance(params, dict) else {})
