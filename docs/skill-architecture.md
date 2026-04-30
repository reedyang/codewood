# Agent Skills architecture principles

This document describes how **Smart Shell** loads and uses **Agent Skills**, written so the same ideas can be reused in **other AI coding assistants, agents, or IDEs** (Cursor, Claude Code, Copilot-style workflows, custom MCP hosts, etc.). It is **not** tied to a single vendor UI.

Upstream reference format: [Anthropic Agent Skills (`anthropics/skills`)](https://github.com/anthropics/skills/blob/main/README.md).

---

## 1. Goals

- **Portable skill packs**: A skill is a **folder** you can copy, version, and share; behavior is documented in `SKILL.md` and optional sidecars.
- **Host-agnostic contracts**: Skills must not encode **product-specific** environment variable prefixes (e.g. a single IDE name). Hosts implement **generic** discovery and bridging.
- **No host hardcoding of individual skills**: The runtime must not special-case skill **names** or script filenames (e.g. `if skill == "baidu"`). Matching is by **bundle path**, **declared metadata**, or **user intent**—not string literals for one skill in host code.
- **Separation of concerns**: Skills describe *what* to do; the host provides tools (`shell`, file I/O, MCP, etc.) and injects context.
- **Multi-skill orchestration**（多 skill 编排）: Prefer **stdin** and **shell pipes** to pass payloads between steps; avoid **avoidable intermediate files** in the workspace when the same data can flow on the command line. Document the preferred pattern in `SKILL.md` so contributors can self-check when adding or reviewing skills.

---

## 2. Skill bundle layout

Each skill is a directory (the **skill id** equals the **folder name**):

```text
<skills_root>/<skill_id>/
  SKILL.md                 # required: YAML frontmatter + Markdown body (optional keys: see §5)
  scripts/                 # optional: bundled executables (e.g. *.py)
  ...                      # other assets as needed
```

---

## 3. Load order and overrides (merged catalog)

When multiple roots exist, the host merges skills by **`skill_id`** (folder name). Typical priority **low → high**:

| Layer        | Typical path                          | Role                          |
|-------------|----------------------------------------|-------------------------------|
| Workspace   | `<workspace>/skills/`                  | Project-local skills          |
| Builtin     | `<app>/skills/`                        | Shipped with the application |
| User/config | `<config_dir>/skills/` (e.g. next to `config.json`) | Per-user overrides   |

**Same `skill_id`**: higher-priority layer **replaces** the lower one. This keeps forks and user patches predictable.

Hosts that only have a single `skills/` directory can still follow the same **folder = skill id** rule.

---

## 4. `SKILL.md` contract

- **YAML frontmatter** (between `---` lines) with at least:
  - **`name`**: Human-readable name (fallback: folder name).
  - **`description`**: Short routing blurb for the model (when to use this skill).
- **Body**: Full instructions, CLI, orchestration, limitations—whatever the model must follow.

Invalid frontmatter may cause the host to **skip** the skill; keep YAML valid.

Optional fields are **not** required to contain host-specific keys. Prefer neutral, portable wording (e.g. “the host’s shell or subprocess runner” instead of a single product name).

### Host–skill boundary: what `SKILL.md` must not prescribe

Individual skills **cannot see** the host’s full tool surface or other skill bundles. A `SKILL.md` must describe **only** what happens **inside this bundle** (scripts, flags, expected sections in output, retries, safety). The following belong to **host** prompts and runtime—not to any single skill file:

| Stay in the host (system / tool prompts, agent code) | Stay inside the skill (`SKILL.md` + bundle assets) |
|------------------------------------------------------|---------------------------------------------------|
| Task lifecycle: e.g. when to emit **done**, **ask_more_info**, **task_changed** | When **this** script’s run is complete for the current query (e.g. required markers in stdout), and “do not re-run the same command for the same query” |
| Naming or ordering **other skills**, MCP tools, or “load skill” injection | Neutral wording: e.g. “further steps the host may schedule are out of scope here” |
| Multi-skill pipelines, stdin pipes **between** bundles, cross-skill ids | CLI for **this** bundle only; how the host merges `model_context_file_env` into the subprocess result |

**Rules of thumb**

- Do **not** mention host control tools by name (`done`, `ask_more_info`, etc.).
- Do **not** reference other **`skill_id`** values or tell the model to call another skill next.
- Do **not** reference **MCP** or other plugin namespaces as part of this skill’s contract unless the repo defines a **neutral, portable** pattern that applies to all skills equally.
- Prefer **subprocess result** / **merged `output`** over **tool `output`** when you mean shell stdout or merged file content, so “tool” is not confused with the host’s JSON tool API.

Multi-skill orchestration and “finish the whole user goal” policies live in **host documentation** (e.g. Smart Shell’s `src/system_prompt.md`, `src/tools_prompt.md`), not in per-skill `SKILL.md` files.

---

## 5. Optional frontmatter: `model_context_file_env` (extended tool output)

Some scripts want to pass **large text** to the model **without** printing it all to the user terminal. Declare this in **`SKILL.md` YAML frontmatter** (same file as the rest of the skill—no separate sidecar):

```yaml
---
name: my-skill
description: "..."
model_context_file_env: MY_SKILL_MERGE_OUTPUT
---
```

- **`model_context_file_env`** (or **`modelContextFileEnv`**): Must be a valid environment variable name (`[A-Za-z_][A-Za-z0-9_]*`).
- **Semantics**: A conforming host **may** create a temporary UTF-8 file, set that env var to its **absolute path** for the child process, and after exit code **0** append the file contents to the tool result shown to the model (exact merge format is host-defined).
- **Naming**: Choose a **skill-specific** name (e.g. `BAIDU_SKILL_MERGE_OUTPUT`), **not** a host product prefix.

Hosts should:

1. Resolve which skill bundle contains the **invoked script path** (longest matching `bundle_root` wins if multiple match).
2. Read **`model_context_file_env`** from the parsed frontmatter of that bundle’s `SKILL.md`.
3. Avoid creating temp files when the field is absent or invalid.

---

## 6. Environment variables inside skills

- Scripts should use **neutral, skill-scoped** names, e.g. `BAIDU_SKILL_VERBOSE`, `DEEPCRAWL_SKILL_INSECURE_SSL`.
- Avoid embedding **host product** names in env vars inside skill code (portability and clarity).

---

## 7. Invoking bundled scripts

- The host usually runs commands in the **user workspace cwd**; it does **not** auto-`cd` into the skill folder.
- Tools and prompts should tell the model to call scripts with **absolute paths**: `<bundle_root>/scripts/...`.
- Listing detected `scripts/*.py` paths in the system prompt improves copy-paste reliability across tools.

---

## 8. What hosts should **not** do

- Do not branch on **specific `skill_id`** or script filenames for generic behavior (merge output, SSL, etc.).
- Do not require skills to use **host-private** YAML keys that only one product understands; optional **`model_context_file_env`** in frontmatter is a **documented, portable** field (see §5), not a product-specific secret.
- Do not strip skill-authored **timeliness / safety** rules from `SKILL.md`; those belong in the skill, not scattered as one-off checks in the host.

---

## 9. Compatibility notes for other AI programming tools

| Concern | Portable practice |
|--------|-------------------|
| **System prompt** | Inject skill index + full `SKILL.md` bodies (or on-demand via a “load skill” tool). |
| **Tool naming** | Map your tool names (`run_terminal_cmd`, `execute_shell`, etc.) to the same *intent* as `shell`; skills stay agnostic. |
| **Paths** | Use OS-native absolute paths in examples; avoid assumptions about WSL vs Windows beyond normal path rules. |
| **MCP / plugins** | Skills remain file-based; an MCP server can mirror the same folder layout. |

---

## 10. Smart Shell mapping (reference implementation)

In this repository:

- Loader: `src/skills_loader.py`
- Merge / `model_context_file_env` handling for subprocess `shell`: `src/smart_shell_agent.py` (resolves env name from the matched skill’s `SKILL.md` frontmatter via `src/skills_loader.py`)
- Tool-facing description: `src/tools_prompt.md`

Other products can implement the same **principles** without copying implementation details.

---

## Document history

- Introduced to capture host–skill boundaries and portability expectations for Agent Skills in Smart Shell and compatible agents.
- Added multi-skill orchestration principle: prefer stdin/pipes, avoid avoidable intermediate files.
- Documented **host–skill boundary** for `SKILL.md`: no `done`/other-skill/MCP orchestration inside individual skills; those rules belong in host prompts.
