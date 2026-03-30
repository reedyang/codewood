#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Depth-controlled website crawler with targeted extractive output.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from html import unescape
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import requests
    import urllib3
except ImportError as exc:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise SystemExit(1) from exc


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_TEXT_CHARS = 12000
MAX_LINKS_PER_PAGE = 120


@dataclass
class PageData:
    url: str
    depth: int
    status_code: int
    title: str
    text: str
    matched_lines: List[str]
    error: str = ""


def _configure_stdio() -> None:
    """Avoid Unicode encoding issues in Windows consoles."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _normalize_url(url: str) -> Optional[str]:
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    cleaned = parsed._replace(fragment="")
    return urlunparse(cleaned)


def _host_from_url(url: str) -> str:
    return (urlparse(url).hostname or "").strip().lower()


def _is_blocked_host(host: str) -> bool:
    """Block local/private addresses to reduce SSRF risk."""
    if not host:
        return True
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    )


def _extract_links(base_url: str, html: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = unescape(match.group(1).strip())
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        candidate = _normalize_url(urljoin(base_url, href))
        if not candidate:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
        if len(out) >= MAX_LINKS_PER_PAGE:
            break
    return out


def _html_to_text(html: str) -> str:
    no_script = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    no_style = re.sub(r"<style[^>]*>.*?</style>", " ", no_script, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", no_style)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TEXT_CHARS]


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:300]


def _goal_terms(goal: str) -> List[str]:
    g = (goal or "").strip()
    if not g:
        return []
    terms: List[str] = []
    for t in re.split(r"[,;/|，；、\s]+", g):
        token = t.strip()
        if len(token) >= 2 and token.lower() not in {x.lower() for x in terms}:
            terms.append(token)
    return terms[:40]


def _matched_lines(text: str, goal: str) -> List[str]:
    lines: List[str] = []
    if not text:
        return lines
    terms = _goal_terms(goal)
    sentences = re.split(r"(?<=[。！？!?\.])\s+", text)
    for sentence in sentences:
        s = sentence.strip()
        if len(s) < 16:
            continue
        if not terms:
            lines.append(s)
        else:
            lower_s = s.lower()
            if any(term.lower() in lower_s for term in terms):
                lines.append(s)
        if len(lines) >= 8:
            break
    return lines


def _allowed_by_patterns(url: str, includes: List[re.Pattern], excludes: List[re.Pattern]) -> bool:
    if excludes and any(p.search(url) for p in excludes):
        return False
    if not includes:
        return True
    return any(p.search(url) for p in includes)


def _fetch(session: requests.Session, url: str, timeout_sec: int, verify_ssl: bool) -> Tuple[int, str]:
    resp = session.get(url, timeout=timeout_sec, allow_redirects=True, verify=verify_ssl)
    status = int(resp.status_code)
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" not in ctype and "application/xhtml+xml" not in ctype:
        return status, ""
    return status, resp.text


def deep_crawl(
    start_url: str,
    goal: str,
    max_depth: int,
    max_pages: int,
    allow_external: bool,
    includes: List[re.Pattern],
    excludes: List[re.Pattern],
    timeout_sec: int,
    verify_ssl: bool,
) -> Tuple[List[PageData], List[str]]:
    warnings: List[str] = []
    pages: List[PageData] = []
    visited: Set[str] = set()
    queue: Deque[Tuple[str, int]] = deque([(start_url, 0)])
    start_host = _host_from_url(start_url)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    while queue and len(visited) < max_pages:
        current_url, depth = queue.popleft()
        if depth > max_depth:
            continue
        if current_url in visited:
            continue

        host = _host_from_url(current_url)
        if _is_blocked_host(host):
            warnings.append(f"Blocked host skipped: {host} ({current_url})")
            visited.add(current_url)
            continue
        if not allow_external and host != start_host:
            continue
        if not _allowed_by_patterns(current_url, includes, excludes):
            continue

        visited.add(current_url)
        try:
            status, html = _fetch(session, current_url, timeout_sec=timeout_sec, verify_ssl=verify_ssl)
            if not html:
                pages.append(
                    PageData(
                        url=current_url,
                        depth=depth,
                        status_code=status,
                        title="",
                        text="",
                        matched_lines=[],
                    )
                )
                continue
            text = _html_to_text(html)
            title = _extract_title(html)
            lines = _matched_lines(text, goal)
            pages.append(
                PageData(
                    url=current_url,
                    depth=depth,
                    status_code=status,
                    title=title,
                    text=text,
                    matched_lines=lines,
                )
            )

            if depth < max_depth:
                for nxt in _extract_links(current_url, html):
                    if nxt in visited:
                        continue
                    if not _allowed_by_patterns(nxt, includes, excludes):
                        continue
                    if not allow_external and _host_from_url(nxt) != start_host:
                        continue
                    queue.append((nxt, depth + 1))
        except requests.RequestException as exc:
            pages.append(
                PageData(
                    url=current_url,
                    depth=depth,
                    status_code=0,
                    title="",
                    text="",
                    matched_lines=[],
                    error=str(exc),
                )
            )
    return pages, warnings


def _build_answer(goal: str, pages: List[PageData]) -> str:
    matched: List[Tuple[int, str, str]] = []
    for p in pages:
        for line in p.matched_lines:
            matched.append((p.depth, p.url, line))
    if not matched:
        if goal:
            return "No strong evidence matched the extraction goal. Increase depth/pages or refine include/exclude patterns."
        return "Crawl completed. No explicit goal was provided, so only generic page summaries are available."

    top = matched[:12]
    snippets = [f"- [{u}] {line}" for _, u, line in top]
    return "Key evidence:\n" + "\n".join(snippets)


def _compile_patterns(items: List[str], flag_name: str) -> List[re.Pattern]:
    out: List[re.Pattern] = []
    for raw in items:
        try:
            out.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            print(f"Invalid regex for {flag_name}: {raw} ({exc})", file=sys.stderr)
            raise SystemExit(2) from exc
    return out


def _print_report(
    start_url: str,
    goal: str,
    max_depth: int,
    max_pages: int,
    allow_external: bool,
    pages: List[PageData],
    warnings: List[str],
) -> None:
    ok_pages = [p for p in pages if p.error == ""]
    err_pages = [p for p in pages if p.error != ""]
    matched_pages = [p for p in ok_pages if p.matched_lines]

    print("【Crawl Summary】")
    print(f"start_url: {start_url}")
    print(f"goal: {goal or '(none)'}")
    print(f"max_depth: {max_depth}")
    print(f"max_pages: {max_pages}")
    print(f"allow_external: {allow_external}")
    print(f"visited_pages: {len(pages)}")
    print(f"matched_pages: {len(matched_pages)}")
    print(f"error_pages: {len(err_pages)}")
    print("")

    print("【Extracted Findings】")
    if not matched_pages:
        print("(no matched findings)")
    else:
        for p in matched_pages[:20]:
            title = p.title or "(untitled)"
            print(f"- depth={p.depth} status={p.status_code} title={title}")
            print(f"  url: {p.url}")
            for line in p.matched_lines:
                print(f"  evidence: {line}")
    print("")

    print("【Visited Pages】")
    for p in pages[:100]:
        line = f"- depth={p.depth} status={p.status_code} url={p.url}"
        if p.error:
            line += f" error={p.error}"
        print(line)
    print("")

    print("【Answer】")
    print(_build_answer(goal, pages))
    print("")

    print("【Audit Notes】")
    if warnings:
        for w in warnings:
            print(f"- {w}")
    else:
        print("- no warnings")
    print("- Validate extracted facts against source URLs before final decisions.")


def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Depth-controlled web crawler with targeted extraction.")
    parser.add_argument("url", help="Seed URL (http/https)")
    parser.add_argument("--goal", default="", help="Information extraction goal")
    parser.add_argument("--max-depth", type=int, default=2, help="Crawl depth (0+)")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch (1-200)")
    parser.add_argument("--allow-external", action="store_true", help="Allow crawling across hosts")
    parser.add_argument("--include-pattern", action="append", default=[], help="Regex URL include filter")
    parser.add_argument("--exclude-pattern", action="append", default=[], help="Regex URL exclude filter")
    parser.add_argument("--timeout-sec", type=int, default=12, help="Request timeout seconds (3-60)")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    args = parser.parse_args()

    start_url = _normalize_url(args.url)
    if not start_url:
        print("Invalid start URL. Use http/https URL.", file=sys.stderr)
        raise SystemExit(2)
    if args.max_depth < 0 or args.max_depth > 8:
        print("--max-depth must be between 0 and 8.", file=sys.stderr)
        raise SystemExit(2)
    if args.max_pages < 1 or args.max_pages > 200:
        print("--max-pages must be between 1 and 200.", file=sys.stderr)
        raise SystemExit(2)
    if args.timeout_sec < 3 or args.timeout_sec > 60:
        print("--timeout-sec must be between 3 and 60.", file=sys.stderr)
        raise SystemExit(2)

    verify_ssl = not args.insecure
    if os.environ.get("SMARTSHELL_DEEPCRAWL_INSECURE_SSL", "").strip().lower() in ("1", "true", "yes"):
        verify_ssl = False
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    includes = _compile_patterns(args.include_pattern, "--include-pattern")
    excludes = _compile_patterns(args.exclude_pattern, "--exclude-pattern")

    pages, warnings = deep_crawl(
        start_url=start_url,
        goal=args.goal,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        allow_external=args.allow_external,
        includes=includes,
        excludes=excludes,
        timeout_sec=args.timeout_sec,
        verify_ssl=verify_ssl,
    )
    _print_report(
        start_url=start_url,
        goal=args.goal,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        allow_external=args.allow_external,
        pages=pages,
        warnings=warnings,
    )


if __name__ == "__main__":
    main()
