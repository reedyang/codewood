# Project Context Retrieval: Current Design

This document describes the current lightweight project-context retrieval implementation in Smart Shell. It supersedes the original M1 draft language in this file.

## Goals

- Provide a deterministic, low-latency way to find candidate files and symbols before broader shell exploration.
- Reduce blind global scanning in large repositories.
- Keep indexing local, transparent, and cheap enough to run opportunistically in the background.
- Inject first-turn evidence only when the workspace already has a usable index.

## Non-goals

- No embedding/vector database for project-context search.
- No semantic model serving.
- No call graph construction.
- No automatic patch generation from the index.
- No use in the Default workspace.

## Module Map

- `src/tools/project_context_index.py`
  - Defines `ProjectContextIndex`.
  - Owns persistent JSON loading/saving, workspace binding, incremental refresh, status, and ranked search.

- `src/actions/command_actions.py`
  - Implements `action_project_context_search(agent, params)`.
  - Enforces workspace gating, parameter normalization, refresh policy, and search invocation.

- `src/agent.py`
  - Provides feature/tool gating helpers.
  - Binds the project index to the active work directory and workspace storage.
  - Schedules background refresh threads.
  - Renders first-turn evidence blocks from search results.

- `src/runtime/bootstrap.py`
  - Creates the index during runtime service setup.
  - Schedules a startup background refresh.

- `src/runtime/runtime_loop.py`
  - Attempts first-turn project-context evidence injection before sending the user task to the model.

- `src/tooling/handlers/file_shell_handlers.py`
  - Routes `project_context_search` through the `ToolDispatcher` handler path.

- `src/tooling/execution_engine.py`
  - Keeps the legacy execution branch for `project_context_search` and prints a short console summary.

- `src/runtime/prompt_composer.py`
  - Loads tool schemas from `src/tools/tools.jsonc`.
  - Hides `project_context_search` from the injected tool catalog when the current workspace is the Default workspace.

- `src/tools/tools.jsonc`
  - Declares the public tool schema for `project_context_search`.

- `src/prompts/tools_prompt.md`
  - Tells the model to prefer `project_context_search` for large cross-module code tasks when the tool is available.

## Workspace Policy

Project-context retrieval is disabled in the Default workspace.

Current checks:

- `_is_default_workspace()` returns true when `workspace_id` is `default` or `workspace_kind` is `default`.
- `_project_context_feature_enabled()` returns false in the Default workspace.
- `_project_context_tool_allowed()` returns false in the Default workspace.
- `build_tools_prompt_append()` omits `project_context_search` from the visible tool list when the tool is not allowed.
- `action_project_context_search()` fails fast in the Default workspace with an explanatory error.

For non-Default workspaces, the feature is enabled by default and can be controlled by:

```json
{
  "project_context_first_round_evidence": true
}
```

## Index Storage

The index is stored under the active AI workspace directory:

- `<ai_workspace>/indexes/project_context_index.json`

`ProjectContextIndex` is initialized with:

- `workspace_root = agent.work_directory`
- `storage_dir = agent.workspace_config_dir / "indexes"`

When the active workspace/runtime is refreshed, Smart Shell schedules a background refresh using the current `work_directory` and the current workspace `indexes` directory.

## Data Model

The persisted JSON shape is:

```json
{
  "version": 1,
  "workspace_root": "...",
  "last_index_at": 0.0,
  "files": {
    "relative/path.ext": {
      "path": "relative/path.ext",
      "mtime_ns": 1723456789000000000,
      "size": 12345,
      "symbols": ["FooService", "handle_request"],
      "imports": ["from x import y", "import z"],
      "tokens": ["foo", "service", "request"]
    }
  }
}
```

Each file entry is represented internally by `_FileEntry`.

## Indexed Files

The index currently scans code files with these extensions:

- `.py`, `.js`, `.jsx`, `.ts`, `.tsx`
- `.java`, `.go`, `.rs`
- `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hpp`
- `.cs`, `.swift`, `.kt`, `.kts`
- `.rb`, `.php`, `.m`, `.mm`

These directories are pruned during traversal:

- `.git`, `.hg`, `.svn`
- `node_modules`, `dist`, `build`, `out`
- `.idea`, `.vscode`, `.smartshell`
- `__pycache__`, `.pytest_cache`

The traversal uses `os.walk(..., followlinks=False)`.

## Refresh Behavior

`refresh_index(force=False, timeout_ms=None)` performs an incremental refresh:

- Files are identified by relative path.
- Unchanged files are reused when both `mtime_ns` and `size` match.
- Changed or new files are reparsed.
- Removed files are dropped only when the refresh completes without timing out.
- If a timeout occurs, the existing index remains usable and the result is marked stale.

