# 终端 UI

[English](../en/tui.md) | **简体中文**

终端 UI 是 Code Wood 最早的界面。它与 GUI 打在同一份可执行文件里：不带参数运行进入 TUI，传入 `app` 则打开桌面 GUI（[桌面 GUI](gui.md)）。

```bash
python cli/main.py

# 以名称或路径指定工作区启动
python cli/main.py --workspace <工作区名称或路径>
python cli/main.py -w <工作区名称或路径>

# 执行一个任务后退出
python cli/main.py exec "你的任务描述"

# 指定工作区并执行一个任务后退出
python cli/main.py --workspace <工作区名称或路径> exec "你的任务描述"

# 启动时选择模型
python cli/main.py --model <模型名称>
python cli/main.py -m <模型名称>
```

## 输入规则

- `/command` —— 本地处理的内置命令，不消耗 AI 轮次
- `!command` —— 直接运行的原生 shell 命令或脚本
- 其他内容 —— 视为自然语言，交给 AI 处理

## 内置命令

| 命令 | 用途 |
|---|---|
| `/help`、`/exit`、`/quit` | 查看帮助、退出程序 |
| `/clear screen`、`/clear context`、`/clear input history` | 清屏、清空对话上下文、清空输入历史 |
| `/compact` | 摘要压缩对话以释放上下文 |
| `/workspace` | 列出、创建、切换、重命名、更新、删除工作区 |
| `/chat` | 新建、列出、切换、重命名、fork、编辑、重载、删除对话 |
| `/model` | 选择 `provider/model`；模型声明了 `reasoning_effort` 时可用 `/model reasoning <level>` |
| `/reasoning` | 设置当前对话的推理强度 |
| `/plan`、`/plan off`、`/plan status` | 进入或退出 Plan 模式（只做方案设计不执行）并查看状态 |
| `/agent` | 切回 Agent 模式 |
| `/execution-policy` | 查看或设置 `unlimited`、`moderate`、`confirmation` |
| `/always_confirm-reset` | 重置 always-confirm 白名单 |
| `/mcp` | 列出工具/资源/prompt、查看状态、重连、重载配置、启用/禁用工具 |
| `/memory` | 启用/禁用记忆、列出、检索、记录、删除条目、查看统计 |
| `/language` | 切换界面语言 |

输入 `/` 后按 Tab 可查看补全菜单；带参数的命令会提示参数形态（例如 `/chat switch <index|id|name>`）。

## 斜杠命令与 GUI 的关系

上述每个内置命令在 GUI 中都有对应操作，两端保持同步。执行策略与确认白名单的细节见 [AI 特性](ai-features.md)。
