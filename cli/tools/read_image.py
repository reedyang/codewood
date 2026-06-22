"""Tool: read_image."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class ReadImageTool(BaseTool):
    name = "read_image"
    description = "Read an image file and return the AI interpretation of its contents."
    requires_multimodal = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
            },
            "prompt": {
                "type": "string",
            },
        },
        "required": [
            "path",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        file_path = params.get("path")
        prompt = params.get("prompt", "")
        if file_path:
            return agent.action_read_image(file_path, prompt)
        return {"success": False, "error": "missing path"}
