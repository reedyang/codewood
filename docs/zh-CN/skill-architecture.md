# Agent Skills 架构原则

[English](../en/skill-architecture.md) | **简体中文**

本文描述 **Code Wood** 如何加载与使用 **Agent Skills**，写法上刻意让同一套思路可以复用到 **其他 AI 编程助手、agent 或 IDE**（Cursor、Claude Code、Copilot 风格的流程、自定义 MCP 宿主等）。它 **不** 绑定任何单一厂商界面。

上游参考格式：[Anthropic Agent Skills (`anthropics/skills`)](https://github.com/anthropics/skills/blob/main/README.md)。

---

## 1. 目标

- **可移植的技能包**：技能就是一个可以复制、版本化与分享的 **文件夹**；行为记录在 `SKILL.md` 与可选附属文件中。
- **与宿主无关的约定**：技能不得写入 **特定产品** 的环境变量前缀（例如某个 IDE 的名字）。宿主实现通用的发现与桥接机制。
- **宿主不得硬编码具体技能**：运行时不得为技能 **名称** 或脚本文件名做特判（例如 `if skill == "baidu"`）。匹配依据是 **技能包路径**、**声明的元数据** 或 **用户意图**，而不是宿主代码中针对某个技能的字符串字面量。
- **关注点分离**：技能描述 *做什么*；宿主提供工具（`shell`、文件 I/O、MCP 等）并注入上下文。
- **多技能编排**：优先使用 **stdin** 与 **shell 管道** 在步骤之间传递数据；当同一份数据可以在命令行流转时，避免在工作区产生 **可以避免的中间文件**。把推荐做法写进 `SKILL.md`，让贡献者在新增或评审技能时能自查。

---

## 2. 技能包布局

每个技能是一个目录（**技能 id 即文件夹名**）：

```text
<skills_root>/<skill_id>/
  SKILL.md                 # 必填：YAML frontmatter + Markdown 正文（可选字段见 §5）
  scripts/                 # 可选：随技能包分发的可执行文件（例如 *.py）
  ...                      # 其他按需资源
```

---

## 3. 加载顺序与覆盖（合并后的技能目录）

当存在多个根目录时，宿主按 **`skill_id`**（文件夹名）合并技能。典型优先级 **由低到高**：

| 层级 | 典型路径 | 作用 |
|-------------|----------------------------------------|-------------------------------|
| 内置 | `<app>/skills/` | 随应用发布 |
| 用户/配置 | `<config_dir>/skills/`（例如与 `config.jsonc` 同级） | 用户级覆盖 |
| 工作区 | `<workspace>/skills/` | 项目本地技能 |

**相同 `skill_id`**：高优先级层会 **替换** 低优先级层，使 fork 与用户补丁的行为可预期。

只有单一 `skills/` 目录的宿主同样可以遵循 **文件夹即技能 id** 的规则。

---

## 4. `SKILL.md` 约定

- **YAML frontmatter**（位于 `---` 之间），至少包含：
  - **`name`**：人类可读的名称（缺省时回退为文件夹名）。
  - **`description`**：给模型的路由简介（何时使用该技能）。
- **正文**：完整指令、CLI、编排方式、限制 —— 模型需要遵循的一切。

非法的 frontmatter 可能导致宿主 **跳过** 该技能；请保持 YAML 合法。

可选字段 **不要求** 包含宿主专有键。请优先使用中性、可移植的措辞（例如用 "the host's shell or subprocess runner"，而不是某个产品名）。

### 宿主与技能的边界：`SKILL.md` 不应规定什么

单个技能 **看不到** 宿主的完整工具面与其他技能包。`SKILL.md` 只能描述 **该技能包内部** 发生的事情（脚本、参数、输出中约定的分节、重试、安全）。以下内容属于 **宿主** 的提示词与运行时，而不属于任何单个技能文件：

| 应留在宿主（系统/工具提示词、agent 代码） | 应留在技能内（`SKILL.md` + 包内资源） |
|------------------------------------------------------|---------------------------------------------------|
| 任务生命周期：例如何时发出 **request_user_input** | **本** 脚本针对当前查询何时算执行完成（例如 stdout 中的必需标记），以及 "同一查询不要重复运行同一命令" |
| 命名或编排 **其他技能**、MCP 工具，或 "加载技能" 的注入 | 中性措辞：例如 "宿主可能调度的后续步骤不在本技能范围内" |
| 跨技能流水线、**技能包之间** 的 stdin 管道、跨技能 id | 仅 **本** 技能包的 CLI；宿主如何把 `model_context_file_env` 合并进子进程结果 |

**经验规则**

- **不要** 点名提及宿主的控制类工具（`done`、`request_user_input` 等）。
- **不要** 引用其他 **`skill_id`**，也不要指示模型下一步去调用另一个技能。
- **不要** 把 **MCP** 或其他插件命名空间写进本技能的约定，除非仓库定义了适用于所有技能的 **中性、可移植** 模式。
- 当你要表达 shell stdout 或合并后的文件内容时，优先用 **subprocess result** / 合并后的 **`output`**，而不要用 **工具 `output`**，以免与宿主的 JSON 工具 API 混淆。

多技能编排与"完成用户整体目标"的策略属于 **宿主文档**（例如 Code Wood 的 `cli/system_prompt.md`、`cli/tools_prompt.md`），而不是各技能的 `SKILL.md`。

---

## 5. 可选 frontmatter：`model_context_file_env`（扩展工具输出）

有些脚本希望把 **大段文本** 传给模型，而 **不** 全部打印到用户终端。请在 **`SKILL.md` 的 YAML frontmatter** 中声明（与技能其余内容同文件，无需额外附属文件）：

```yaml
---
name: my-skill
description: "..."
model_context_file_env: MY_SKILL_MERGE_OUTPUT
---
```

- **`model_context_file_env`**（或 **`modelContextFileEnv`**）：必须是合法的环境变量名（`[A-Za-z_][A-Za-z0-9_]*`）。
- **语义**：符合约定的宿主 **可以** 创建临时 UTF-8 文件，把该环境变量设为它的 **绝对路径** 传给子进程，并在退出码为 **0** 后把文件内容追加到展示给模型的工具结果中（具体合并格式由宿主定义）。
- **命名**：请选择 **技能专属** 的名称（例如 `BAIDU_SKILL_MERGE_OUTPUT`），**不要** 使用宿主产品前缀。

宿主的职责：

1. 判断 **被调用的脚本路径** 属于哪个技能包（多个匹配时，`bundle_root` 最长匹配者优先）。
2. 从该技能包 `SKILL.md` 解析出的 frontmatter 中读取 **`model_context_file_env`**。
3. 字段缺失或非法时不要创建临时文件。

---

## 6. 技能内部的环境变量

- 脚本应使用 **中性且以技能为前缀** 的名称，例如 `BAIDU_SKILL_VERBOSE`、`DEEPCRAWL_SKILL_INSECURE_SSL`。
- 避免在技能代码的环境变量中内嵌 **宿主产品** 名称（可移植性与清晰性）。

---

## 7. 调用随包脚本

- 宿主通常在 **用户工作区当前目录** 下执行命令，**不会** 自动 `cd` 到技能目录。
- 工具与提示词应告诉模型用 **绝对路径** 调用脚本：`<bundle_root>/scripts/...`。
- 在系统提示词中列出探测到的 `scripts/*.py` 路径，可提升跨工具的复制粘贴可靠性。

---

## 8. 宿主 **不应** 做的事

- 不要为通用行为（合并输出、SSL 等）按 **具体 `skill_id`** 或脚本文件名分支。
- 不要要求技能使用只有单一产品能理解的 **宿主私有** YAML 键；frontmatter 中可选的 **`model_context_file_env`** 是 **有文档、可移植** 的字段（见 §5），而不是产品专属秘密。
- 不要删除 `SKILL.md` 中由技能作者写下的 **时效性 / 安全性** 规则；它们属于技能本身，而不应散落成宿主里的一次性检查。

---

## 9. 面向其他 AI 编程工具的兼容性说明

| 关注点 | 可移植做法 |
|--------|------|
| **系统提示词** | 注入技能索引 + 完整 `SKILL.md` 正文（或通过"加载技能"工具按需注入）。 |
| **工具命名** | 把你的工具名（`run_terminal_cmd`、`execute_shell` 等）映射到与 `shell` 相同的 *意图*；技能保持中立。 |
| **路径** | 示例中使用操作系统原生绝对路径；除常规路径规则外，不要假设 WSL 与 Windows 的差异。 |
| **MCP / 插件** | 技能保持基于文件；MCP 服务器可以镜像同样的目录布局。 |

---

## 10. Code Wood 的对应实现（参考实现）

在本仓库中：

- 加载器：`cli/skills_loader.py`
- 子进程 `shell` 的合并 / `model_context_file_env` 处理：`cli/agent.py`（通过 `cli/skills_loader.py` 从匹配技能的 `SKILL.md` frontmatter 解析环境变量名）
- 面向工具的描述：`cli/tools_prompt.md`

其他产品可以实现同样的 **原则**，而无需照搬实现细节。

---

## 11. 最小实现草图：Skill Context Pack

为在不改变既有技能执行流程的前提下改善大仓库导航，Code Wood 可以在注入技能提示词时前置一段紧凑的 "Skill Context Pack"。

**范围（最小、向后兼容）：**

- 保持既有 `request_skill_prompt` 行为与完整技能正文注入不变。
- 在完整正文之前增加结构化摘要，改善第一步规划。
- 不新增工具，不改变任务循环语义。

**本地技能包（基于 bundle）：**

- `skill_id`、`bundle_root`、`SKILL.md` 绝对路径
- 探测到的随包脚本（`scripts/*.py`，数量有上限）
- 探测到的参考文件（`references/*.md`，数量有上限）
- `SKILL.md` 正文的前几个 Markdown 标题

**MCP prompt 技能包：**

- 来源服务器、prompt id
- 渲染后的消息条数 / 字符数
- 简洁的执行提示

**Code Wood 当前实现说明：**

- Context Pack 在 `cli/agent.py`（`_build_single_skill_prompt`）中前置，本地与 MCP 技能路径都适用。
- 对较长的 `SKILL.md`，Code Wood 先注入 `Context Pack + 前 N 个分节`，之后允许通过 `request_skill_prompt` 的参数（`section` 或 `full=true`）按需展开。
- 运行时技能合并优先级为：`builtin -> config_dir -> workspace`（按 `skill_id` 高层覆盖低层）。

---

## 文档历史

- 初版用于记录 Code Wood 及兼容 agent 中 Agent Skills 的宿主—技能边界与可移植性预期。
- 增加多技能编排原则：优先 stdin/管道，避免可避免的中间文件。
- 记录了 `SKILL.md` 的 **宿主—技能边界**：单个技能内不写 `done`/其他技能/MCP 编排；这些规则属于宿主提示词。
