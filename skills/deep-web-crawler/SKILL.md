---
name: deep-web-crawler
description: Crawl and extract targeted information from a user-provided website URL with controllable depth. Use whenever the user asks to crawl/scrape/spider a web site, do multi-page website data collection, or "深度爬取网页" with explicit depth/page constraints and extraction goals. Prefer this skill for website-wide retrieval tasks over one-shot page fetch.
license: Proprietary
---

# Deep Web Crawler (built-in)

## Scope

- **Goal**: Crawl a website from a seed URL and extract information relevant to a user goal.
- **Depth control**: Supports strict crawl depth via `--max-depth`.
- **Traversal**: Breadth-first crawl for predictable shallow-to-deep exploration.
- **Safety defaults**:
  - `http/https` only
  - blocks localhost/private/link-local/multicast IP targets
  - default crawl scope is same host only

## CLI

Run with `shell` using the absolute script path shown in the system prompt:

```text
python "<BUNDLE_ROOT>/scripts/deep_crawl.py" "<url>" --goal "<what to extract>" [--max-depth N] [--max-pages N] [--allow-external] [--include-pattern REGEX] [--exclude-pattern REGEX] [--timeout-sec N] [--insecure]
```

## Parameters

- `url` (required): Seed URL.
- `--goal` (optional): Extraction objective. Example: `"pricing, api rate limit, authentication flow"`.
- `--max-depth` (optional): Non-negative crawl depth, default `2`.
- `--max-pages` (optional): Maximum fetched pages, default `20`.
- `--allow-external` (optional): Allow cross-host links. Off by default.
- `--include-pattern` (repeatable): Only crawl URLs matching any regex.
- `--exclude-pattern` (repeatable): Skip URLs matching any regex.
- `--timeout-sec` (optional): Request timeout (seconds), default `12`.
- `--insecure` (optional): Disable TLS verification only when SSL trust chain is broken in local/corporate network.

## Orchestration (mandatory)

1. Clarify user intent: seed URL, target information, and acceptable crawl depth/page budget.
2. Choose conservative limits first (for example depth `1-2`, pages `10-30`) and increase only when needed.
3. Execute a single `shell` command running `deep_crawl.py`.
4. Read the command output and answer based on:
   - `【Extracted Findings】` for relevant evidence
   - `【Visited Pages】` for source traceability
5. If evidence is insufficient, rerun with adjusted depth/page/pattern constraints (not blind retries).

## Output contract

The script prints sections to stdout:

- `【Crawl Summary】`
- `【Extracted Findings】`
- `【Visited Pages】`
- `【Answer】`
- `【Audit Notes】`

Use these sections to produce the user-facing response with clear source URLs.

## Notes

- Prefer regex constraints to reduce noise (`--include-pattern "/docs|/pricing"`).
- Keep depth bounded to avoid accidental broad crawling.
- Respect the target site's terms of use and robots policy in your environment.