The refresh result includes:

```json
{
  "success": true,
  "force": false,
  "workspace_root": "...",
  "files_total": 1023,
  "scanned": 1023,
  "processed": 1023,
  "added": 1,
  "updated": 2,
  "unchanged": 1020,
  "deleted": 0,
  "timed_out": false,
  "stale": false,
  "elapsed_ms": 42,
  "index_path": "..."
}
```

Background refreshes are serialized by `_project_context_refresh_gate` and `_project_context_refresh_inflight`.

Current background refresh scheduling:

- Startup: `reason="startup"`.
- Workspace runtime refresh: `reason="workspace-refresh"`.
- First-turn evidence not ready: `reason="first-round-evidence-not-ready"`.
- Tool call with `refresh_async=true`: `reason="project-context-search"`.

Non-force background refreshes use a 2000 ms timeout. Forced rebuilds do not apply that timeout.

## Parsing Behavior

Parsing is intentionally lightweight and regex-based.

The index extracts:

- Symbols from Python classes/functions, JavaScript/TypeScript functions and variables, and C-like method declarations.
- Imports/includes/usings/requires from common language patterns.
- Tokens from the relative file path, extracted symbols, and extracted imports.

Limits:

- Symbols are de-duplicated and capped at 120 per file.
- Imports are de-duplicated and capped at 120 per file.
- Tokens are sorted and capped at 300 per file.

Files are read as UTF-8 with replacement for invalid characters.

## Search Behavior

`search(query, max_files=12, auto_refresh=True, refresh_timeout_ms=None)`:

- Rejects an empty query.
- Optionally runs an incremental refresh before scoring.
- Tokenizes the query with the same normalized word splitter used for file tokens.
- Scores each indexed file using path, token, symbol, and import matches.

Current scoring:

- Full query substring in path: `+8.0`, reason `path_contains_query`.
- Query token appears in path: `+3.0`.
- Query token appears in file tokens: `+2.0`.
- Query token appears in one of the first 80 symbols: `+4.0`.
- Query token appears in one of the first 80 imports: `+1.5`.
- Any token hit adds a reason like `token_hits=4`.

Results are sorted by score descending and capped by `max_files`.

## Tool Contract

Tool name:

- `project_context_search`

Declared parameters in `src/tools/tools.jsonc`:

- `query`: string, required by schema.
- `max_files`: integer, default `12`, runtime maximum `50`.
- `refresh`: boolean, default behavior is true when omitted.
- `refresh_async`: boolean, default false.
- `force_rebuild`: boolean, default false.
- `status_only`: boolean, default false.

Runtime details:

- `max_files <= 0` falls back to `12`.
- `max_files > 50` is clamped to `50`.
- `force_rebuild=true` performs a synchronous full rebuild before searching.
- `refresh_async=true` schedules a background refresh and searches the current index without a synchronous refresh.
- `refresh_timeout_ms` is fixed at `2000` for synchronous incremental refreshes initiated by the action.
- `status_only=true` returns `ProjectContextIndex.status()` after binding the index workspace.

Typical success result:

```json
{
  "success": true,
  "query": "settings timer crash",
  "query_tokens": ["settings", "timer", "crash"],
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
    "success": true,
    "workspace_root": "...",
    "index_path": "...",
    "files_total": 1023,
    "last_index_at": 1723.12
  },
  "stale": false,
  "index_refresh": {
    "success": true,
    "timed_out": false
  }
}
```

When `refresh_async=true`, the result also includes:

```json
{
  "refresh_scheduled": true
}
```

## First-Turn Evidence Injection

Before the model receives a user task, the runtime may auto-inject a compact evidence block.

This happens only when:

- The feature is enabled.
- The current workspace is not Default.
- No project-context refresh is currently in flight.
- The in-memory index already has at least one file.

When ready, the runtime calls:

```json
{
  "query": "<original user task>",
  "max_files": 8,
  "refresh": false,
  "refresh_async": true
}
```

The rendered block starts with:

```text
[First-turn Evidence Block (auto-injected)]
```

It lists up to 8 candidate files with score, reasons, and up to 4 symbols per candidate.

When the index is empty or refresh is in flight, Smart Shell skips evidence injection and schedules a background refresh instead.

## Operational Notes

- The feature is designed as a cheap orientation layer, not a complete code intelligence system.
- A stale or partial index is acceptable because the model is still expected to verify candidates with shell reads/searches before editing.
- Timeout handling favors preserving the previous index over applying a partially discovered deletion set.
- The current implementation is local and deterministic; future work can add richer language parsers, call graph edges, test affinity, or semantic ranking without changing the public tool shape.

