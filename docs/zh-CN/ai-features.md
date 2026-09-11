# AI 特性

[English](../en/ai-features.md) | **简体中文**

## 执行策略

在 `.codewood/config.jsonc` 中用 `execution_policy` 控制潜在风险操作的处理方式：

- `confirmation`：每个需要审批的操作都询问 y/n 确认
- `moderate`：评估风险后自动执行安全操作
- `unlimited`：跳过安全检查，直接执行

切换策略：

```bash
/execution-policy <unlimited|moderate|confirmation>
```

通过内置 `script` 命令创建的临时脚本被视为会话内的工作产物。若之后通过 `shell` 运行该脚本且退出码为 `0`，Code Wood 会尝试自动删除它，避免临时文件残留。若想长期保留脚本，请改用 `text_file` 在当前工作目录中创建。

## Always-Confirm 与 `confirm_allowlist.json`

当自由模式被关闭、仍需交互确认时，只有 `shell` 命令的提示会提供 `a` 或 `always` 选项。也就是说只有当前这条命令会被加入白名单（`shell_script_paths` / `shell_exe_tokens`），而不是全局放开所有命令。

- `script` 产物文件与 `text_file` 写入仍然只有 y/n
- 对同一次会话内创建的脚本执行 `shell` 仍然只有 y/n
- 白名单文件与 `config.jsonc` 同目录，名为 `confirm_allowlist.json`
- 旧版 `shell_commands` 条目会在启动时自动转换为新的 v2 结构
- `/always_confirm reset` 会删除该文件并恢复默认确认行为

## 内置命令与原生 shell 命令

- 不经过 AI 的内置命令必须以 `/` 开头，例如 `/exit`、`/help`、`/clear screen`、`/clear context`、`/free`
- 需要直接运行的原生 shell 命令或脚本必须以 `!` 开头，例如 `!dir`、`!git status`
- 任何不以 `/` 开头的输入都被视为自然语言，交给 AI 处理

## 相关文档

- [Agent Skills](skills.md)
- [子代理](subagents.md)
- [Shell 沙箱](sandbox.md)
