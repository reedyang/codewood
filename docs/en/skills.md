# Agent Skills

**English** | [简体中文](../zh-CN/skills.md)

Code Wood follows the same layout as [Anthropic Agent Skills](https://github.com/anthropics/skills/blob/main/README.md). Create a `skills/` directory and place one skill per folder containing a `SKILL.md` file with YAML frontmatter and Markdown body.

For the underlying design principles (portable skill packs, host-agnostic contracts, multi-skill orchestration), see [Agent Skills architecture principles](skill-architecture.md).

## Load Paths & Priority

Skills are loaded from five sources, merged by folder name (`skill_id`) from **lowest to highest** priority (higher-priority sources override lower-priority ones with the same name):

| Priority | Source | Path | Description |
|---|---|---|---|
| 1 (lowest) | Built-in | `<project_root>/skills/` | Shipped with the application |
| 2 | Agents external | `~/.agents/skills/` | System-wide agent skills (e.g. from Cursor/Codex) |
| 3 | Global external | `~/.codewood/skills/` | User-level configuration skills |
| 4 | Workspace agents | `<workspace>/.agents/skills/` | Workspace-local agent skills |
| 5 (highest) | Workspace external | `<workspace>/.codewood/skills/` | Workspace-local project skills |

> **Note:** In a standard setup, `<workspace>/` refers to the workspace root directory. The global config directory (`~/.codewood/`) is at `~/.config/codewood/` by default, or overridden via the `CODEWOOD_HOME` environment variable.

Example: if both `~/.agents/skills/my-skill/SKILL.md` and `~/.codewood/skills/my-skill/SKILL.md` exist, the latter takes precedence.

## Enabling / Disabling Skills

In the GUI, open **Settings → Skills** to see all non-workspace skills grouped by source. Each skill can be toggled on or off independently. Disabled skills are persisted in `skills.jsonc`:

- Built-in, agents, and global skills → `~/.config/codewood/skills.jsonc`
- Workspace skills → `<workspace>/.codewood/skills.jsonc`

```jsonc
{
  "disabledSkills": ["my-skill", "another-skill"]
}
```

## Skill Folder Contents

- Skill folders can include helper files such as `scripts/*.py`
- Relative paths in a skill body are resolved from that skill folder
- The runtime injects the absolute skill bundle root and detected scripts into the system prompt
- During startup, Code Wood scans and parses all available skills and uses them when a task matches a skill description

## Bundled Scripts

Skills may ship executable scripts under a `scripts/` subdirectory (e.g. `scripts/*.py`). When the model is given the skill prompt, Code Wood detects these files and lists their absolute paths so the model can invoke them directly via `shell` without guessing the OS-specific location.

## Bundled and Additional Skills

`skills/` holds the built-in skills shipped with the application, while `additional-skills/` contains optional extras (`docx`, `pdf`, `pptx`, `xlsx`, and more). Copy an additional skill into `.codewood/skills/` to activate it.
