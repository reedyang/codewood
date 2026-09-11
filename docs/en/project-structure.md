# Project Structure

**English** | [简体中文](../zh-CN/project-structure.md)

```text
codewood/
├── cli/                           # Core application code
├── cli/server/                    # Headless serve mode (HTTP + SSE) for the GUI
├── cli/tests/                     # Test suite
├── desktop/                       # Desktop GUI (TypeScript UI + pywebview host)
│   ├── frontend/                  # Vite + React + TypeScript UI
│   └── host/                      # pywebview host (launched via "codewood app");
│                                  #   launcher.py builds the thin codewood-gui
├── skills/                        # Built-in Agent Skills
├── additional-skills/             # Extra skills; copy them into .codewood/skills if needed
├── additional-subagents/          # Example sub-agents; copy them into .codewood/subagents if needed
├── docs/                          # Design and reference documentation
├── demo/                          # Demo assets
├── requirements.txt               # Python dependencies
├── bin/
|   ├── codewood.bat               # Windows launch script
|   └── codewood.sh                # Bash launch script
└── README.md                      # Project documentation
```
