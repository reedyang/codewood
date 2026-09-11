# Configuration

**English** | [简体中文](../zh-CN/configuration.md)

`model_providers` is required.

Create `.codewood/config.jsonc` in your user directory:

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
  "language": "en"
}
```

## Supported providers and platforms

`api_mode` — not a hard-coded provider list — decides how a request is sent, so
any OpenAI-compatible endpoint works:

- `auto` (default) — OpenAI-compatible HTTP API, probing `/chat/completions` and `/responses` from the `base_url`
- `chat` — OpenAI-compatible HTTP API, forcing `/chat/completions`
- `responses` — OpenAI-compatible HTTP API, forcing `/responses`
- `ollama` — native local Ollama API (uses `port`)

The GUI ships presets that fill in `base_url` and `api_mode` for you, so only an
API key is needed:

| Preset | Base URL |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| OpenAI | `https://api.openai.com/v1` |
| 智谱 (Zhipu) | `https://open.bigmodel.cn/api/paas/v4` |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 豆包 (Doubao) | `https://ark.cn-beijing.volces.com/api/v3` |
| 月之暗面 (Moonshot) | `https://api.moonshot.cn/v1` |
| MiniMax | `https://api.minimax.chat/v1` |
| SenseNova (商汤日日新) | `https://token.sensenova.cn/v1` |
| Mimo (Xiaomi) | `https://api.mimo.xiaomi.com/v1` |
| Google | `https://generativelanguage.googleapis.com/v1beta/openai` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Agnes AI | `https://apihub.agnes-ai.com/v1` |
| Ollama | local (`api_mode: "ollama"`, default port `11434`) |
| Custom API | anything you type |

Provider-specific behavior is applied where it matters: DeepSeek responses get
cache-hit/miss accounting and reasoning-aware output-token counting, and hidden
reasoning blocks (`<think>…</think>` and equivalents) are stripped from streamed
output for every provider.

## Configuration Notes

- `model_providers`: ordered list of model providers; Code Wood uses the first provider by default
- `model_providers[i].provider`: free-form label used only as the model selector prefix (e.g. `openai/gpt-4o`, `ollama/qwen2.5vl:3b`); it does NOT participate in API-call dispatch — that is decided by `api_mode`
- `model_providers[i].params.api_mode`: selects the API call method
  - `auto` (default): OpenAI-compatible HTTP API; auto-probes `/chat/completions` and `/responses` based on `base_url` suffix
  - `chat`: OpenAI-compatible HTTP API; forces `/chat/completions`
  - `responses`: OpenAI-compatible HTTP API; forces `/responses`
  - `ollama`: local Ollama HTTP API (uses `port`; ignores `api_key`/`base_url`)
  - For backward compatibility, configurations that omit `api_mode` and set `provider: "ollama"` are still treated as `api_mode: "ollama"`
- `model_providers[i].params.port`: used by `api_mode: "ollama"`, with a default of `11434`
- `model_providers[i].params.models`: model list; the first model is used by default
  - String form: `"gpt-oss-120b"` uses the default `context_window=128K` (= `131072`) and `streaming=true`
  - Object form: `{"name":"gpt-oss-120b","context_window":"128K","streaming":true,"use_clean_content":false,"multimodal":false,"extra_headers":{"X-Model":"gpt-oss-120b"}}`
- `context_window`: accepts a positive integer or a string matching `^\d+[kKmM]?$`, where `1K = 1024` and `1M = 1024K` (e.g. `"128K"` = `131072`); invalid or missing values fall back to `128K` (`131072`)
- All models share one context-packing logic: Code Wood sends the full system prompt, tool prompts, skill prompts, memory, and operational context regardless of `context_window` (only the token budget ratios adapt to the window size)
- `streaming`: per-model streaming toggle, default `true`
- `use_clean_content`: per-model history-cleaning toggle, default `false`. When enabled, Code Wood prefers stored `_clean_content` over raw assistant `content` when replaying prior history to the model
- `multimodal`: per-model image-input capability, default `true`. When set to `false`, Code Wood hides image-input capability from the `read` tool. To still analyze images, delegate to a multimodal sub-agent via `run_subagent`'s `image` argument (see `additional-subagents/image-analyzer.md`)
- `extra_headers`: per-model custom request headers, available only for OpenAI-compatible `api_mode` values (`auto`/`chat`/`responses`)
- `reasoning_effort`: optional per-model list of reasoning-effort levels the model supports (e.g. `["low","medium","high"]`). When set, a level can be selected per chat — in the TUI via `/model reasoning <level>` and in the GUI model menu — and the choice is sent to the provider as `reasoning_effort` (chat API) or `reasoning.effort` (responses API). The selected level is saved per chat and restored on reload. Omit or leave empty to disable reasoning-effort selection for the model. Object-form example: `{"name":"gpt-oss-120b","context_window":"128K","reasoning_effort":["low","medium","high"]}`
- `auto_compact_trigger_percent`: automatic summarization threshold, default `80`
- `security_audit_model`: optional model selector for security review calls (e.g. script risk assessment and command reversibility classification). Use `"provider/model_name"` format, e.g. `"openai/gpt-4o-mini"`. When set, all `freedom_combined_review` and `minimal_classifier` AI calls use this model instead of the default chat model. Leave empty or `""` to use the default chat model for security review. This setting lives at the top level of `config.jsonc` and is configurable via the GUI Security settings page.
- `model_providers[i].params`: provider-specific parameters such as API keys and base URLs
- All string values in `config.jsonc` support environment variable placeholders of the form `${ENV_NAME}`
- Placeholders are type-converted automatically, including `bool`, `int`, `float`, `null`, and JSON `list` / `dict` values

## Interface language

- `language`: interface language, stored as `en` (default) or `zh-CN`. The GUI writes the same key from Settings → General, and the terminal UI changes it with `/language`.
- Accepted aliases for Simplified Chinese include `zh`, `zh-CN`, `zh_CN`, `zh-Hans`, `chinese`, and `简体中文`; anything else falls back to `en`.
- Locale strings for the terminal and backend live in `cli/config/locales/<language>.json`; the GUI carries its own frontend bundle with the same key set.
