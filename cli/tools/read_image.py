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
        from ._delegation import delegate_file_shell

        return delegate_file_shell(agent, "read_image", params if isinstance(params, dict) else {})
