# 配置

[English](../en/configuration.md) | **简体中文**

`model_providers` 为必填项。

在用户目录下创建 `.codewood/config.jsonc`：

```json
{
  "model_providers": [
    {
      "provider": "openai",
      "params": {
        "api_key": "YOUR_API_KEY",
        "base_url": "YOUR API BASE URL",
        "api_mode": "auto",
        "models": [
          {
            "name": "gpt-oss-120b",
            "context_window": "128K",
            "streaming": true,
            "use_clean_content": false,
            "multimodal": false
          },
          { "name": "gpt-4o-mini", "context_window": 64000, "streaming": false }
        ]
      }
    },
    {
      "provider": "ollama",
      "params": {
        "api_mode": "ollama",
        "port": 11434,
        "models": [
          { "name": "qwen2.5vl:3b", "context_window": "96k", "streaming": true }
        ]
      }
    }
  ],
  "execution_policy": "moderate",
  "auto_compact_trigger_percent": 80,
  "max_tool_rounds": 30,
  "security_audit_model": "openai/gpt-4o-mini",
  "memory_enabled": false,
  "language": "zh-CN"
}
```

## 支持的提供方与平台

决定请求如何发送的是 `api_mode`，而不是硬编码的提供方清单，因此任何 OpenAI 兼容端点都能接入：

- `auto`（默认）—— OpenAI 兼容 HTTP API，根据 `base_url` 自动探测 `/chat/completions` 与 `/responses`
- `chat` —— OpenAI 兼容 HTTP API，强制 `/chat/completions`
- `responses` —— OpenAI 兼容 HTTP API，强制 `/responses`
- `ollama` —— 本地原生 Ollama API（使用 `port`）

GUI 内置了会自动填好 `base_url` 与 `api_mode` 的平台预设，因此只需填 API key：

| 预设 | Base URL |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| OpenAI | `https://api.openai.com/v1` |
| 智谱 (Zhipu) | `https://open.bigmodel.cn/api/paas/v4` |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 豆包 (Doubao) | `https://ark.cn-beijing.volces.com/api/v3` |
| 月之暗面 (Moonshot) | `https://api.moonshot.cn/v1` |
| MiniMax | `https://api.minimax.chat/v1` |
| SenseNova (商汤日日新) | `https://token.sensenova.cn/v1` |
| Mimo (小米) | `https://api.mimo.xiaomi.com/v1` |
| Google | `https://generativelanguage.googleapis.com/v1beta/openai` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Agnes AI | `https://apihub.agnes-ai.com/v1` |
| Ollama | 本地（`api_mode: "ollama"`，默认端口 `11434`） |
| Custom API | 自行填写 |

在必要之处会做提供方针对性处理：DeepSeek 的响应会做缓存命中/未命中统计与包含推理的输出 token 计数；对所有提供方，流式输出中的隐藏思考块（`<think>…</think>` 等）都会被剥离。

## 配置说明

- `model_providers`：有序的模型提供方列表；Code Wood 默认使用第一个
- `model_providers[i].provider`：自由命名的标签，仅作为模型选择器的前缀（例如 `openai/gpt-4o`、`ollama/qwen2.5vl:3b`）；它 **不** 参与 API 调用分发，分发由 `api_mode` 决定
- `model_providers[i].params.api_mode`：选择 API 调用方式
  - `auto`（默认）：OpenAI 兼容 HTTP API；根据 `base_url` 后缀自动探测 `/chat/completions` 与 `/responses`
  - `chat`：OpenAI 兼容 HTTP API；强制使用 `/chat/completions`
  - `responses`：OpenAI 兼容 HTTP API；强制使用 `/responses`
  - `ollama`：本地 Ollama HTTP API（使用 `port`；忽略 `api_key`/`base_url`）
  - 为向后兼容，省略 `api_mode` 且设置 `provider: "ollama"` 的配置仍按 `api_mode: "ollama"` 处理
- `model_providers[i].params.port`：由 `api_mode: "ollama"` 使用，默认 `11434`
- `model_providers[i].params.models`：模型列表；默认使用第一个模型
  - 字符串形式：`"gpt-oss-120b"` 使用默认的 `context_window=128K`（即 `131072`）与 `streaming=true`
  - 对象形式：`{"name":"gpt-oss-120b","context_window":"128K","streaming":true,"use_clean_content":false,"multimodal":false,"extra_headers":{"X-Model":"gpt-oss-120b"}}`
- `context_window`：接受正整数或匹配 `^\d+[kKmM]?$` 的字符串，其中 `1K = 1024`、`1M = 1024K`（例如 `"128K"` = `131072`）；非法或缺失时回退为 `128K`（`131072`）
- 所有模型共用同一套上下文拼装逻辑：无论 `context_window` 大小，Code Wood 都会发送完整的系统提示词、工具提示词、技能提示词、记忆与运行上下文（仅 token 预算比例随窗口大小调整）
- `streaming`：逐模型流式开关，默认 `true`
- `use_clean_content`：逐模型历史清理开关，默认 `false`。开启后，在回放历史给模型时优先使用存储的 `_clean_content` 而非原始 assistant `content`
- `multimodal`：逐模型图像输入能力，默认 `true`。设为 `false` 时，Code Wood 会从 `read` 工具中隐藏图像输入能力。若仍需分析图像，可通过 `run_subagent` 的 `image` 参数委派给多模态子代理（见 `additional-subagents/image-analyzer.md`）
- `extra_headers`：逐模型自定义请求头，仅对 OpenAI 兼容的 `api_mode`（`auto`/`chat`/`responses`）可用
- `reasoning_effort`：可选的逐模型推理强度级别列表（例如 `["low","medium","high"]`）。设置后可按对话选择级别 —— TUI 中通过 `/model reasoning <level>`，GUI 中通过模型菜单 —— 并以 `reasoning_effort`（chat API）或 `reasoning.effort`（responses API）发送给提供方。所选级别按对话保存并在重新加载时恢复。省略或留空则关闭该模型的推理强度选择。对象形式示例：`{"name":"gpt-oss-120b","context_window":"128K","reasoning_effort":["low","medium","high"]}`
- `auto_compact_trigger_percent`：自动摘要阈值，默认 `80`
- `security_audit_model`：安全审核调用（如脚本风险评估、命令可逆性分类）可选使用的模型选择器，格式为 `"provider/model_name"`，例如 `"openai/gpt-4o-mini"`。设置后，所有 `freedom_combined_review` 与 `minimal_classifier` AI 调用都改用该模型而非默认对话模型。留空或设为 `""` 表示安全审核使用默认对话模型。该设置位于 `config.jsonc` 顶层，并可在 GUI 的安全设置页配置
- `model_providers[i].params`：提供方专有参数，如 API key 与 base URL
- `config.jsonc` 中所有字符串值都支持 `${ENV_NAME}` 形式的环境变量占位符
- 占位符会自动做类型转换，包括 `bool`、`int`、`float`、`null` 以及 JSON `list` / `dict`
- `language`：界面语言，取值为 `en`（默认）或 `zh-CN`。GUI 会在「设置 → 通用」写入同一字段，终端 UI 通过 `/language` 修改
  - 简体中文可接受的别名包括 `zh`、`zh-CN`、`zh_CN`、`zh-Hans`、`chinese`、`简体中文`；其他值回退为 `en`
  - 终端与后端的文案位于 `cli/config/locales/<language>.json`；GUI 自带前端语言包，key 集合一致
