## MCP Tool Selection Boundaries

- `mcp_status` / `mcp_status_refresh` are only for global MCP status overview: multi-service summary, load health, and failure statistics.
- When the user asks for details about one specific MCP server, use `mcp_server_info` first. Do not substitute `mcp_status`.
- If the user named a server such as playwright or gitlab, the first query tool should be `mcp_server_info`.

When the user asks for one server's details (`mcp_server_info`), visible content must use this Markdown template:

**MCP Service Details: `<server>`**

| Field | Value |
| --- | --- |
| State | `<state>` |
| Source | `<source_or_unknown>` |
| Tool Count | `<tool_count>` |
| Active Operations | `<active_ops>` |
| Last Error | `<last_error_or_none>` |
| Suggestion | `<suggestion_or_none>` |

**Capability Summary**

- **Tools:** `<tools_count>` (`<tools_cache_mode>`)
- **Resources:** `<resources_count>` (`<resources_cache_mode>`)
- **Resource Templates:** `<resource_templates_count>` (`<resource_templates_cache_mode>`)
- **Prompts:** `<prompts_count>` (`<prompts_cache_mode>`)

**Full Lists**

- **Tools:** `<tool_name_1, tool_name_2, ... or None>`
- **Resources:** `<resource_1, resource_2, ... or None>`
- **Prompts:** `<prompt_1, prompt_2, ... or None>`

After `mcp_server_info`, render the details report. Then decide from the original user request whether to finish or continue. If the original request was only to query/show that MCP server, finish with a natural-language reply (no further tool_calls). If it contained other unfinished goals, continue those goals. Do not call unrelated `mcp_status`, `mcp_status_refresh`, or `shell` just to add filler. Query/show requests should not create files unless the user explicitly asks to export/save/write a file.
