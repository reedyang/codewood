---
name: codex-usage
description: |
  Use this skill to inspect OpenAI Codex usage. The supported arguments match the current `ccusage` version. It supports date-range queries, JSON output, timezone/locale settings, offline mode, compact output, color output, and the `daily`, `monthly`, and `session` subcommands for different reporting granularities.
compatibility: []
---

# Codex Usage Skill

Use this skill to inspect OpenAI Codex usage. The supported arguments match `@ccusage/codex@19.0.0`. If `npx ccusage` is unavailable, install it with `npm install @ccusage/codex@19.0.0`.

## Supported Invocation Patterns

- **Query by date range**
  ```bash
  npx ccusage codex --since <YYYY-MM-DD> --until <YYYY-MM-DD>

> **Note**: If the user asks for Codex usage for a specific date or a date range, you must provide date arguments explicitly, such as `--since` and `--until`.
  ```

- **Specify timezone**
  ```bash
  npx ccusage codex -z <TIMEZONE>
  ```

- **Specify locale**
  ```bash
  npx ccusage codex -l <LOCALE>
  ```

- **Offline mode**
  ```bash
  npx ccusage codex -O   # Enable offline mode
  npx ccusage codex --no-offline   # Disable offline mode
  ```

- **Compact output**
  ```bash
  npx ccusage codex --compact
  ```

- **Color output**
  ```bash
  npx ccusage codex --color
  npx ccusage codex --no-color
  ```

- **Help**
  ```bash
  npx ccusage codex -h
  ```

- **Version**
  ```bash
  npx ccusage codex -v
  ```

- **Subcommands**
  - `daily`   View usage statistics for recent days
  - `monthly` View usage statistics for the current month
  - `session` View usage statistics for the current session

> All flags can be combined. For example:
> `npx ccusage codex --since 2023-07-01 --until 2023-07-31 -j --compact`。
