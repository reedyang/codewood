---
name: stock-data
description: Use Sina Finance real-time market data API to fetch A-share snapshots and output JSON that can be injected directly into `stock-daily-analysis` (`stdout`; compatible with `analyzer --quote-stdin` piping). Triggers: stock-data, market snapshot, real-time quotes, live A-share prices, quote fetch, quote injection, `quote-json`, upstream data for analysis, and pre-analysis market data retrieval. Use this skill when you want to fetch data first and analyze later, especially as the upstream data provider for `stock-daily-analysis`.
license: MIT
---

# Stock Data Snapshot

This built-in skill is based on the Sina Finance real-time quote API (`hq.sinajs.cn`) and fetches A-share live quotes, producing snapshot JSON that `stock-daily-analysis` can consume directly.

## Bundle and Script Path (Portable)

This skill is distributed as a **bundle**: executable scripts live in the sibling `scripts/` directory next to this `SKILL.md`. The placeholder **`<skill_root>`** means the **absolute path to the bundle root** and must be resolved by the runtime before invocation. A common approach is to list the detected absolute paths for `scripts/*.py` in the prompt. Do **not** guess the parent directory name. Commands look like this:

`python "<skill_root>/scripts/fetch_realtime_snapshot.py" ...`

## Purpose

- Fetch live quotes for one or more A-shares.
- Output standard fields: `name/price/change_pct/change_amount/open_price/high/low/volume/amount/pre_close`.
- Serve as an upstream skill that provides input to `stock-daily-analysis/scripts/analyzer.py` (recommended pipeline: `fetch_realtime_snapshot.py ... --compact | analyzer.py ... --quote-stdin`).

## Dependencies

- Python: standard library only, no extra packages required
- Credentials: no token required by default, depending on upstream data source availability

## CLI

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318
```

Comma-separated input is also supported:

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519,601318
```

Compact JSON output is recommended for downstream **`analyzer.py --quote-stdin`** pipelines or `--quote-json`:

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact
```

If the network is unstable, add retry parameters:

```bash
python "<skill_root>/scripts/fetch_realtime_snapshot.py" 600519 --compact --retries 5 --retry-delay 1.5
```

## Integration with `stock-daily-analysis`

**Recommended: a single stdin/stdout pipeline** that avoids writing intermediate files into the workspace:

```bash
python "<stock_data_skill_root>/scripts/fetch_realtime_snapshot.py" 600519 601318 --compact | python "<stock_skill_root>/scripts/analyzer.py" 600519 601318 --quote-stdin --json
```

If piping is not possible, pass the previous step’s stdout as `analyzer.py`'s **`--quote-json` argument** when the JSON is short. **Avoid** writing a temporary file and then using `--quote-file`, unless the environment requires a path.

## Symbols and Exchanges (Common Pitfalls)

- If you provide only a 6-digit number, the script infers `sh` or `sz` from the first digit, for example `600519` -> `sh600519`, `000001` -> `sz000001`.
- The Shanghai Composite Index and the Shenzhen-listed `000001` stock use different Sina symbols. The index is `sh000001`. If you need the Shanghai Composite Index, explicitly pass `sh000001` (or `SH000001`) instead of only `000001`.
- When `sh` or `sz` is explicitly provided, it is not stripped. The top-level JSON key matches the input symbol, such as `sh000001`, which makes it easier to align with the code argument passed to downstream `analyzer.py`.

## Constraints

- This skill only fetches and normalizes market data. It does not provide trading advice.
- Do not call other skill scripts from within this skill. Orchestration must happen in the upper layer.
- If the user gives only a company or asset name in natural language and does not include the exchange symbol required by this script, the orchestrator should first obtain a reliable symbol, for example from confirmed session context or runtime memory lookup, and only then call this script. Do not guess the symbol.
- If the script encounters network errors, retry with higher `--retries` and `--retry-delay` values.
