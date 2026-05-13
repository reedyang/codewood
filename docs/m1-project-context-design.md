# M1 Technical Design: Project Context Retrieval

This document defines the first milestone (M1) to improve large-repo code understanding and implementation quality in Smart Shell.

## Goals

- Provide a low-latency, deterministic way to locate candidate files/symbols before editing.
- Reduce blind global scanning and prompt noise in large projects.
- Keep implementation lightweight and compatible with current task loop.

## Non-goals (M1)

- No embedding/vector DB dependency.
- No semantic model serving.
- No automatic patch generation from the index.

## Module breakdown

- `src/tools/project_context_index.py`
  - `ProjectContextIndex`: workspace-bound index manager
  - Capabilities:
    - incremental refresh by `(mtime_ns, size)`
    - lightweight parsing (`symbols`, `imports`, `tokens`)
    - ranked retrieval for `query`
    - persistent storage to JSON

- `src/smart_shell_agent.py`
  - Runtime integration:
    - initialize index instance
    - bind workspace changes (`cd`)
    - expose tool action `project_context_search`
    - route tool call in `execute_tool_call`

- `src/tools.jsonc`
  - New tool schema: `project_context_search`

- `src/tools_prompt.md`
  - Guidance to prefer `project_context_search` first for large cross-module tasks

## Data structures

## File entry

```json
{
  "path": "src/foo/bar.py",
  "mtime_ns": 1723456789000000000,
  "size": 12345,
  "symbols": ["FooService", "handle_request"],
  "imports": ["from x import y", "import z"],
  "tokens": ["foo", "service", "request", "bar"]
}
```

## Index file

Stored at:

- `<ai_workspace>/knowledge_db/project_context_index.json`

Schema:

```json
{
  "version": 1,
  "workspace_root": "...",
  "last_index_at": 0.0,
  "files": {
    "relative/path.ext": { "...File entry..." }
  }
}
```

## Index update strategy

- Default path: incremental refresh
  - scan code files with known extensions
  - skip ignored dirs (`.git`, `node_modules`, `dist`, `build`, `.smartshell`, etc.)
  - unchanged files (same `mtime_ns` and `size`) are reused
  - changed/new files are re-parsed
  - removed files are dropped from index

- Rebuild path:
  - `force_rebuild=true` triggers full refresh

- Workspace switch:
  - on `cd` success, rebind index workspace root

## Retrieval strategy (M1 ranking)

Given query:

- tokenize normalized query words
- per file scoring:
  - query substring in path
  - token overlap in path
  - token overlap in extracted symbols/imports/tokens
- return top-k candidates with reasons and snippets:
  - `path`
  - `score`
  - `reasons`
  - top symbols/imports

This is intentionally transparent and debuggable for early-stage tuning.

## Integration points in `smart_shell_agent.py`

- `__init__`
  - create `ProjectContextIndex(work_directory, ai_workspace/knowledge_db)`

- `execute_tool_call`
  - new action branch: `project_context_search`
  - existing `cd` branch binds index workspace on success

- `action_project_context_search(params)`
  - params:
    - `query` (required)
    - `max_files` (optional)
    - `refresh` (optional)
    - `force_rebuild` (optional)
    - `status_only` (optional)

## API contract (tool result)

Success result:

```json
{
  "success": true,
  "query": "...",
  "query_tokens": ["..."],
  "total_matches": 37,
  "candidates": [
    {
      "path": "src/a/b.py",
      "score": 14.5,
      "reasons": ["path_contains_query", "token_hits=4"],
      "symbols": ["Foo", "bar"],
      "imports": ["from x import y"]
    }
  ],
  "index_status": {
    "files_total": 1023,
    "last_index_at": 1723.12
  }
}
```

## Operational notes

- M1 is optimized for determinism and low complexity, not semantic depth.
- M2 can add:
  - call graph edges
  - test-file affinity scoring
  - structured plan coupling (`Plan -> Evidence -> Edit`)

