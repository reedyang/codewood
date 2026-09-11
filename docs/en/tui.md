# Terminal UI

**English** | [简体中文](../zh-CN/tui.md)

The terminal UI is the original Code Wood interface. It ships in the same
executable as the GUI: run Code Wood with no arguments for the TUI, or pass
`app` for the desktop
GUI ([Desktop GUI](gui.md)).

```bash
python cli/main.py

# Start with a specific workspace by name or path
python cli/main.py --workspace <workspace name or path>
python cli/main.py -w <workspace name or path>

# Run one task and exit
python cli/main.py exec "your task request"

# Start with a workspace and run one task before exiting
python cli/main.py --workspace <workspace name or path> exec "your task request"

# Choose a model at startup
python cli/main.py --model <model name>
python cli/main.py -m <model name>
```

## Input Rules

- `/command` — built-in commands handled locally, without an AI round trip
- `!command` — run a native shell command or script directly
- anything else — treated as natural language and sent to the AI

## Built-in Commands

| Command | Purpose |
|---|---|
| `/help`, `/exit`, `/quit` | Show help, leave the app |
| `/clear screen`, `/clear context`, `/clear input history` | Clear the screen, the conversation context, or the input history |
| `/compact` | Summarize the conversation to free up context |
| `/workspace` | List, create, switch, rename, update, or delete workspaces |
| `/chat` | New, list, switch, rename, fork, edit, reload, or delete chats |
| `/model` | Select `provider/model`, and `/model reasoning <level>` when the model declares `reasoning_effort` levels |
| `/reasoning` | Set the reasoning-effort level for the current chat |
| `/plan`, `/plan off`, `/plan status` | Enter or leave Plan mode (design-only, no execution) and inspect its state |
| `/agent` | Switch back to Agent mode |
| `/execution-policy` | Show or set `unlimited`, `moderate`, or `confirmation` |
| `/always_confirm-reset` | Reset the always-confirm allowlist |
| `/mcp` | List tools, resources, or prompts; inspect status; reconnect; reload config; enable/disable tools |
| `/memory` | Enable/disable memory, list, search, remember, or delete entries, show stats |
| `/language` | Switch the interface language |

Type `/` and press Tab to see completion menus; commands that take arguments
advertise their shape (for example `/chat switch <index|id|name>`).

## Slash Commands vs GUI

Every built-in command above has an equivalent action in the GUI, so the two
front ends stay in sync. See [AI Features](ai-features.md) for execution
policies and the confirm allowlist in detail.
