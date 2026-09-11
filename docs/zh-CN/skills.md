# Agent Skills

[English](../en/skills.md) | **简体中文**

Code Wood 采用与 [Anthropic Agent Skills](https://github.com/anthropics/skills/blob/main/README.md) 相同的布局。创建 `skills/` 目录，每个技能一个文件夹，内含带 YAML frontmatter 与 Markdown 正文的 `SKILL.md`。

关于背后的设计原则（可移植的技能包、与宿主无关的约定、多技能编排），见 [Agent Skills 架构](skill-architecture.md)。

## 加载路径与优先级

技能来自五个来源，按文件夹名（`skill_id`）从 **低到高** 优先级合并（同名时高优先级覆盖低优先级）：

| 优先级 | 来源 | 路径 | 说明 |
|---|---|---|---|
| 1（最低） | 内置 | `<project_root>/skills/` | 随应用发布 |
| 2 | Agents 外部 | `~/.agents/skills/` | 系统级 agent 技能（例如来自 Cursor/Codex） |
| 3 | 全局外部 | `~/.codewood/skills/` | 用户级配置技能 |
| 4 | 工作区 agents | `<workspace>/.agents/skills/` | 工作区本地 agent 技能 |
| 5（最高） | 工作区外部 | `<workspace>/.codewood/skills/` | 工作区本地项目技能 |

> **注意：** 标准情况下 `<workspace>/` 指工作区根目录。全局配置目录（`~/.codewood/`）默认位于 `~/.config/codewood/`，可通过环境变量 `CODEWOOD_HOME` 覆盖。

例如 `~/.agents/skills/my-skill/SKILL.md` 与 `~/.codewood/skills/my-skill/SKILL.md` 同时存在时，后者生效。

## 启用 / 禁用技能

在 GUI 中打开 **设置 → Skills**，可按来源查看所有非工作区技能。每个技能都可以单独开关。禁用的技能记录在 `skills.jsonc` 中：

- 内置、agents 与全局技能 → `~/.config/codewood/skills.jsonc`
- 工作区技能 → `<workspace>/.codewood/skills.jsonc`

```jsonc
{
  "disabledSkills": ["my-skill", "another-skill"]
}
```

## 技能文件夹内容

- 技能文件夹可以包含辅助文件，例如 `scripts/*.py`
- 技能正文中的相对路径以该技能文件夹为基准解析
- 运行时会把技能包的绝对根路径与探测到的脚本注入系统提示词
- 启动时 Code Wood 会扫描并解析所有可用技能，并在任务匹配技能描述时使用它们

## 内置脚本

技能可以在 `scripts/` 子目录下提供可执行脚本（例如 `scripts/*.py`）。当模型拿到技能提示词时，Code Wood 会探测这些文件并列出其绝对路径，使模型能直接通过 `shell` 调用，而无需猜测各操作系统的具体位置。

## 内置与附加技能

`skills/` 存放随应用发布的内置技能，`additional-skills/` 则包含可选扩展（`docx`、`pdf`、`pptx`、`xlsx` 等）。把附加技能复制到 `.codewood/skills/` 即可启用。
