# MCP Configuration

**English** | [简体中文](../zh-CN/mcp.md)

Code Wood automatically reads `mcp.jsonc` from the same directory as `config.jsonc`:

- If the file exists and is valid, `mcpServers` is loaded and injected into the system prompt
- MCP server tool metadata is preloaded asynchronously in the background so the UI stays responsive
- If the file is missing or invalid, Code Wood keeps running and simply starts with an empty MCP server list
- Store secrets in environment variables instead of committing them in plain text
- MCP connection and retry logs are written to `workspace/logs/mcp_manager.log` rather than the terminal

## Available MCP Actions

- `mcp_list_tools`: list tools for a specific server
- `mcp_list_resources`: list resources for a specific server
- `mcp_read_resource`: read a resource URI
- `mcp__<server>__<tool>`: MCP tools are injected directly into the function-calling tool list with a `mcp__server__toolname` prefix; call them like any native tool
- `mcp_list_prompts`: list prompts for a specific server
- `mcp_get_prompt`: fetch a prompt result by name and parameters
- Failure states are classified as `unsupported`, `missing_dependency`, or `connect_failed`

In the GUI, open **Settings → MCP** to inspect servers and enable or disable individual MCP tools.

## OAuth 2.0 for URL-Based MCP Servers

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

- `redirect_port: 0` means Code Wood will pick a free local callback port automatically
- If `open_browser=false`, the app prints the authorization URL so you can open it manually
- If `scope` is not configured, Code Wood prefers the 401 challenge scope and otherwise falls back to `scopes_supported`

Example `mcp.jsonc`:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
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

`$VAR` and `${VAR}` inside `env` values are expanded from the environment of the launching process, so you can prepend paths like `PATH=/opt/homebrew/bin:$PATH` to avoid GUI launches from Finder/Dock missing a complete `PATH` and failing to find `npx`/`node`.
