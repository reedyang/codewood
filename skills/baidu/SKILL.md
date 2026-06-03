---
name: baidu
description: "Use this skill for web search and fact-finding via **Baidu**. Trigger when the user asks to search with Baidu, wants web search as long as Baidu is acceptable, asks what the web says, or needs fresh public facts such as weather, news, stocks, definitions, or current events without specifying another engine. Do **not** trigger when the user explicitly wants Google, Bing, or DuckDuckGo only, or when the task is purely local files/code with no web need. This skill’s YAML frontmatter declares **`model_context_file_env`**; compatible hosts may point that environment variable to a temp file path so the script can write the full report without flooding the terminal, then merge it into the subprocess result shown to the model. For time-sensitive questions, run the bundled script once per query; do not repeat the same command more than 5 times in a row for this skill. After a successful run whose captured `output` contains both `【Answer】` and `【AI Review】`, treat the Baidu search as complete for the current query. Do not rerun the same `baidu_search.py` command unless the user refines the question."
license: Proprietary
model_context_file_env: BAIDU_SKILL_MERGE_OUTPUT
---

# Baidu search (built-in)

## Scope

- **Engine**: Baidu `www.baidu.com` SERP only. If the user names another engine, use that engine’s workflow instead and do not force Baidu.
- **Strengths**: First-screen SERP plus optional follow-up page reads and **generic** extractive summarization, not tuned to a single topic. Relevance uses heuristic scoring over query terms and page text.
- **Output**: If the host sets **`BAIDU_SKILL_MERGE_OUTPUT`** to a writable file path (declared in this file’s YAML frontmatter as **`model_context_file_env`**), the script writes the **full** report there; the host may merge that file into the subprocess result `output`. If the variable is unset, the standalone CLI prints the report to stdout. Use the merged `output` sections such as `【Search Summary】` and `【Extracts and Key Points】` when present.

## CLI

Run with the **absolute path** listed under **Detected bundled `scripts/*.py`** in the system prompt (bundle root + `scripts/baidu_search.py`).

```text
python "<BUNDLE_ROOT>/scripts/baidu_search.py" "<query>" --cache-dir "<CACHE_DIR>" [--max-pages N] [--insecure]
```

- **`--max-pages`**: Integer **1–10**. How many result pages to fetch **after** parsing the SERP (default **3**). The implementation may stop earlier if accumulated text is already enough to answer (**early exit**).
- **`--cache-dir`**: **Required** cache root directory. The script writes cache under its `baidu` subdirectory (for example `<CACHE_DIR>/baidu/serp_cache.json`).
- **`--insecure`**: Disable TLS certificate verification if a corporate proxy or local CA causes `SSL: CERTIFICATE_VERIFY_FAILED`. Alternatively set env **`BAIDU_SKILL_INSECURE_SSL=1`** (same effect). Use only when necessary.

## Orchestration (mandatory)

1. **Plan** in natural language: identify the query string, whether time sensitivity matters, and a reasonable `--max-pages` value, with higher values for disputed or sparse SERPs.
2. **Execute** one invocation of the script with the absolute path, using the host’s shell or subprocess runner. Do not chain duplicate identical commands.
3. **Read** the command result: the host attaches **`output`** (stdout plus optional merged file content, if the host reads **`model_context_file_env`** from `SKILL.md` frontmatter) to the next turn. Use it to answer the user.
4. **Completion for this skill**: If `output` contains **`【Answer】`** and **`【AI Review】`**, this Baidu search invocation is complete. Do not rerun the same script for the same query unless the user refines it. That only means this single run of `baidu_search.py` is finished: if you already told the user you would do later phases, such as analysis, another script, or another bundle, you must still carry out those phases afterward. Do not treat this retrieval output alone as the full deliverable for a multi-phase goal you announced.
5. **Retries**: At most 5 consecutive attempts for this skill on the same user goal. After that, explain the failure and ask for a different query or engine.

## Time-sensitive tasks

The merged `output` always includes **`【Current Local Time】`** in ISO-like local timestamp form. Use it when judging whether cited material is still plausible today.

### Query signals the script treats as strong recency (see `baidu_search.py`)

The implementation turns on stricter recent-evidence handling when the query matches patterns such as: **recent, latest, today, this month, one month, market, trend, forecast** (regex over the query string).

### Additional fresh market / macro figure intent (model-side)

Even if the script’s regex does not fire, treat the question as time- and source-sensitive and still apply the rules below when the user asks about things like **the last 30 days, recent performance, market moves, trends, market cap, or forecasts**, or names **A-shares, US equities, Hong Kong stocks, the S&P 500, or the Hang Seng Index**. Read **`【Time Sensitivity Check】`** carefully.

### If `【Answer】` states insufficient recent evidence

When the report says that recent evidence is insufficient, meaning not enough sources have acceptable publish dates in the configured recent window:

- **Do not** invent quantitative forecasts, index levels, or precise percentages.
- Say clearly that evidence is insufficient for a firm conclusion.
- Suggest narrower queries or more authoritative sources, such as exchange data, issuer filings, or official statistics. Do not fabricate latest numbers from weak snippets.

### Numeric and high-stakes claims (model-side)

- Do not assert multiple concrete market figures, such as percentages, index-point values, currency ranges, or multi-number spans, unless those values appear explicitly in `【Extracts and Key Points】` or unambiguously in `【Search Summary】` lines tied to a source.
- One-off numbers in titles or snippets still need to be checked against `【Extracts and Key Points】` before you treat them as verified facts.

### User-facing reply style (after retrieval)

The merged `output` may be long, with full sections in the model context. For the end user:

1. Give 1-2 sentences that directly answer the question.
2. Add at most 3 short evidence bullets, including source angle and date if visible.
3. Add a freshness or uncertainty note when `【Time Sensitivity Check】` or source dates warrant it.
4. Do not paste large blocks of raw page text into the user reply; summarize instead.

## Cache

- Directory: pass cache root via `--cache-dir` from host-side orchestration; script stores data in `<cache-dir>/baidu`.
- `--cache-dir` is mandatory for this skill invocation.
- Roughly **20** entries, **~30** minutes expiry; implementation may use one JSON file. Safe to delete `.cache` if disk or stale data is a concern.

## Host integration notes

- **`model_context_file_env`** (optional, in this file’s YAML frontmatter): set to **`BAIDU_SKILL_MERGE_OUTPUT`**. Any host that runs the bundled script in a subprocess may create a temp file, set that environment variable to its path for the child process, and after exit code 0 merge the file contents into the subprocess result shown to the model. The declaration lives in `SKILL.md` with the rest of the skill contract, with no separate sidecar file.
- **`stderr`**: TLS fallback messages only if **`BAIDU_SKILL_VERBOSE=1`**; argparse errors or missing `requests` may still write to stderr.
- The trailing **`【AI Review】`** block is for the model to verify claims against the cited snippets.

## Limitations

- Baidu may return a captcha page; the script reports that in `【Search Summary】` or `【Answer】`.
- Page HTML varies; SERP parsing is best effort. If results are empty, suggest rephrasing the query or using another engine if allowed.
- Fetched pages must be `http`/`https`. Pay attention to robots/terms of use in your environment.
