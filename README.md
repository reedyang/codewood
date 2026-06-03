# Smart Shell

Smart Shell is an LLM-powered shell that understands natural language, automates user tasks.

## Highlights

- Natural language command handling powered by configurable model providers
- Clear separation between temporary session scripts and long-lived text files
- Automatic loading of Agent Skills from the `skills/` directory
- Unified model provider configuration with per-model settings
- Built-in MCP support, including resource loading, batch tool calls, and OAuth 2.0 for URL-based servers
- Execution policies and confirmation guardrails for safer automation

## Quick Start

### Requirements

- Python 3.8+
- Network access for AI model calls

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run the App

```bash
python src/main.py

# Start with a specific workspace by name or path
python src/main.py <workspace name or path>

# Run one task and exit
python src/main.py exec "your task request"

# Start with a workspace and run one task before exiting
python src/main.py <workspace name or path> exec "your task request"

# Choose a model at startup
python src/main.py --model <model name>
python src/main.py -m <model name>
```

## AI Features

### Execution Policy

Use `execution_policy` in `.smartshell/config.jsonc` to control how potentially risky actions are handled:

- `confirmation`: ask for y/n confirmation for every action that needs approval
- `moderate`: automatically execute safe actions after evaluating risk
- `unlimited`: skip safety checks and execute directly

Switch policies with:

```bash
/execution-policy <unlimited|moderate|confirmation>
```

Temporary scripts created through the built-in `script` command are considered session-local work items. If you later run that script through `shell` and it exits with code `0`, Smart Shell will attempt to delete it automatically so temporary files do not linger. If you want to keep a script permanently, use `text_file` to create it in the current working directory instead.

### Always-Confirm and `confirm_allowlist.json`

When free mode is disabled and interactive confirmation is still required, the prompt offers `a` or `always` only for `shell` commands. That means only the current command is added to the allowlist (`shell_script_paths` / `shell_exe_tokens`), not every command globally.

- `script` output files and `text_file` writes remain y/n only
- `shell` execution of a script created in the same session also remains y/n only
- The allowlist file lives next to `config.jsonc` as `confirm_allowlist.json`
- Legacy `shell_commands` entries are converted into the newer v2 structure automatically at startup
- `/always_confirm reset` deletes the file and restores the default prompt behavior

### Agent Skills

