# MCP 配置

[English](../en/mcp.md) | **简体中文**

Code Wood 会自动读取与 `config.jsonc` 同目录下的 `mcp.jsonc`：

- 若文件存在且格式合法，则加载 `mcpServers` 并注入系统提示词
- MCP 服务器的工具元数据会在后台异步预加载，以保持界面响应
- 若文件缺失或非法，Code Wood 仍会正常运行，只是以空的 MCP 服务器列表启动
- 密钥请放在环境变量中，不要明文提交
- MCP 连接与重试日志写入 `workspace/logs/mcp_manager.log`，不输出到终端

## 可用的 MCP 动作

- `mcp_list_tools`：列出指定服务器的工具
- `mcp_list_resources`：列出指定服务器的资源
- `mcp_read_resource`：读取某个资源 URI
- `mcp__<server>__<tool>`：MCP 工具以 `mcp__server__toolname` 前缀直接注入函数调用工具列表，可像原生工具一样调用
- `mcp_list_prompts`：列出指定服务器的 prompt
- `mcp_get_prompt`：按名称与参数获取 prompt 结果
- 失败状态会被归类为 `unsupported`、`missing_dependency` 或 `connect_failed`

在 GUI 中打开 **设置 → MCP** 可以查看服务器并逐个启用/禁用 MCP 工具。

## URL 型 MCP 服务器的 OAuth 2.0

URL 传输在收到 `401 Unauthorized` 挑战后会自动走 OAuth 流程：

- 解析 `WWW-Authenticate`，包括 `resource_metadata` 与 `scope`
- 发现 Protected Resource Metadata 与 Authorization Server Metadata
- 使用 Authorization Code + PKCE（`S256`）
- 从 `<config_dir>/oauth_tokens.json` 保存与加载 token，并在可能时刷新
- 当未配置 `client_id` 且授权服务器暴露 `registration_endpoint` 时，尝试动态客户端注册

推荐的单服务器 OAuth 配置：

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

说明：

- `redirect_port: 0` 表示由 Code Wood 自动挑选一个空闲的本地回调端口
- 若 `open_browser=false`，应用会打印授权 URL，由你手动打开
- 若未配置 `scope`，Code Wood 优先使用 401 挑战中的 scope，否则回退到 `scopes_supported`

`mcp.jsonc` 示例：

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

`env` 值中的 `$VAR` 与 `${VAR}` 会从启动进程的环境中展开，因此可以像 `PATH=/opt/homebrew/bin:$PATH` 这样前置路径，避免从 Finder/Dock 启动 GUI 时 `PATH` 不完整而找不到 `npx`/`node`。
