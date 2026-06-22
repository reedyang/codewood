"""Tool: user_preferences_patch."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class UserPreferencesPatchTool(BaseTool):
    name = "user_preferences_patch"
    description = "Write/update the persistent user preferences file (Markdown + YAML frontmatter). It is injected into system every turn. When the user says to remember something permanently/always/not forget and the content is a stable stance (especially assistant/user forms of address, default tone, or rules), use this tool, usually upsert_section, instead of only memory_add. operation=replace_body replaces the body; operation=upsert_section inserts or replaces a section by level-2 heading. Do not write secrets/tokens."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "replace_body | upsert_section",
            },
            "markdown_body": {
                "type": "string",
                "description": "Required for replace_body: full Markdown body without YAML frontmatter.",
            },
            "section_heading": {
                "type": "string",
                "description": "Required for upsert_section: section title without ##.",
            },
            "section_body": {
                "type": "string",
                "description": "For upsert_section: section body Markdown.",
            },
        },
        "required": [
            "operation",
        ],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        try:
            from pathlib import Path

            from ..core.state import user_preferences_manager as _upm

            op = str(params.get("operation") or "upsert_section").strip().lower()
            if op == "replace_body":
                return _upm.replace_body(
                    Path(agent.config_dir),
                    str(params.get("markdown_body") or ""),
                )
            if op == "upsert_section":
                sh = str(params.get("section_heading") or "").strip()
                if not sh:
                    return {
                        "success": False,
                        "error": "user_preferences_patch upsert_section requires section_heading",
                    }
                sb = str(params.get("section_body") or "")
                return _upm.upsert_section(Path(agent.config_dir), sh, sb)
            return {"success": False, "error": f"unknown operation: {op}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
