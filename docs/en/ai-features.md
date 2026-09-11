# AI Features

**English** | [简体中文](../zh-CN/ai-features.md)

## Execution Policy

Use `execution_policy` in `.codewood/config.jsonc` to control how potentially risky actions are handled:

- `confirmation`: ask for y/n confirmation for every action that needs approval
- `moderate`: automatically execute safe actions after evaluating risk
- `unlimited`: skip safety checks and execute directly

Switch policies with:

```bash
/execution-policy <unlimited|moderate|confirmation>
```

Temporary scripts created through the built-in `script` command are considered session-local work items. If you later run that script through `shell` and it exits with code `0`, Code Wood will attempt to delete it automatically so temporary files do not linger. If you want to keep a script permanently, use `text_file` to create it in the current working directory instead.

## Always-Confirm and `confirm_allowlist.json`

When free mode is disabled and interactive confirmation is still required, the prompt offers `a` or `always` only for `shell` commands. That means only the current command is added to the allowlist (`shell_script_paths` / `shell_exe_tokens`), not every command globally.

- `script` output files and `text_file` writes remain y/n only
- `shell` execution of a script created in the same session also remains y/n only
- The allowlist file lives next to `config.jsonc` as `confirm_allowlist.json`
- Legacy `shell_commands` entries are converted into the newer v2 structure automatically at startup
- `/always_confirm reset` deletes the file and restores the default prompt behavior

## Built-In Commands vs Native Shell Commands

- Built-in commands that do not go through AI must start with `/`, for example `/exit`, `/help`, `/clear screen`, `/clear context`, and `/free`
- Native shell commands or scripts that should run directly must start with `!`, for example `!dir` or `!git status`
- Any input that does not start with `/` is treated as natural language and sent to the AI

## Related Documentation

- [Agent Skills](skills.md)
- [Sub-agents](subagents.md)
- [Shell sandbox](sandbox.md)
