---
name: codex-usage
description: |
  用于查询 OpenAI Codex 的使用情况，提供的参数与当前 `@ccusage/codex@latest` 版本保持一致。支持日期范围查询、JSON 输出、时区/语言环境设置、离线模式、紧凑与彩色输出等常用选项，并提供 `daily`、`monthly`、`session` 三个子命令以查看不同粒度的统计数据。
compatibility: []
---

# Codex Usage Skill

此 skill 用于查询 OpenAI Codex 的使用情况，提供的参数与当前 `@ccusage/codex@latest` 版本保持一致。

## 支持的调用方式

- **按日期范围查询**
  ```bash
  npx @ccusage/codex@latest --since <YYYY-MM-DD> --until <YYYY-MM-DD>

> **注意**：如果用户查询 **指定日期** 或 **日期范围** 的 Codex 用量，必须在调用参数中显式提供日期信息（如 `--since`、`--until` 等）。
  ```

- **指定时区**
  ```bash
  npx @ccusage/codex@latest -z <TIMEZONE>
  ```

- **指定语言环境**
  ```bash
  npx @ccusage/codex@latest -l <LOCALE>
  ```

- **离线模式**
  ```bash
  npx @ccusage/codex@latest -O   # 开启离线
  npx @ccusage/codex@latest --no-offline   # 关闭离线
  ```

- **紧凑输出**
  ```bash
  npx @ccusage/codex@latest --compact
  ```

- **彩色输出**
  ```bash
  npx @ccusage/codex@latest --color
  npx @ccusage/codex@latest --noColor
  ```

- **帮助信息**
  ```bash
  npx @ccusage/codex@latest -h
  ```

- **版本信息**
  ```bash
  npx @ccusage/codex@latest -v
  ```

- **子命令**
  - `daily`   查看最近几日的使用统计
  - `monthly` 查看本月使用统计
  - `session` 查看当前会话使用情况

> 所有参数均可组合使用，例如
> `npx @ccusage/codex@latest --since 2023-07-01 --until 2023-07-31 -j --compact`。