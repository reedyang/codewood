# 子代理

[English](../en/subagents.md) | **简体中文**

子代理是用户配置的助手，运行拥有独立模型、系统指令与工具白名单的嵌套 agent 循环。主模型基于每个子代理的 `description`，通过唯一的 `run_subagent` 工具自动调用子代理，子代理的最终文本结果会被注入回主循环。

以 Markdown 文件 + YAML frontmatter 的方式定义子代理，一个文件一个：

- 用户级子代理位于 `~/.codewood/subagents/<name>.md`
- 工作区子代理位于 `<workspace>/.codewood/subagents/<name>.md`（同名时工作区覆盖用户级）

frontmatter 字段（Markdown 正文即该子代理独立的系统指令）：

- `name`（必填）—— 子代理 id
- `description`（必填）—— 驱动主模型自动选择的使用场景描述
- `model`（可选）—— 引用 `model_providers` 的 `provider/model` 选择器；默认使用主模型
- `tools`（可选）—— 工具名白名单；默认是一组核心编程工具（`shell`、`apply_patch`、`read`、`project_context_search`、`update_plan`、`request_skill_prompt`）。显式写空列表（`tools: []`）表示不授予任何工具。`run_subagent` 始终被排除，因此子代理不能嵌套调用子代理。
- `max_rounds`（可选）—— 子代理返回前允许的最大工具调用轮数（默认 20）

`run_subagent` 工具还接受可选的 `image` 参数（文件路径）。图像会交给 **子代理自身的模型** 而非主模型分析 —— 因此非多模态主模型可以把图像理解委派给多模态子代理。`additional-subagents/image-analyzer.md` 是一个现成示例，可把 UI 设计稿、截图、图表或流程图转成编程模型可直接使用的结构化描述。

在 GUI 中打开 **设置 → 子代理** 可以查看和管理已配置的子代理。

## 示例子代理

`additional-subagents/` 提供现成示例（例如 `image-analyzer.md`）。复制到 `.codewood/subagents/` 即可启用。
