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
    _call_desc = ""
    try:
        _rp = Path(path)
        if not _rp.is_absolute():
            _rp = (agent.workspace_root / path) if hasattr(agent, "workspace_root") and agent.workspace_root else (agent.work_directory / path)
        try:
            _rel = _rp.relative_to(agent.workspace_root)
        except Exception:
            _rel = _rp
        _call_desc = f"Read {_rel} [offset={offset}, limit={limit}]"
    except Exception:
        _call_desc = f"Read {path}"
    try:
        abs_path = Path(path)
        if not abs_path.is_absolute():
            ws_root = getattr(agent, "workspace_root", None) or agent.work_directory
            abs_path = ws_root / path

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
            return {"success": True, "content": "\n".join(entries), "file": str(abs_path), "call": _call_desc}

        if not abs_path.is_file():
            return {"success": False, "error": f"'{path}' is not a file"}

        # ---------- images ----------
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg", ".ico"}
        if abs_path.suffix.lower() in _IMAGE_EXTS:
            _call_desc = f"Read {_rel}"
            image_task_context = f"Image file path: {str(abs_path)}"
            image_user_prompt = prompt if prompt else "Please read this image and describe its contents."
            analysis = agent.call_ai(
                image_user_prompt,
                context=image_task_context,
                image_path=str(abs_path),
                stream=False,
                record_history_override=False,
            )
            # Record user prompt as internal-only for API cache prefix matching.
            agent._append_chat_message("user", image_user_prompt, _internal=True, api_content=image_user_prompt)
            return {"success": True, "content": str(analysis or ""), "file": str(abs_path), "call": _call_desc}

        # ---------- text files ----------
        try:
            content = abs_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return {"success": False, "error": f"'{path}' is not a text file (binary content)"}

        lines = content.split("\n")
        if offset < 0:
            start = max(0, len(lines) + offset)
        else:
            start = offset
        if limit and limit > 0:
            end = start + limit
            sliced = lines[start:end]
        else:
            sliced = lines[start:]

        result = "\n".join(f"{start + i + 1}: {line}" for i, line in enumerate(sliced))
        return {"success": True, "content": result, "file": str(abs_path), "call": _call_desc}
    except Exception as e:
        return {"success": False, "error": f"Read failed: {str(e)}"}


class ReadTool(BaseTool):
    name = "read"
    description = "Read a file, directory, or image from the local filesystem."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The absolute path to the file or directory to read"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default 0). Negative values read from the end (e.g. -10 starts from the 10th last line)"},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default 100)"},
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
        limit = int(params.get("limit", 100) or 100)
        prompt = str(params.get("prompt", "") or "")
        return action_read(agent, file_path, offset=offset, limit=limit, prompt=prompt)
