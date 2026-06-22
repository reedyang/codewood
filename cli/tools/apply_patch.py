"""Tool: apply_patch."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "Apply a standard unified diff to a specified text file (patch/git apply format, including ---/+++ and @@ hunks)."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
            },
            "patch": {
                "type": "string",
                "description": "Prefer a standard patch/git apply unified diff, including ---/+++ and at least one @@ ... @@ hunk.",
            },
        },
        "required": [
            "path",
            "patch",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        from ._delegation import delegate_file_shell

        return delegate_file_shell(agent, "apply_patch", params if isinstance(params, dict) else {})
