# AGENTS.md

[English](../en/agents-md.md) | **简体中文**

Code Wood 会读取 `AGENTS.md` 文件并作为用户自定义指令注入系统提示词，因此仓库可以自带编码规范、提示与约束，无需任何逐项目配置。

## 加载位置

会扫描两类位置：

1. **全局** —— Code Wood 的配置目录（存放 `config.jsonc` 的目录，默认为 `~/.config/codewood/`，或由 `$CODEWOOD_HOME` 指定）。
2. **项目** —— 当前工作区，以及从仓库根目录到工作区路径上的每一层目录：
   - 若工作区位于 Git 仓库内，链条从仓库根（最近的含 `.git` 的上级目录）开始，逐层向下直到工作区。
   - 若不在仓库内，只读取工作区目录本身（不向上搜索父目录）。

这让 monorepo 用起来很自然：根目录的 `AGENTS.md` 作用于全部内容，而嵌套的 `src/api/AGENTS.md` 为该子树追加或覆盖规则。

目录会去重，即使多条路径解析到同一个文件，该文件也只注入一次。

## `AGENTS.md` 与 `AGENTS.override.md`

每个目录可以同时存在这两个文件：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | 该目录的常规指令，通常会提交到仓库 |
| `AGENTS.override.md` | 在同一目录中优先于 `AGENTS.md` |

由于每个目录只使用一个文件，`AGENTS.override.md` 就是让本地、针对单机或实验性的指令不进入已提交 `AGENTS.md` 的方式。

## 内容如何注入

- 加载到的文件会以 `## User Custom Prompts (AGENTS.md)` 章节追加到系统提示词，每段都标注作用域与来源路径（`### global · AGENTS.md`、`### project · src/api · AGENTS.md`）。
- 合并后的章节上限为 **32 KiB**，超出部分会被丢弃。
- 文件变化（存在性、大小或修改时间）后会被重新读取，修改内容无需重启 Code Wood 即在下一次请求生效。
- 文件中的相对路径与其他说明，会像普通提示词文本一样由模型理解。

## 优先级

指令冲突时，意图越具体者优先：

1. 当前请求中显式选择的技能（`/skills/<skill-name>` 或触发的 `request_skill_prompt`）优先于 `AGENTS.md`。
2. 显式指定的 MCP 服务器/工具目标优先于 `AGENTS.md` 与通用规则，但硬性安全与权限约束除外。
3. 其余情况下，`AGENTS.md` 内容与基础系统提示词共同生效。

## 相关文档

- [Agent Skills](skills.md)
- [配置](configuration.md)
- [AI 特性](ai-features.md)
