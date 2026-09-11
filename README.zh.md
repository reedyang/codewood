# Code Wood

[English](README.md) | **简体中文**

Code Wood —— 桌面端 AI 编程助手，内置 Agent Skills、MCP 工具，并附带终端 CLI。

## 特色功能

### 桌面 GUI

- 原生桌面应用（Windows 使用 WebView2，macOS/Linux 使用 WebKit），承载 React 界面，与终端共用同一套 agent 内核
- 流式对话与富文本输入框：Markdown 渲染、代码高亮、图片附件、就地 diff 预览、逐轮步骤时间线
- 全部可视化可点击：工作区与对话管理、侧边栏导航、全局对话搜索、归档对话，以及显示当前模型/工作区状态的底部状态栏
- 所有 `/workspace`、`/chat` 命令都有对应 UI 操作，无需记忆斜杠命令
- 明暗主题，以及主题/语言/模型/MCP/技能/子代理/工具/安全等设置页
- 内置面板：展示各分块 token 占用的 **Dashboard**、可实时观看 agent 输入命令的 **内置终端**、可预览本地 HTML 或浏览网页的 **浏览器** 标签页
- **全量对话全文检索**：一个搜索框检索所有会话（跨工作区）的历史，直接跳转到命中的消息
- **窗口背景图片**：支持拖入图片作为背景，并可用透明度滑块调节，兼顾观感与可读性
- 一个启动器即可无控制台窗口打开 GUI（`codewood-gui`），GUI 进程与后端进程并存，额外开销极小

### 模型可真正驱动的工具

- **内置终端是真实可用的共享控制台**：模型通过它执行命令并读回输出，因此可以编译、运行、调试你的程序，你也能在终端面板里实时看到全过程
- **内置浏览器**是为模型准备的网页环境：可打开 URL、执行 JavaScript、读取渲染后页面的 DOM 与 console，用来调试网页代码
- **工具与技能可按需开闭**：在「设置 → 工具」「设置 → 技能」中逐个开关；被禁用的工具会从发给模型的 schema 中移除，不消耗 token
- **极简模式（Compact mode）**：一键关闭所有可选工具得到最小工具集，再一键全部恢复

### 多语言界面

- **英文与简体中文**界面，GUI 中通过「设置 → 通用」、终端中通过 `/language` 随时切换，无需重启

### 模型提供方与 API 兼容

- **一套配置适配所有提供方**：`model_providers` 是有序列表，每一项包含 `api_key` / `base_url` / `api_mode` 以及逐模型选项
- **由 `api_mode` 决定调用形态**：`chat` 强制使用 OpenAI 兼容的 `/chat/completions`，`responses` 强制使用 OpenAI 兼容的 `/responses`，`ollama` 走本地 Ollama 服务，默认的 `auto` 会自动探测两者并适配
- **开箱即用的平台预设**，只需填 API key（base URL 与 API 模式自动填好）：DeepSeek、OpenAI、智谱 (Zhipu)、Qwen (DashScope)、豆包 (Doubao)、月之暗面 (Moonshot)、MiniMax、SenseNova (商汤日日新)、Mimo (小米)、Google、OpenRouter、Agnes AI、本地 **Ollama**，以及完全自定义的端点
- **任何 OpenAI 兼容 API 都能接入**，包括自建网关与本地服务 —— 分发由 `api_mode` 决定而非硬编码的提供方清单；需要自定义路由头的网关可用逐模型 `extra_headers`
- **逐模型精细控制**：上下文窗口、流式开关、多模态图像输入、历史清理（`use_clean_content`）、推理强度级别与自定义请求头
- **GUI 内发现模型**：选择平台、填入 key，刷新即可直接从提供方的 `/models` API 拉取模型列表（及上下文窗口值）
- **按提供方做针对性处理**：DeepSeek 的缓存命中/未命中统计、推理 token 计数，以及从流式输出中剥离隐藏的 `<think>` 类思考块

### 模型效率与上下文

- 自然语言指令处理，模型提供方完全可配置
- **极致的缓存命中优化**：历史被序列化为字节稳定的前缀，只在尾部增长，使提供方的 prompt 前缀缓存持续命中 —— 配合 DeepSeek API 可达 **99% 以上命中率**，同时降低延迟与成本
- **Dashboard** 展示缓存命中/未命中与各分块 token 占用，收益一目了然
- **Agent Skills**：自动从 `skills/` 目录（及其他四个层级）发现技能，任务匹配描述时自动注入

