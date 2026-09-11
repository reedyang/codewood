# AGENTS.md

**English** | [简体中文](../zh-CN/agents-md.md)

Code Wood reads `AGENTS.md` files and injects them into the system prompt as
user-defined instructions, so a repository can carry its own coding conventions,
tips, and constraints without any per-project configuration.

## Where files are loaded from

Two kinds of locations are scanned:

1. **Global** — the Code Wood config directory (the one holding `config.jsonc`,
   `~/.config/codewood/` by default, or `$CODEWOOD_HOME`).
2. **Project** — the current workspace, plus every directory on the path from the
   repository root down to the workspace:
   - If the workspace is inside a Git repository, the chain starts at the repo
     root (the nearest ancestor containing `.git`) and walks down one directory
     at a time to the workspace.
   - If it is not inside a repository, only the workspace directory itself is
     read (parent directories are not searched).

This makes monorepos work naturally: a root `AGENTS.md` applies to everything,
while a nested `src/api/AGENTS.md` adds or overrides rules for that subtree.

Directories are de-duplicated, and a file is only injected once even if several
paths resolve to it.

## `AGENTS.md` vs `AGENTS.override.md`

Each directory may contain both files:

| File | Role |
|---|---|
| `AGENTS.md` | The normal, usually committed instructions for that directory |
| `AGENTS.override.md` | Takes precedence over `AGENTS.md` in the same directory |

Because only one file per directory is used, `AGENTS.override.md` is the way to
keep local, machine-specific or experimental instructions out of a committed
`AGENTS.md`.

## How the content is injected

- Loaded files are appended to the system prompt under a
  `## User Custom Prompts (AGENTS.md)` section, each labelled with its scope and
  source path (`### global · AGENTS.md`, `### project · src/api · AGENTS.md`).
- The combined section is capped at **32 KiB**; content past the limit is dropped.
- Files are re-read when they change (existence, size, or modification time), so
  edits apply to the next request without restarting Code Wood.
- Relative paths and other instructions inside the file are interpreted by the
  model like any other prompt text.

## Precedence

When instructions conflict, the more specific intent wins:

1. A skill explicitly selected for the current request (`/skills/<skill-name>` or
   a triggered `request_skill_prompt`) takes precedence over `AGENTS.md`.
2. An explicitly named MCP server/tool target takes precedence over `AGENTS.md`
   and general rules, except for hard safety and privilege constraints.
3. Otherwise, `AGENTS.md` content applies alongside the base system prompt.

## Related Documentation

- [Agent Skills](skills.md)
- [Configuration](configuration.md)
- [AI Features](ai-features.md)
