"""Tool: read.

Reads files from the local filesystem. For text files returns the content with
line numbers (line_number: content). For image files returns an AI interpretation
of the image. For directories returns a listing of entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import BaseTool


def action_read(agent: Any, path: str, offset: int = 0, limit: int = 2000, prompt: str = "") -> Dict[str, Any]:
    try:
        abs_path = Path(path)
        if not abs_path.is_absolute():
            p1 = agent.work_directory / path
            p_temp = agent.ai_workspace_temp_dir / path
            p2 = agent.workspace_config_dir / path
            if p1.exists():
                abs_path = p1
            elif p_temp.exists():
                abs_path = p_temp
            elif p2.exists():
                abs_path = p2
            else:
                abs_path = p1

        if not abs_path.exists():
            return {"success": False, "error": f"File '{path}' does not exist"}

        # ---------- directories ----------
        if abs_path.is_dir():
            entries: list[str] = []
            try:
                for entry in sorted(abs_path.iterdir()):
                    suffix = "/" if entry.is_dir() else ""
                    entries.append(entry.name + suffix)
            except PermissionError:
                return {"success": False, "error": f"Permission denied: {path}"}
            return {"success": True, "content": "\n".join(entries), "file": str(abs_path)}

        if not abs_path.is_file():
            return {"success": False, "error": f"'{path}' is not a file"}

        # ---------- images ----------
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg", ".ico"}
        if abs_path.suffix.lower() in _IMAGE_EXTS:
            image_task_context = f"Image file path: {str(abs_path)}"
            image_user_prompt = prompt if prompt else "Please read this image and describe its contents."
            analysis = agent.call_ai(
                image_user_prompt,
                context=image_task_context,
                image_path=str(abs_path),
                stream=False,
            )
            return {"success": True, "content": str(analysis or ""), "file": str(abs_path)}

        # ---------- text files ----------
        try:
            content = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"success": False, "error": f"'{path}' is not a text file (binary content)"}

        lines = content.split("\n")
        start = max(0, offset)
        if limit and limit > 0:
            end = start + limit
            sliced = lines[start:end]
        else:
            sliced = lines[start:]

        result = "\n".join(f"{start + i + 1}: {line}" for i, line in enumerate(sliced))
        return {"success": True, "content": result, "file": str(abs_path)}
    except Exception as e:
        return {"success": False, "error": f"Read failed: {str(e)}"}


class ReadTool(BaseTool):
    name = "read"
    description = (
        "Read a file from the local filesystem. For text files, returns the "
        "content with line numbers. For image files, returns an AI "
        "interpretation of the image. For directories, returns a listing of "
        "entries."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The absolute path to the file or directory to read"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default 0)"},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default 2000)"},
            "prompt": {"type": "string", "description": "When reading an image, what aspect or detail to focus on (optional)"},
        },
        "required": ["path"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        file_path = params.get("path")
        if not file_path:
            return {"success": False, "error": "missing path"}
        offset = int(params.get("offset", 0) or 0)
        limit = int(params.get("limit", 2000) or 2000)
        prompt = str(params.get("prompt", "") or "")
        return action_read(agent, file_path, offset=offset, limit=limit, prompt=prompt)
