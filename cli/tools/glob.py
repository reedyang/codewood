"""Tool: glob.

Find files matching a glob pattern within the workspace. Uses ripgrep (rg)
under the hood. Returns relative file paths, one per line.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseTool
from .shell import _workspace_rg_executable_path


def _find_rg(agent: Any) -> Optional[Path]:
    return _workspace_rg_executable_path(agent)


def action_glob(
    agent: Any,
    pattern: str,
    path: str = ".",
    limit: int = 100,
) -> Dict[str, Any]:
    try:
        _rg = _find_rg(agent)
        if _rg is None:
            return {"success": False, "error": "ripgrep (rg) is not available. It may still be downloading."}

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

        cmd = [
            str(_rg),
            "--files",
            "--color", "never",
            "--no-messages",
        ]

        if pattern and pattern != "*":
            cmd.extend(["--glob", pattern])

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
            return {"success": False, "error": f"glob timed out searching '{path}'"}

        stdout = (result.stdout or "").strip()
        exit_code = result.returncode

        if exit_code != 0 and not stdout:
            return {"success": False, "error": f"rg exited with code {exit_code}: {result.stderr}"}

        if not stdout:
            return {"success": True, "content": "No files found", "matches": 0}

        lines = stdout.splitlines()

        if limit and limit > 0 and len(lines) > limit:
            lines = lines[:limit]

        match_count = len(lines)

        # Convert absolute paths to relative
        output_parts: List[str] = []
        for file_path in lines:
            try:
                rel_path = str(Path(file_path).relative_to(search_dir))
            except ValueError:
                rel_path = file_path
            output_parts.append(rel_path)

        return {
            "success": True,
            "content": "\n".join(output_parts),
            "matches": match_count,
            "pattern": pattern,
        }

    except Exception as e:
        return {"success": False, "error": f"glob failed: {e}"}


class GlobTool(BaseTool):
    name = "glob"
    description = (
        "Find files matching a glob pattern within the workspace. "
        "Returns relative file paths, one per line."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files (e.g. \"**/*.py\" or \"src/**/*.ts\")",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative path from workspace root). Defaults to the workspace root.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default: 100)",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            return {"success": False, "error": "missing pattern"}
        search_path = str(params.get("path") or ".")
        limit = int(params.get("limit", 100) or 100)
        return action_glob(agent, pattern, path=search_path, limit=limit)
