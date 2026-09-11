# 项目结构

[English](../en/project-structure.md) | **简体中文**

```text
codewood/
├── cli/                           # 核心应用代码
├── cli/server/                    # GUI 使用的 headless serve 模式（HTTP + SSE）
├── cli/tests/                     # 测试套件
├── desktop/                       # 桌面 GUI（TypeScript 界面 + pywebview 宿主）
│   ├── frontend/                  # Vite + React + TypeScript 界面
│   └── host/                      # pywebview 宿主（通过 "codewood app" 启动）；
│                                  #   launcher.py 用于构建轻量的 codewood-gui
├── skills/                        # 内置 Agent Skills
├── additional-skills/             # 附加技能；按需复制到 .codewood/skills
├── additional-subagents/          # 示例子代理；按需复制到 .codewood/subagents
├── docs/                          # 设计与参考文档
├── demo/                          # 演示资源
├── requirements.txt               # Python 依赖
├── bin/
|   ├── codewood.bat               # Windows 启动脚本
|   └── codewood.sh                # Bash 启动脚本
└── README.md                      # 项目文档
```
