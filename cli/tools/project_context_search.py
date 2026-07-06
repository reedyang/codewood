"""Tool: project_context_search."""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool


class ProjectContextSearchTool(BaseTool):
    name = "project_context_search"
    description = "Fast code-context retrieval using BM25 + embeddings. Supports text search and call-graph queries (callers/callees)."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language retrieval intent. Describe what you're looking for as if asking a teammate, e.g. 'where is auth token validated', 'find the RoomSettingsPanel timer code', 'database connection pool implementation'. Required unless 'call_graph' is provided.",
            },
            "call_graph": {
                "type": "string",
                "description": "Symbol (function/method name) to run a call-graph query on. When set, returns callers and/or callees of this symbol instead of a text search.",
            },
            "call_graph_direction": {
                "type": "string",
                "description": "Call-graph direction when 'call_graph' is set: 'callees' (what the symbol calls), 'callers' (what calls the symbol), or 'both' (default).",
            },
            "max_files": {
                "type": "integer",
                "description": "Maximum number of candidate files (or call-graph edges per direction) to return (default 12, max 50).",
            },
            "refresh": {
                "type": "boolean",
                "description": "Whether to run an incremental refresh before retrieval (default true).",
            },
            "refresh_async": {
                "type": "boolean",
                "description": "Whether to trigger incremental refresh asynchronously (default false). If enabled, this call returns candidates from the current index first; refresh results affect later turns.",
            },
            "force_rebuild": {
                "type": "boolean",
                "description": "Whether to force a full index rebuild before retrieval (slow).",
            },
            "status_only": {
                "type": "boolean",
                "description": "Return only index status; do not run retrieval.",
            },
        },
        "required": [],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return agent.action_project_context_search(params if isinstance(params, dict) else {})
