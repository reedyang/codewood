"""Tool: user_preferences_read."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class UserPreferencesReadTool(BaseTool):
    name = "user_preferences_read"
    description = "Read the persistent user preferences file (user_preferences.md). Optional max_chars truncates the content."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Optional maximum body characters; default 16000.",
            },
        },
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        try:
            from pathlib import Path

            from ..core.state import user_preferences_manager as _upm

            meta, body = _upm.read_body(Path(agent.config_dir))
            lim = int(params.get("max_chars") or 16000)
            truncated = len(body) > lim
            text = body if not truncated else body[:lim] + "..."
            return {
                "success": True,
                "meta": meta,
                "body": text,
                "truncated": truncated,
                "path": str(Path(agent.config_dir) / _upm.DEFAULT_FILENAME),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
