---
name: stock-data
description: 使用新浪财经实时行情 API 获取 A 股快照，并输出可直接注入 stock-daily-analysis 的 quote-json。触发词：stock-data、实时行情、A股实时价格、获取股票快照、行情注入、quote-json。适用于先取数再分析的场景。
license: MIT
---

# Stock Data Snapshot

基于新浪财经实时行情接口（`hq.sinajs.cn`）的内建 skill，用于获取 A 股实时行情，并生成 `stock-daily-analysis` 可直接消费的快照 JSON。

## 用途

- 获取单只或多只 A 股实时行情。
- 输出标准字段：`name/price/change_pct/change_amount/open_price/high/low/volume/amount/pre_close`。
- 作为上游 skill，为 `stock-daily-analysis/scripts/analyzer.py --quote-json` 提供输入。

## 依赖

- Python：标准库即可（无需额外依赖包）
- 凭证：默认无需 token（取决于上游数据源可用性）

## 命令行

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318
```

逗号分隔也支持：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519,601318
```

输出紧凑 JSON（推荐给下游 `--quote-json`）：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact
```

网络不稳定时可加重试参数：

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact --retries 5 --retry-delay 1.5
```

## 与 stock-daily-analysis 联动

先获取快照，再注入分析：

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318 --compact
```

将输出作为 `--quote-json` 参数传给：

```bash
python "<stock_skill_root>/scripts/analyzer.py" 600519 601318 --quote-json "<上一步JSON>" --json
```

## 约束

- 本 skill 只负责行情抓取与标准化，不做交易建议。
- 不在本 skill 内调用其他 skill 脚本，由上层 AI 进行编排。
- 若脚本出现网络错误，建议提高 `--retries` 与 `--retry-delay` 后重试。