### 项目级指令

- **支持 AGENTS.md**：在仓库里放一个 `AGENTS.md`，Code Wood 就会把它作为用户自定义指令注入，无需逐工具配置或额外文件
- 指令来自全局配置目录 **以及** 从仓库根目录到当前工作区的整条路径上的每一层，因此 monorepo 既能定义仓库级规范，也能为子包写局部覆盖
- 与 `AGENTS.md` 同目录放置 `AGENTS.override.md` 即可在该目录中覆盖它，便于保留已提交文件的同时加入本地指令
- 文件变更后会被重新读取，修改指令无需重启应用
- 与 `AGENTS.md` 冲突时，显式选择的技能与显式指定的 MCP 目标优先级更高
- **项目代码语义索引**：对工作区构建增量 BM25 + 向量索引（文件监听保持新鲜），并以工具形式提供文本检索与调用者/被调用者调用图查询

### 安全与可控

- **AI 安全审核**：脚本执行、命令可逆性等风险操作由专用且可配置的审核模型分类（设置 → 安全）
- **Shell 沙箱**：以操作系统级隔离层运行 AI 发起的命令，提供 `read_only`、`workspace_write`、`full_access` 三级，另有网络开关（暂只支持 Windows，见 [Shell 沙箱](docs/zh-CN/sandbox.md)）
- 执行策略与确认护栏，配合可编辑的确认白名单
- 内置 MCP 支持：资源加载、批量工具调用，URL 型服务器支持 OAuth 2.0
- **子代理**：拥有独立模型、指令与工具白名单的嵌套 agent 循环，还可把图像理解委派给多模态模型
- **Plan 模式**：只做方案设计不执行变更的模式

## 快速开始

### 环境要求

- Python 3.12+
- 调用 AI 模型所需的网络访问

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动桌面 GUI

```bash
# 开发环境
python cli/main.py app

# 打包版本
codewood app
```

GUI 需要前端产物：打包版本已内置，源码运行需先构建一次：

```bash
cd desktop/frontend
npm install
npm run build
```

完整架构、serve 模式、前端热更新与打包细节见 [桌面 GUI](docs/zh-CN/gui.md)。

### 启动终端 UI

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

终端 UI 与 GUI 共用同一套 agent 内核（对话、工作区、模型、技能、子代理、MCP、Plan/Agent 模式）。命令参考见 [终端 UI](docs/zh-CN/tui.md)，斜杠命令见 [内置命令](docs/zh-CN/ai-features.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [桌面 GUI](docs/zh-CN/gui.md) | GUI 架构、serve 模式、开发与打包 |
| [终端 UI](docs/zh-CN/tui.md) | TUI 入口与斜杠命令参考 |
| [配置](docs/zh-CN/configuration.md) | `config.jsonc`、模型提供方与逐模型参数 |
| [MCP 配置](docs/zh-CN/mcp.md) | `mcp.jsonc`、可用的 MCP 动作、OAuth 2.0 |
| [Agent Skills](docs/zh-CN/skills.md) | 加载路径、优先级、开关方式、内置脚本 |
| [子代理](docs/zh-CN/subagents.md) | 子代理定义、frontmatter、图像委派 |
| [AGENTS.md](docs/zh-CN/agents-md.md) | 全局与项目指令文件、覆盖文件、优先级 |
| [AI 特性](docs/zh-CN/ai-features.md) | 执行策略、确认白名单、内置命令与 shell 命令 |
| [Shell 沙箱](docs/zh-CN/sandbox.md) | 沙箱级别与各平台实现 |
| [Agent Skills 架构](docs/zh-CN/skill-architecture.md) | 可移植技能的设计原则 |
| [项目结构](docs/zh-CN/project-structure.md) | 仓库目录结构 |
| [故障排查](docs/zh-CN/troubleshooting.md) | 常见配置问题 |

## 界面截图

### 桌面 GUI

![Code Wood 桌面 GUI](docs/images/gui-snapshot.png)

### 终端 UI

![Code Wood 终端 UI](docs/images/tui-snapshot.png)

## 贡献

欢迎提交 Issue 与 Pull Request。

## 许可证

MIT License
