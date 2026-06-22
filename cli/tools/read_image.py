"""Tool: read_image.

Hosts action_read_image (formerly part of cli/actions/filesystem_actions.py)
plus the ReadImageTool class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import BaseTool


def action_read_image(agent: Any, file_path: str, prompt: str = "") -> Dict[str, Any]:
    try:
        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            p1 = agent.work_directory / file_path
            p_temp = agent.ai_workspace_temp_dir / file_path
            p2 = agent.workspace_config_dir / file_path
            if p1.is_file():
                abs_path = p1
            elif p_temp.is_file():
                abs_path = p_temp
            elif p2.is_file():
                abs_path = p2
            else:
                abs_path = p1
        if not abs_path.exists():
            return {"success": False, "error": f"Image file '{file_path}' does not exist"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' is not a file"}
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"]
        if abs_path.suffix.lower() not in image_exts:
            return {"success": False, "error": f"Unsupported file format: {abs_path.suffix}"}
        image_task_context = f"Image file path: {str(abs_path)}"
        image_user_prompt = prompt if prompt else "Please read this image first, then continue with the current task."
        # Force a non-streaming call so we receive the complete analysis as a
        # string. Without ``stream=False`` a streaming-enabled model returns an
        # unconsumed stream result object, which is not JSON-serializable and
        # breaks downstream tool-result handling.
        analysis = agent.call_ai(
            image_user_prompt,
            context=image_task_context,
            image_path=str(abs_path),
            stream=False,
        )
        return {"success": True, "analysis": str(analysis or ""), "file": str(abs_path)}
    except Exception as e:
        return {"success": False, "error": f"Image read failed: {str(e)}"}


class ReadImageTool(BaseTool):
    name = "read_image"
    description = "Read an image file and return the AI interpretation of its contents."
    requires_multimodal = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["path"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        file_path = params.get("path")
        prompt = params.get("prompt", "")
        if file_path:
            return action_read_image(agent, file_path, prompt)
        return {"success": False, "error": "missing path"}
