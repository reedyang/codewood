# Sub-agents

**English** | [简体中文](../zh-CN/subagents.md)

Sub-agents are user-configured helpers that run an isolated nested agentic loop with their own model, system instructions, and tool allowlist. The main model invokes a sub-agent automatically through a single `run_subagent` tool, based on each sub-agent's `description`, and the sub-agent's final text result is injected back into the main loop.

Define sub-agents as Markdown files with YAML frontmatter, one per file:

- User-level sub-agents live under `~/.codewood/subagents/<name>.md`
- Workspace sub-agents live under `<workspace>/.codewood/subagents/<name>.md` (workspace overrides user-level by `name`)

Frontmatter keys (the Markdown body is the sub-agent's independent system instructions):

- `name` (required) — sub-agent id
- `description` (required) — when-to-use text that drives the main model's auto-selection
- `model` (optional) — a `provider/model` selector referencing `model_providers`; defaults to the main model
- `tools` (optional) — allowlist of tool names; defaults to a core coding set (`shell`, `apply_patch`, `read`, `project_context_search`, `update_plan`, `request_skill_prompt`). An explicit empty list (`tools: []`) grants no tools. `run_subagent` is always excluded, so sub-agents cannot nest.
- `max_rounds` (optional) — maximum tool-use rounds before the sub-agent must return (default 20)

The `run_subagent` tool also accepts an optional `image` argument (a file path). The image is attached to and analyzed by the **sub-agent's own model**, not the main model — so a non-multimodal main model can delegate image understanding to a multimodal sub-agent. See `additional-subagents/image-analyzer.md` for a ready-made example that turns a UI mockup, screenshot, diagram, or chart into a structured description a coding model can act on.

In the GUI, open **Settings → Sub-agents** to view and manage the configured sub-agents.

## Example Sub-agents

`additional-subagents/` ships ready-made examples (for example `image-analyzer.md`). Copy one into `.codewood/subagents/` to activate it.
