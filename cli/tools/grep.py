"""Tool: grep.

Search file contents by regular expression within the workspace. Uses ripgrep
(rg) under the hood. Returns matched files with line numbers and content previews.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseTool
from .shell import _workspace_rg_executable_path


def _find_rg(agent: Any) -> Optional[Path]:
    """Resolve the ripgrep binary path."""
    return _workspace_rg_executable_path(agent)


def action_grep(
    agent: Any,
    pattern: str,
    path: str = ".",
    include: str = "",
    limit: int = 50,
) -> Dict[str, Any]:
    try:
        _rg = _find_rg(agent)
        if _rg is None:
            return {"success": False, "error": "ripgrep (rg) is not available. It may still be downloading."}

        # Resolve search directory
        if not path or not str(path).strip():
            path = "."
        search_dir = Path(path)
        if not search_dir.is_absolute():
            ws_root = getattr(agent, "workspace_root", None) or agent.workspace_root
            search_dir = ws_root / path
        search_dir = search_dir.resolve()

        if not search_dir.exists():
            return {"success": False, "error": f"Directory not found: {path}"}

        target = str(search_dir)

        # Build command: rg --no-heading --with-filename --line-number --color never
        cmd = [
            str(_rg),
            "--no-heading",
            "--with-filename",
            "--line-number",
            "--color", "never",
            "--no-messages",
        ]

        if include:
            cmd.extend(["--glob", include])

        if limit and limit > 0:
            cmd.extend(["-m", str(limit)])

        cmd.append(pattern)
        cmd.append(target)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(search_dir),
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"grep timed out searching '{path}'"}

        stdout = (result.stdout or "").strip()
        exit_code = result.returncode

        # rg exit code 1 = no matches
        if exit_code == 1 and not stdout:
            return {
                "success": True,
                "content": "No files found",
                "matches": 0,
            }

        if exit_code != 0 and not stdout:
            return {"success": False, "error": f"rg exited with code {exit_code}: {result.stderr}"}

        lines = stdout.splitlines()
        match_count = len(lines)

        # Build concise model-friendly output grouped by file
        output_parts: List[str] = [f"Found {match_count} matches"]
        current_file = ""

        for line_str in lines:
            # rg --no-heading --with-filename --line-number output: filepath:lineno:text
            parts = line_str.split(":", 2)
            if len(parts) < 3:
                output_parts.append(line_str)
                continue

            file_path = parts[0]
            try:
                lineno = int(parts[1])
            except ValueError:
                output_parts.append(line_str)
                continue
            text = parts[2]

            try:
                rel_path = str(Path(file_path).relative_to(search_dir))
            except ValueError:
                rel_path = file_path

            if current_file != rel_path:
                if current_file:
                    output_parts.append("")
                current_file = rel_path
                output_parts.append(f"{rel_path}:")
            output_parts.append(f"  Line {lineno}: {text}")

        return {
            "success": True,
            "content": "\n".join(output_parts),
            "matches": match_count,
            "pattern": pattern,
        }

    except Exception as e:
        return {"success": False, "error": f"grep failed: {e}"}


class GrepTool(BaseTool):
    name = "grep"
    description = (
        "Search file contents by regular expression within the workspace. "
        "Returns matched files with line numbers and content previews."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regex pattern to search for in file contents",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative path from workspace root). Defaults to the workspace root.",
            },
            "include": {
                "type": "string",
                "description": 'File glob to filter results (e.g. "*.js" or "*.{ts,tsx}")',
            },
            "limit": {
                "type": "integer",
                "description": "Maximum matches to return (default: 50)",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        pattern = str(params.get("pattern") or "").strip().strip('"')
        if not pattern:
            return {"success": False, "error": "missing pattern"}
        search_path = str(params.get("path") or ".")
        include = str(params.get("include") or "")
        limit = int(params.get("limit", 50) or 50)
        return action_grep(agent, pattern, path=search_path, include=include, limit=limit)
