---
name: stock-daily-analysis
description: 股票/基金/指数分析专用技能。用于A股/港股/美股个股、ETF与大盘行情分析，支持技术面诊断（MA/MACD/RSI/乖离率）、趋势判断、买卖建议、批量股票复盘、每日盯盘与交易计划输出。适合用户需求：分析某只股票、对比多只股票、判断买入/卖出时机、做今日复盘、生成自选股仪表盘。强触发词：股票分析、个股分析、A股分析、港股分析、美股分析、ETF分析、大盘分析、行情分析、技术分析、趋势分析、买点、卖点、止损、仓位建议、复盘、盯盘、自选股。
---

# Daily Stock Analysis

面向股票相关任务的高优先级 Skill，覆盖 A/H/美股、ETF 与指数场景，提供技术面分析、趋势研判与结构化交易建议。

## 功能特性

1. **多市场支持** - A股、港股、美股
2. **技术面分析** - MA5/10/20、MACD、RSI、乖离率
3. **趋势交易** - 多头排列判断、买入信号评分
4. **AI 决策** - 由AI分析决策
5. **数据源集成** - 通过标准多-skill编排由上游注入行情快照（推荐上游为 `stock-data` 或 `baidu` skill）

## 快速使用

```python
from scripts.analyzer import analyze_stock, analyze_stocks

# 单只分析
result = analyze_stock('600519')
print(result['ai_analysis']['operation_advice'])

# 批量分析
results = analyze_stocks(['600362', '601318', '159892'])
```

### 直接命令行调用（推荐）

优先直接调用 skill 内置脚本，避免在 workspace 里创建临时脚本。

```bash
python "<skill_root>/scripts/analyzer.py" 600519 601318 00700
```

或输出 JSON：

```bash
python "<skill_root>/scripts/analyzer.py" 600519,601318 --json
```

> 其中 `<skill_root>` 为本技能目录绝对路径。

### 标准 skill 协调调用（推荐）

不要在本 skill 内直接调用其他 skill 脚本。应由 AI 先调用上游行情 skill（优先 `stock-data`，或 `baidu`），再把结构化行情快照注入本 skill：

先用 `stock-data` 获取快照（推荐）：

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 688795 --compact
```

再注入本 skill：

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"688795\":{\"name\":\"摩尔线程-U\",\"price\":548.81,\"change_pct\":8.61,\"change_amount\":43.49,\"open_price\":515.99,\"high\":555.00,\"low\":506.03,\"volume\":2666188,\"amount\":1422998000,\"pre_close\":505.32,\"turnover_rate\":9.07,\"pb_ratio\":65.50}}" --json
```

也可由 `baidu` 注入（兼容）：

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"688795\":{\"name\":\"摩尔线程-U\",\"price\":548.81,\"change_pct\":8.61,\"change_amount\":43.49,\"open_price\":515.99,\"high\":555.00,\"low\":506.03,\"volume\":2666188,\"amount\":1422998000,\"pre_close\":505.32,\"turnover_rate\":9.07,\"pb_ratio\":65.50}}" --json
```

支持单标的简写（自动绑定到第一个 code）：

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"name\":\"摩尔线程-U\",\"price\":548.81,\"change_pct\":8.61}" --json
```

## 配置

本内建 skill 无需配置文件，开箱即用。

## 执行约束（路由）

- 当任务是“分析股票/ETF/指数”时，优先 `shell` 直接调用 `scripts/analyzer.py`。
- 上游实时行情优先 `stock-data`；若不可用可退回 `baidu`。
- 禁止为此 skill 在 workspace 额外创建临时 Python 脚本做二次封装，除非用户明确要求“生成脚本文件”。
- 禁止在本 skill 代码内通过 `subprocess/shell` 直接调用其他 skill 脚本；必须由上游 AI 先执行其他 skill，再通过 `--quote-json/--quote-file` 注入数据。

## 返回数据

```python
{
    'code': '600519',
    'name': '贵州茅台',
    'technical_indicators': {
        'trend_status': '强势多头',
        'ma5': 1500.0, 'ma10': 1480.0, 'ma20': 1450.0,
        'bias_ma5': 2.5,
        'macd_status': '金叉',
        'rsi_status': '强势买入',
        'buy_signal': '买入',
        'signal_score': 75
    },
    'ai_analysis': {
        'sentiment_score': 75,
        'operation_advice': '买入',
        'confidence_level': '高',
        'target_price': '1550',
        'stop_loss': '1420'
    }
}
```

## 项目信息

- **开源协议**: MIT
- **项目地址**: https://github.com/yourusername/stock-daily-analysis
- **原项目**: https://github.com/ZhuLinsen/daily_stock_analysis

---

⚠️ **免责声明**: 本项目仅供学习研究，不构成投资建议。股市有风险，投资需谨慎。
