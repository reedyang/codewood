---
name: baidu
description: "Use for web search and fact-finding via **Baidu** (百度搜索). Trigger when the user asks to 百度一下, 用百度搜, 联网搜索 (and Baidu is acceptable), 查一下网上/网上怎么说, or needs fresh public facts (weather, news, stock, definitions, current events) **without** specifying another engine. Do **not** trigger when the user explicitly wants Google/Bing/DuckDuckGo only, or when the task is purely local files/code with no web need. This skill’s YAML frontmatter declares **`model_context_file_env`**; conforming hosts may set that env var to a temp file path so the script can write the full report without flooding the terminal, then merge it into the subprocess result shown to the model. For time-sensitive questions, run the bundled script **once** per query; do not repeat the same command more than **5** times in a row for this skill. After a successful run whose captured `output` contains both 【回答】 and 【AI 审核】, treat this Baidu search as complete for the current query—do **not** re-run the same `baidu_search.py` line unless the user refines the question."
license: Proprietary
model_context_file_env: BAIDU_SKILL_MERGE_OUTPUT
---

# Baidu search (built-in)

## Scope

- **Engine**: Baidu `www.baidu.com` SERP only. If the user names another engine, use that engine’s workflow instead; do not force Baidu.
- **Strengths**: First-screen SERP plus optional follow-up page reads and **generic** extractive summarization (not tuned to a single topic). Relevance uses heuristic scoring over query terms and page text.
- **Output**: If the host sets **`BAIDU_SKILL_MERGE_OUTPUT`** to a writable file path (declared in this file’s YAML frontmatter as **`model_context_file_env`**), the script writes the **full** report there; the host may merge that file into the subprocess result `output`. If the variable is unset, **standalone CLI** prints the report to stdout. Use 【检索摘要】, 【正文摘录与要点】, etc., from the merged `output` when present.

## CLI

Run with the **absolute path** listed under **Detected bundled `scripts/*.py`** in the system prompt (bundle root + `scripts/baidu_search.py`).

```text
python "<BUNDLE_ROOT>/scripts/baidu_search.py" "<query>" [--max-pages N] [--no-cache] [--insecure]
```

- **`--max-pages`**: Integer **1–10**. How many result pages to fetch **after** parsing the SERP (default **3**). The implementation may stop earlier if accumulated text is already enough to answer (**early exit**).
- **`--no-cache`**: Skip the local cache under host-injected workspace cache dir (default `workspace/skill_cache/<skill_id>/`; fallback to `<bundle root>/.cache/`) (otherwise up to **20** entries, **~30 minutes** TTL).
- **`--insecure`**: Disable TLS certificate verification if a corporate proxy or local CA causes `SSL: CERTIFICATE_VERIFY_FAILED`. Alternatively set env **`BAIDU_SKILL_INSECURE_SSL=1`** (same effect). Use only when necessary.

## Orchestration (mandatory)

1. **Plan** in natural language: need query string, whether time-sensitivity matters, and a reasonable `--max-pages` (higher for disputed or sparse SERP).
2. **Execute** one invocation of the script with the absolute path (via the host’s shell or subprocess runner). **Do not** chain duplicate identical commands.
3. **Read** the command result: the host attaches **`output`** (stdout plus optional merged file content, if the host reads **`model_context_file_env`** from `SKILL.md` frontmatter) to the next turn—use it to answer the user.
4. **Completion for this skill**: If `output` contains **【回答】** and **【AI 审核】**, this **Baidu search invocation** is complete—**do not** re-run the same script for the same query unless the user refines it. That only means this **single run** of `baidu_search.py` is finished: if you already told the user you would do **later phases** (analysis, another script, another bundle, etc.—per the host’s multi-step rules), you must **still** carry out those phases afterward; do **not** treat this retrieval output alone as the full deliverable for a multi-phase goal you announced.
5. **Retries**: At most **5** consecutive attempts for **this skill** on the same user goal; after that, explain the failure and ask for a different query or engine.

## Time-sensitive tasks

The merged `output` always includes **【当前本机时间】** (ISO-like local timestamp). Use it when judging whether cited material is still plausible “today”.

### When to prefer `--no-cache`

If the user cares about **fresh** public facts (news, weather, rates, policy), add **`--no-cache`** so SERP is not served from the skill’s short-TTL local cache.

### Query signals the script treats as “strong recency” (see `baidu_search.py`)

The implementation turns on **stricter recent-evidence handling** when the query matches patterns such as: **最近、近期、最新、今日、本月、一个月、近一个月、行情、走势、预测** (regex over the query string).

### Additional “fresh market / macro figure” intent (model-side)

Even if the script’s regex does not fire, treat the question as **time- and source-sensitive** and still apply the rules below when the user asks about things like: **近30 天、近期、行情、走势、市值、预测**，or names **A股 / 美股 / 港股 / 标普 / 恒生** (and similar). Prefer **`--no-cache`** and read **【时效性检查】** carefully.

### If **【回答】** states insufficient recent evidence

When the report contains wording equivalent to **「近期待证据不足」** (not enough sources with acceptable publish dates in the configured recent window):

- **Do not** invent quantitative forecasts, index levels, or precise percentages.
- Say clearly that evidence is insufficient for a firm conclusion.
- Suggest **narrower** queries or **more authoritative** sources (e.g. exchange, issuer filings, official statistics)—do not fabricate “latest” numbers from weak snippets.

### Numeric and high-stakes claims (model-side)

- Do **not** assert multiple **concrete market figures** (e.g. `%`, index **点**, **亿元/万美元/港元** ranges, multi-number spans) unless those values appear **explicitly** in **【正文摘录与要点】** or unambiguously in **【检索摘要】** lines tied to a source.
- One-off numbers in titles/snippets still need to be checked against **【正文摘录与要点】** before you treat them as verified facts.

### User-facing reply style (after retrieval)

The merged **`output`** may be long (full sections in the model context). For the **end user**, still:

1. Give **1–2 sentences** that directly answer the question.
2. Add **at most 3** short evidence bullets (source angle + date if visible).
3. Add a **freshness / uncertainty** note when **【时效性检查】** or source dates warrant it.
4. **Do not** paste large blocks of raw page text into the user reply; **summarize** instead.

## Cache

- Directory: Prefer host-injected `SMART_SHELL_SKILL_CACHE_DIR` (typically `<workspace>/skill_cache/baidu/`); fallback to `<bundle root for baidu>/.cache/`.
- Roughly **20** entries, **~30** minutes expiry; implementation may use one JSON file. Safe to delete `.cache` if disk or stale data is a concern.

## Host integration notes

- **`model_context_file_env`** (optional, in this file’s YAML frontmatter): set to **`BAIDU_SKILL_MERGE_OUTPUT`**. Any host that runs the bundled script in a subprocess may create a temp file, set that environment variable to its path for the child process, and after exit code **0** merge the file contents into the subprocess result shown to the model. The declaration lives in **`SKILL.md`** with the rest of the skill contract—no separate sidecar file.
- **`stderr`**: TLS fallback messages only if **`BAIDU_SKILL_VERBOSE=1`**; argparse errors or missing `requests` may still write to stderr.
- The trailing **【AI 审核】** block is for the **model** to verify claims against the cited snippets.

## Limitations

- Baidu may return a captcha page; the script reports that in 【检索摘要】 / 【回答】.
- Page HTML varies; SERP parsing is best-effort. If results are empty, suggest rephrasing the query or another engine if allowed.
- Fetched pages must be `http`/`https`. Pay attention to robots/terms of use in your environment.
