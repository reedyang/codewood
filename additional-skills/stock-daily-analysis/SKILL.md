---
name: stock-daily-analysis
description: [Stock analysis must pass through this skill] When the user asks to analyze an individual stock, ETF, index, or broad market performance; discuss technicals, buy or sell points, review performance, monitor a position, or suggest position sizing and stop losses, you must call `scripts/analyzer.py` through this skill. Do not replace this analysis pipeline with web snippets, casual conversation, or quote repetition. This skill is dedicated to stocks, funds, and indexes; it supports A/H/US markets, technical indicators such as MA, MACD, and RSI, trends, and AI-style action advice. Strong triggers include stock analysis, single-stock analysis, market analysis, technical analysis, trade advice, buy/hold/sell guidance, entry and exit points, review and monitoring, watchlists, position sizing, stop loss, target price, and trend diagnosis.
---

# Daily Stock Analysis

High-priority skill for stock-related tasks, covering A/H/US stocks, ETFs, and indexes. It provides technical analysis, trend interpretation, and structured trading guidance.

## Bundle and Script Path (Portable)

This skill is distributed as a **bundle**: `scripts/analyzer.py` and this `SKILL.md` belong to the same bundle. The placeholder **`<skill_root>`** means the **absolute path to this bundle’s root** and must be resolved by the runtime before invocation. Do not guess the directory hierarchy. When wiring this skill to an upstream market-data pipeline, the upstream bundle root is represented as `<stock_data_skill_root>`, and it should also come from a runtime-provided absolute path.

## Mandatory Routing (Read This First)

- Any user goal involving analysis, diagnosis, or trading advice for a stock, fund, or index must produce its conclusion through `scripts/analyzer.py` in this bundle. If the runtime supports loading the full `SKILL.md` on demand, load it first and then call the script as described below, usually as a pipeline with upstream market-data fetching or injected JSON.
- Do not claim that stock analysis is complete using only web snippets, pure speculation, or output that bypasses `analyzer.py`. Snippets may come from search or market APIs, but the analysis conclusion and technical highlights must come from this skill pipeline.
- If the orchestration includes both search or quote retrieval and analysis, retrieval is only a prerequisite step. Do not stop after fetching data; you must still run `analyzer.py` and answer the user according to the output template.

## Capabilities

1. **Multi-market support** - A-shares, Hong Kong stocks, and US stocks
2. **Technical analysis** - MA5/10/20, MACD, RSI, and bias from moving averages
3. **Trend trading** - Bullish alignment detection and buy-signal scoring
4. **AI decision support** - Structured AI-style analysis
5. **Data source integration** - Upstream market snapshots injected through standard multi-skill orchestration, with `stock-data` or `baidu` recommended upstream skills

This skill’s analysis pipeline **depends** on market snapshots, and `data_fetcher` does not fetch from the web on its own. It also does **not** require or encourage the AI to create temporary JSON files in the workspace before feeding the script. Prefer **stdin/stdout pipelines** or **inline JSON on the command line**, as shown below.

## Quick Start

```python
from scripts.analyzer import analyze_stock, analyze_stocks

# Single-stock analysis
result = analyze_stock('600519')
print(result['ai_analysis']['operation_advice'])

# Batch analysis
results = analyze_stocks(['600362', '601318', '159892'])
```

### Direct CLI Usage

Prefer calling the built-in script directly to avoid creating temporary Python scripts in the workspace. **If you only provide stock codes and no market snapshot, the analysis will fail due to missing data**. Inject a snapshot using the orchestration patterns below.

```bash
python "<skill_root>/scripts/analyzer.py" 600519 601318 00700
```

Or output JSON:

```bash
python "<skill_root>/scripts/analyzer.py" 600519,601318 --json
```

> Here, `<skill_root>` is the absolute path to this skill’s directory.

### Standard Skill Orchestration (Recommended: Pipe, Do Not Write Files)

Do not call other skills from within this skill via `subprocess`. The orchestrator should **run the upstream script first and then pipe it into this skill**, or use **single-line inline `--quote-json`** when the payload is small.

**Preferred (A-shares + `stock-data`): one pipeline, no intermediate file**

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 688795 --compact | python "<skill_root>/scripts/analyzer.py" 688795 --quote-stdin --json
```

When using multiple codes, keep the code lists aligned on both sides, for example:

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318 --compact | python "<skill_root>/scripts/analyzer.py" 600519 601318 --quote-stdin --json
```