Smart Shell follows the same layout as [Anthropic Agent Skills](https://github.com/anthropics/skills/blob/main/README.md). Create a `skills/` directory next to `config.jsonc`, and place one skill per folder containing a `SKILL.md` file with YAML frontmatter and Markdown body.

- Project-local skills live under `.smartshell/skills/<skill-name>/SKILL.md`
- User-level skills live under `~/.smartshell/skills/...`
- Skill folders can include helper files such as `scripts/*.py`
- Relative paths in a skill body are resolved from that skill folder
- The runtime injects the absolute skill bundle root and detected scripts into the system prompt
- During startup, Smart Shell scans and parses all available skills and uses them when a task matches a skill description

### Built-In Commands vs Native Shell Commands

- Built-in commands that do not go through AI must start with `/`, for example `/exit`, `/help`, `/clear screen`, `/clear context`, and `/free`
- Native shell commands or scripts that should run directly must start with `!`, for example `!dir` or `!git status`
- Any input that does not start with `/` is treated as natural language and sent to the AI

## Project Structure

```text
smart-shell/
├── src/                           # Core application code
├── skills/                        # Built-in Agent Skills
├── additional-skills/             # Extra skills; copy them into .smartshell/skills if needed
├── docs/                          # Design and reference documentation
├── demo/                          # Demo assets
├── tests/                         # Test suite
├── requirements.txt               # Python dependencies
├── smartshell.bat                 # Windows launch script
└── README.md                      # Project documentation
```

## Configuration

`model_providers` is required.

Create `.smartshell/config.jsonc` in your user directory:

```json
{
  "model_providers": [
    {
      "provider": "openai",
      "params": {
        "api_key": "${HAPPYCODING_API_KEY}",
        "base_url": "https://happycoding.corp.zoom.com/api/v1",
        "api_mode": "auto",
        "models": [
          {
            "name": "gpt-oss-120b",
            "context_window": "128K",
            "streaming": true,
            "extra_headers": {
              "X-Model": "gpt-oss-120b"
            }
          },
          { "name": "gpt-4o-mini", "context_window": 64000, "streaming": false }
        ]
      }
    },
    {
      "provider": "ollama",
      "params": {
        "port": 11434,
        "models": [
          { "name": "qwen2.5vl:3b", "context_window": "96k", "streaming": true }
        ]
      }
    }
  ],
  "execution_policy": "moderate",
  "project_context_first_round_evidence": true,
  "auto_compact_trigger_percent": 60,
  "max_tool_rounds": 30,
  "memory_enabled": false,
  "mcp_tools_enabled": false
}
```

### Configuration Notes

- `model_providers`: ordered list of model providers; Smart Shell uses the first provider by default
- `model_providers[i].provider`: supports `ollama` and `openai`
- `model_providers[i].params.port`: used only by `ollama`, with a default of `11434`
- `model_providers[i].params.api_mode`: used only by `openai`, and supports `auto` (default), `chat`, and `responses`
  - `auto`: automatically selects between Chat Completions and Responses API
  - `chat`: uses the Chat Completions API
  - `responses`: uses the Responses API
- `model_providers[i].params.models`: model list; the first model is used by default
  - String form: `"gpt-oss-120b"` uses the default `context_window=128000` and `streaming=true`
  - Object form: `{"name":"gpt-oss-120b","context_window":"128K","streaming":true,"extra_headers":{"X-Model":"gpt-oss-120b"}}`
- `context_window`: accepts a positive integer or a string matching `^\d+[kKmM]?$`; invalid values fall back to `128000`
- When `context_window < 64000`, Smart Shell skips system prompts, tool prompts, skill prompts, memory, and operational context, and only sends conversation history plus the current user input
- `streaming`: per-model streaming toggle, default `true`
- `extra_headers`: per-model custom request headers, available only for the `openai` provider
- `auto_compact_trigger_percent`: automatic summarization threshold, default `60`
- `model_providers[i].params`: provider-specific parameters such as API keys and base URLs
- `mcp_tools_enabled`: enables MCP management tools. When `false`, the following tools are unavailable: `mcp_server_info`, `mcp_disable_tools`, `mcp_enable_tools`, `mcp_list_disabled_tools`, `mcp_sampling_create_message`, and `mcp_completion_complete`
- All string values in `config.jsonc` support environment variable placeholders of the form `${ENV_NAME}`
- Placeholders are type-converted automatically, including `bool`, `int`, `float`, `null`, and JSON `list` / `dict` values

## MCP Configuration

Smart Shell automatically reads `mcp.jsonc` from the same directory as `config.jsonc`:

- If the file exists and is valid, `mcpServers` is loaded and injected into the system prompt
- MCP server tool metadata is preloaded asynchronously in the background so the UI stays responsive
- If the file is missing or invalid, Smart Shell keeps running and simply starts with an empty MCP server list
- Store secrets in environment variables instead of committing them in plain text
- MCP connection and retry logs are written to `workspace/logs/mcp_manager.log` rather than the terminal

### Available MCP Actions

- `mcp_status`: show preload status, success and failure lists, and per-server details
- `mcp_status_refresh`: refresh MCP status for all or selected servers
- `mcp_list_tools`: list tools for a specific server
- `mcp_reconnect`: force a reconnect and refresh the cached tools for a server
- `mcp_call_tool`: call a specific tool
- `mcp_call_tool_batch`: call multiple tools in one JSON-RPC batch request, with optional partial-failure handling and summary counts
- `mcp_list_resources`: list resources for a specific server
- `mcp_read_resource`: read a resource URI
- `mcp_list_resource_templates`: list resource templates for a specific server
- `mcp_list_prompts`: list prompts for a specific server
- `mcp_get_prompt`: fetch a prompt result by name and parameters
- `mcp_sampling_create_message`: use the sampling capability to create a message, requires `mcp_tools_enabled=true`
- `mcp_completion_complete`: use the completion capability, requires `mcp_tools_enabled=true`
- `mcp_server_info`, `mcp_disable_tools`, `mcp_enable_tools`, and `mcp_list_disabled_tools`: require `mcp_tools_enabled=true`
- Failure states are classified as `unsupported`, `missing_dependency`, or `connect_failed`, and `mcp_status` returns suggested fixes

### OAuth 2.0 for URL-Based MCP Servers

URL transport supports OAuth flows automatically after a `401 Unauthorized` challenge:

- Parses `WWW-Authenticate`, including `resource_metadata` and `scope`
- Discovers Protected Resource Metadata and Authorization Server Metadata
- Uses Authorization Code + PKCE with `S256`
- Saves and loads tokens from `<config_dir>/oauth_tokens.json` and refreshes them when possible
- Attempts Dynamic Client Registration when `client_id` is not configured and the authorization server exposes `registration_endpoint`

Recommended per-server OAuth configuration:

```json
{
  "mcpServers": {
    "secure-url-server": {
      "url": "https://mcp.example.com/mcp",
      "headers": {},
      "oauth": {
        "client_id": "https://app.example.com/oauth/client-metadata.json",
        "client_secret": "",
        "redirect_host": "127.0.0.1",
        "redirect_port": 0,
        "scope": "files:read files:write",
        "open_browser": true
      }
    }
  }
}
```

Notes:

- `redirect_port: 0` means Smart Shell will pick a free local callback port automatically
- If `open_browser=false`, the app prints the authorization URL so you can open it manually
- If `scope` is not configured, Smart Shell prefers the 401 challenge scope and otherwise falls back to `scopes_supported`

Example `mcp.jsonc`:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    },
    "figma": {
      "url": "https://mcp.figma.com/mcp",
      "headers": {}
    },
    "custom-stdio": {
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": {
        "MY_API_BASE": "https://example.com"
      }
    }
  }
}
```

## Troubleshooting

### Model Configuration Issues

- Make sure the configuration file is valid JSON
- Check your API keys and base URLs
- For Ollama models, confirm that the model has been downloaded and is available locally

## Demo

![Git command](demo/git_command.png)

## Contributing

Issues and pull requests are welcome.

## License

MIT License
