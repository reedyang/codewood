---
name: baidu
description: "Use for web search and fact-finding via **Baidu** (百度搜索). Trigger when the user asks to 百度一下, 用百度搜, 联网搜索 (and Baidu is acceptable), 查一下网上/网上怎么说, or needs fresh public facts (weather, news, stock, definitions, current events) **without** specifying another engine. Do **not** trigger when the user explicitly wants Google/Bing/DuckDuckGo only, or when the task is purely local files/code with no web need. The bundled script prints **only to stdout** (no report files): sections 【当前本机时间】, 【检索摘要】, 【正文摘录与要点】, 【回答】, 【AI 审核】. Respect **orchestration**: for time-sensitive questions, acknowledge you need local time first (the script prints it), then run **one** `shell` with the script path; do not repeat the same command more than **5** times in a row for this skill. After a successful run whose captured `output` contains both 【回答】 and 【AI 审核】, output `{\"tool\":\"done\",\"args\":{}}` or a final user reply—do **not** run the same `baidu_search.py` line again unless the user asks a new query."
license: Proprietary
---

# Baidu search (built-in)

## Scope

- **Engine**: Baidu `www.baidu.com` SERP only. If the user names another engine, use that engine’s workflow instead; do not force Baidu.
- **Strengths**: First-screen SERP plus optional follow-up page reads and **generic** extractive summarization (not tuned to a single topic). Relevance uses heuristic scoring over query terms and page text.
- **Output**: Everything goes to **stdout** from `scripts/baidu_search.py`. No separate report files.

## CLI

Run via `shell` with the **absolute path** listed under **Detected bundled `scripts/*.py`** in the system prompt (bundle root + `scripts/baidu_search.py`).

```text
python "<BUNDLE_ROOT>/scripts/baidu_search.py" "<query>" [--max-pages N] [--no-cache] [--insecure]
```

- **`--max-pages`**: Integer **1–10**. How many result pages to fetch **after** parsing the SERP (default **3**). The implementation may stop earlier if accumulated text is already enough to answer (**early exit**).
- **`--no-cache`**: Skip the local cache under `<bundle root>/.cache/` (otherwise up to **20** entries, **~30 minutes** TTL).
- **`--insecure`**: Disable TLS certificate verification if a corporate proxy or local CA causes `SSL: CERTIFICATE_VERIFY_FAILED`. Alternatively set env **`SMARTSHELL_BAIDU_INSECURE_SSL=1`** (same effect). Use only when necessary.

## Orchestration (mandatory)

1. **Plan** in natural language: need query string, whether time-sensitivity matters, and a reasonable `--max-pages` (higher for disputed or sparse SERP).
2. **Execute** one `shell` invoking the script with the absolute path. **Do not** chain duplicate identical commands.
3. **Read** the command result: the host attaches **`output`** (full stdout) to the next turn—use it to answer the user.
4. **Stop**: If `output` contains **【回答】** and **【AI 审核】**, treat the retrieval as complete; **do not** re-run the same script for the same query unless the user refines the question.
5. **Retries**: At most **5** consecutive attempts for **this skill** on the same user goal; after that, explain the failure and ask for a different query or engine.

## Time-sensitive tasks

The script always prints **【当前本机时间】** (ISO-like local timestamp). For news, weather, rates, etc., keep that line in mind when judging freshness in later turns.

For queries containing recency constraints (e.g., “最近/近一个月/近期/最新/走势/行情/预测”):
- Prefer adding `--no-cache` to avoid stale SERP snapshots.
- Do **not** output hard numeric conclusions (percentages, market cap, index points) unless those numbers are explicitly present in **【正文摘录与要点】**.
- If output shows insufficient recent evidence in **【时效性检查】**, report uncertainty and ask for narrower, verifiable data-source queries instead of fabricating a forecast.

## Cache

- Directory: `<bundle root for baidu>/.cache/` (e.g. `skills/baidu/.cache/` in the repo).
- Roughly **20** entries, **~30** minutes expiry; implementation may use one JSON file. Safe to delete `.cache` if disk or stale data is a concern.

## Host integration notes

- Terminal **`shell` results now include `output` (stdout) and `stderr`** so the model can see script output without relying on the UI alone.
- The trailing **【AI 审核】** block is for the **host model** to verify claims against the cited snippets; it is not a separate file.

## Limitations

- Baidu may return a captcha page; the script reports that in 【检索摘要】 / 【回答】.
- Page HTML varies; SERP parsing is best-effort. If results are empty, suggest rephrasing the query or another engine if allowed.
- Fetched pages must be `http`/`https`. Pay attention to robots/terms of use in your environment.