**Second choice: inline `--quote-json`** (good for short JSON or when piping is inconvenient)

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"688795\":{\"name\":\"Moore Threads-U\",\"price\":548.81,\"change_pct\":8.61,\"change_amount\":43.49,\"open_price\":515.99,\"high\":555.00,\"low\":506.03,\"volume\":2666188,\"amount\":1422998000,\"pre_close\":505.32,\"turnover_rate\":9.07,\"pb_ratio\":65.50}}" --json
```

When structured market data comes from `baidu` or another source, prefer to **embed it in `--quote-json`** or **pass it through stdin**. You can feed JSON as a here-string or as the upstream script’s stdout. Do **not** write a `text_file` into the workspace first and then use `--quote-file`.

Single-symbol shorthand is supported and automatically binds to the first code:

```bash
python "<skill_root>/scripts/analyzer.py" 688795 --quote-json "{\"name\":\"Moore Threads-U\",\"price\":548.81,\"change_pct\":8.61}" --json
```

**Not recommended: `--quote-file`**. Use it only when the runtime cannot use a pipeline and the command line is too short for the JSON payload. Do **not** create temporary JSON files in the workspace just to use `--quote-file`, unless the user explicitly asks for a file.

## Configuration

This built-in skill requires no configuration file and works out of the box.

## Execution Constraints (Routing)

- Analysis tasks must always go through this skill: when the task is to analyze a stock, ETF, or index, you must produce the conclusion through this bundle’s `scripts/analyzer.py`. Do not skip this skill and answer from other channels. Prefer direct command-line or subprocess calls to `analyzer.py`, combined with upstream market-data pipelines or `--quote-json` as described above.
- Prefer the `stock-data` skill’s fetch script for upstream live market data. If it is unavailable, fall back to another agreed-upon market-data source.
- Do not create extra temporary Python scripts in the workspace to wrap this skill unless the user explicitly asks for a script file.
- Do not create intermediate JSON files in the workspace just to inject market data. Avoid writing snapshots with `text_file` and then using `--quote-file`, unless the user explicitly wants a file. Use **upstream script stdout piped into `analyzer.py --quote-stdin`** or a **single-line `--quote-json`** instead.
- Do not nest calls to other bundle scripts inside this skill. The orchestrator should run upstream steps separately and pass the data to `analyzer.py` through **stdin piping** or **`--quote-json`**.
- After `analyzer.py` returns structured output, especially `--json`, you must first produce a natural-language analysis conclusion before ending the user-facing task. Do not return raw JSON alone and stop.
- The final user-visible reply must be a summary in the order: conclusion first, then key evidence, then risk warnings, then actionable advice. Do not paste large raw logs or raw JSON.

## Output Template (Mandatory)

After receiving the `analyzer.py` result, the AI must organize the response using the following template, with one section per symbol when needed:

```markdown
【Conclusion】
<1-2 sentences that directly answer the user: current trend, whether it is relatively strong or weak, and the recommended action (buy, hold, reduce, or wait)>

【Key Signals】
- Trend: <trend_status>; moving averages: <ma_alignment>
- Momentum: MACD=<macd_status>, RSI=<rsi_status>
- Volume: <volume_status or volume_trend>

【Risks and Uncertainty】
- Risks: <risk_warning / risk_factors>
- Confidence: <confidence_level> (if low, explain why)

【Actionable Advice (For Reference Only)】
- Recommendation: <operation_advice>
- If available: target price <target_price>; stop loss <stop_loss>
```

Additional constraints:

- If `confidence_level` is low or signals conflict, you must explicitly advise waiting, using a small position, or waiting for confirmation.
- If a field is missing, such as `target_price` or `stop_loss`, write that there is no valid target price or stop loss instead of inventing one.
- Once the template is complete and the task goal is satisfied, the orchestrator should end the turn according to the runtime’s conventions. Do not assume a specific host API in this skill document.

## Returned Data

```python
{
    'code': '600519',
    'name': 'Kweichow Moutai',
    'technical_indicators': {
        'trend_status': 'strong_bull',
        'ma5': 1500.0, 'ma10': 1480.0, 'ma20': 1450.0,
        'bias_ma5': 2.5,
        'macd_status': 'golden_cross',
        'rsi_status': 'strong_buy',
        'buy_signal': 'buy',
        'signal_score': 75
    },
    'ai_analysis': {
        'sentiment_score': 75,
        'operation_advice': 'buy',
        'confidence_level': 'high',
        'target_price': '1550',
        'stop_loss': '1420'
    }
}
```

## Project Information

- **License**: MIT
- **Project URL**: https://github.com/yourusername/stock-daily-analysis
- **Original project**: https://github.com/ZhuLinsen/daily_stock_analysis

---

⚠️ **Disclaimer**: This project is for study and research only and does not constitute investment advice. Stock markets are risky; invest carefully.
