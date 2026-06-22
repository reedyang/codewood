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
        params = params if isinstance(params, dict) else {}
        file_path = params.get("path")
        patch = params.get("patch")
        if file_path and patch is not None:
            patch_cmd = {"action": "apply_patch", "params": {"path": file_path}}
            confirmed = agent._freedom_auto_confirm(patch_cmd)
            return agent.action_apply_unified_patch(
                file_path=file_path, patch=str(patch), confirmed=confirmed
            )
        missing = []
        if not file_path:
            missing.append("path")
        if patch is None:
            missing.append("patch")
        missing_text = ", ".join(missing) if missing else "path/patch"
        return {
            "success": False,
            "error": f"apply_patch requires both path and patch; missing: {missing_text}",
        }
