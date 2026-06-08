#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baidu web search helper: SERP first screen, optional page fetches, extractive summary.
When the host sets BAIDU_SKILL_MERGE_OUTPUT to a file path (see ``model_context_file_env``
in ``SKILL.md`` frontmatter), the full report is written there. Otherwise prints to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

try:
    import requests
    import urllib3
except ImportError as e:
    print("[Error] requests is required: pip install requests", file=sys.stderr)
    raise SystemExit(1) from e

# One-time stderr notice when falling back from TLS verify failure (corporate proxy / broken CA store)
_ssl_insecure_fallback_printed: bool = False


def _tls_verify_bundle() -> Optional[str]:
    """Prefer certifi CA bundle; fixes many 'unable to get local issuer certificate' cases on Windows."""
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return None

# --- Constants (generic; not domain-specific) ---
# Must match skills/baidu/SKILL.md frontmatter model_context_file_env
MERGE_OUTPUT_ENV_VAR = "BAIDU_SKILL_MERGE_OUTPUT"
# Optional toggles (host-agnostic; not tied to any single agent product).
ENV_VERBOSE = "BAIDU_SKILL_VERBOSE"
ENV_INSECURE_SSL = "BAIDU_SKILL_INSECURE_SSL"
CACHE_DIR_NAME = ".cache"
CACHE_MAX_ENTRIES = 20
CACHE_TTL_SEC = 30 * 60
MAX_BODY_CHARS = 12000
FETCH_TIMEOUT = 14
EARLY_STOP_SCORE = 0.68
EARLY_STOP_CHARS = 420
RECENT_DAYS_FOR_TIME_QUERY = 45
MIN_RECENT_SOURCES_FOR_TIME_QUERY = 2
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _cache_dir(cache_dir: Optional[Path]) -> Optional[Path]:
    if cache_dir is None:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _emit_report(report: str) -> None:
    """Host sets MERGE_OUTPUT_ENV_VAR to a temp file; standalone CLI prints to stdout."""
    path = os.environ.get(MERGE_OUTPUT_ENV_VAR, "").strip()
    if path:
        try:
            Path(path).write_text(report, encoding="utf-8")
        except OSError:
            pass
        return
    print(report)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_verbose() -> bool:
    return _env_truthy(ENV_VERBOSE)


def _cache_path(cache_dir: Optional[Path]) -> Optional[Path]:
    d = _cache_dir(cache_dir)
    if d is None:
        return None
    return d / "serp_cache.json"


def _cache_key(query: str, max_pages: int) -> str:
    # Bump version when decoding / output format changes to invalidate stale cache.
    raw = f"v2\n{query.strip().lower()}\n{max_pages}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_cache(cache_dir: Optional[Path]) -> List[Dict[str, Any]]:
    p = _cache_path(cache_dir)
    if p is None or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_cache(entries: List[Dict[str, Any]], cache_dir: Optional[Path]) -> None:
    p = _cache_path(cache_dir)
    if p is None:
        return
    try:
        p.write_text(
            json.dumps(entries, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cache_get(key: str, cache_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    now = time.time()
    entries = _load_cache(cache_dir)
    for e in entries:
        if e.get("key") != key:
            continue
        ts = float(e.get("ts", 0))
        if now - ts > CACHE_TTL_SEC:
            return None
        return e.get("payload")
    return None


def _cache_set(key: str, payload: Dict[str, Any], cache_dir: Optional[Path]) -> None:
    now = time.time()
    entries = [e for e in _load_cache(cache_dir) if e.get("key") != key]
    entries.append({"key": key, "ts": now, "payload": payload})
    # keep newest 20
    entries.sort(key=lambda x: float(x.get("ts", 0)), reverse=True)
    entries = entries[:CACHE_MAX_ENTRIES]
    _save_cache(entries, cache_dir)


def _http_headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _normalize_html_charset(name: str) -> str:
    """Map HTML-declared names to Python codec names (Chinese legacy pages)."""
    if not name:
        return "utf-8"
    n = name.strip().strip("\"'").lower()
    if n in ("gb2312", "gb_2312", "chinese", "cn-gb"):
        return "gb18030"
    if n in ("windows-936", "cp936"):
        return "gbk"
    return n


def _charset_from_content_type(content_type: str) -> Optional[str]:
    if not content_type:
        return None
    m = re.search(r"charset\s*=\s*([\w.-]+)", content_type, re.I)
    if m:
        return _normalize_html_charset(m.group(1))
    return None


def _charset_from_html_meta(head: bytes) -> Optional[str]:
    """Parse charset from first bytes (meta charset / http-equiv)."""
    # <meta charset="utf-8">
    m = re.search(rb'<meta\s+charset\s*=\s*["\']?([^"\'\s/>]+)', head, re.I)
    if m:
        try:
            return _normalize_html_charset(
                m.group(1).decode("ascii", errors="ignore").strip()
            )
        except Exception:
            pass
    # content="text/html; charset=gbk" (attribute order may vary)
    m = re.search(
        rb'content\s*=\s*["\'][^"\']*charset\s*=\s*([\w.-]+)',
        head,
        re.I,
    )
    if m:
        try:
            return _normalize_html_charset(
                m.group(1).decode("ascii", errors="ignore").strip()
            )
        except Exception:
            pass
    # Loose: charset= after meta or anywhere in head (Baidu / legacy)
    m = re.search(rb'charset\s*=\s*["\']?([^"\'\s;>]+)', head[:16384], re.I)
    if m:
        try:
            return _normalize_html_charset(
                m.group(1).decode("ascii", errors="ignore").strip()
            )
        except Exception:
            pass
    return None


def _decode_html_bytes(raw: bytes, content_type: str = "") -> str:
    """
    Decode HTML body without relying on requests' apparent_encoding (often wrong for GBK pages).
    Prefer declared charset, then UTF-8, then GB18030/GBK for Chinese sites.
    """
    if not raw:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")

    enc_header = _charset_from_content_type(content_type)
    enc_meta = _charset_from_html_meta(raw[:32768])

    try_order: List[str] = []
    for e in (enc_header, enc_meta):
        if e and e not in try_order:
            try_order.append(e)
    for e in ("utf-8", "gb18030", "gbk", "big5"):
        if e not in try_order:
            try_order.append(e)

    for enc in try_order:
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort: GB family replaces mojibake less often than UTF-8 replace on mixed pages
    try:
        return raw.decode("gb18030", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _fetch(url: str, verify_ssl: bool = True) -> Tuple[str, str]:
    global _ssl_insecure_fallback_printed

    def _response_to_text(r: requests.Response) -> Tuple[str, str]:
        ct = r.headers.get("Content-Type", "") or ""
        text = _decode_html_bytes(r.content, ct)
        return r.url, text

    def _get(verify: object) -> requests.Response:
        return requests.get(
            url,
            headers=_http_headers(),
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            verify=verify,
        )

    verify: object = False
    if verify_ssl:
        bundle = _tls_verify_bundle()
        verify = bundle if bundle is not None else True

    try:
        r = _get(verify)
        r.raise_for_status()
        return _response_to_text(r)
    except requests.exceptions.SSLError as e:
        if not verify_ssl:
            raise RuntimeError(f"Request failed: {e}") from e
        if not _ssl_insecure_fallback_printed:
            if _env_verbose():
                print(
                    "[Warning] HTTPS certificate verification failed (often because Python has no CA bundle or an enterprise proxy replaced the certificate). "
                    "Retrying with certificate verification disabled. You can install certifi, configure the system or proxy CA, "
                    "or set BAIDU_SKILL_INSECURE_SSL=1 to always skip verification.",
                    file=sys.stderr,
                )
            _ssl_insecure_fallback_printed = True
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            r = _get(False)
            r.raise_for_status()
            return _response_to_text(r)
        except requests.RequestException as e2:
            raise RuntimeError(f"Request failed: {e2}") from e2
    except requests.RequestException as e:
        raise RuntimeError(f"Request failed: {e}") from e


def _html_to_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.I)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    t = unescape(html)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:MAX_BODY_CHARS]


def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?])\s+|\n+", text)
    out = [p.strip() for p in parts if len(p.strip()) > 8]
    return out


def _query_terms(query: str) -> List[str]:
    q = query.strip()
    if not q:
        return []
    terms: List[str] = [q]
    # English-ish tokens
    for w in re.findall(r"[A-Za-z0-9]{2,}", q):
        if w.lower() not in {t.lower() for t in terms}:
            terms.append(w)
    # short Chinese phrases (2–4 chars) sliding
    for ln in (4, 3, 2):
        if len(q) >= ln:
            for i in range(0, len(q) - ln + 1):
                frag = q[i : i + ln]
                if frag not in terms:
                    terms.append(frag)
    return terms[:48]


def _relevance_score(text: str, query: str) -> float:
    if not text or not query:
        return 0.0
    t = text.lower()
    ql = query.strip().lower()
    if ql and ql in t:
        base = 0.55
    else:
        base = 0.0
    terms = _query_terms(query)
    hits = 0
    for term in terms:
        if len(term) >= 2 and term.lower() in t:
            hits += 1
    term_score = min(0.45, hits * 0.06)
    # length normalization
    return min(1.0, base + term_score)


def _is_time_sensitive_query(query: str) -> bool:
    q = query.strip()
    if not q:
        return False
    # Match both Chinese and English recency keywords because Baidu queries
    # are predominantly in Chinese while this script's report text is English.
    patterns = (
        r"最近",
        r"近期",
        r"最新",
        r"今日",
        r"本月",
        r"一个月",
        r"近一个月",
        r"行情",
        r"走势",
        r"预测",
        r"recent",
        r"latest",
        r"today",
        r"this month",
        r"one month",
        r"last month",
        r"market",
        r"trend",
        r"forecast",
    )
    return any(re.search(p, q) for p in patterns)


def _extract_publish_datetime(text: str) -> Optional[datetime]:
    if not text:
        return None
    patterns = [
        r"(?P<y>20\d{2}|19\d{2})[-/\.](?P<m>0?[1-9]|1[0-2])[-/\.](?P<d>0?[1-9]|[12]\d|3[01])",
        r"(?P<y>20\d{2}|19\d{2})[./-](?P<m>0?[1-9]|1[0-2])[./-](?P<d>0?[1-9]|[12]\d|3[01])",
    ]
    for p in patterns:
        m = re.search(p, text)
        if not m:
            continue
        try:
            y = int(m.group("y"))
            mo = int(m.group("m"))
            d = int(m.group("d"))
            return datetime(y, mo, d)
        except ValueError:
            continue
    return None


def _parse_serp(html: str) -> List[Dict[str, str]]:
    """Extract organic results: title, url, optional snippet."""
    results: List[Dict[str, str]] = []
    seen = set()

    # Primary: h3.t > a
    block_re = re.compile(
        r'<h3[^>]*class="[^"]*\bt\b[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.I,
    )
    for m in block_re.finditer(html):
        href = unescape(m.group(1).strip())
        title = _html_to_text(m.group(2))
        if not href or not title:
            continue
        if not href.startswith(("http://", "https://")):
            continue
        if "baidu.com" in urlparse(href).netloc and "/link?" not in href:
            continue
        key = href[:200]
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title, "url": href, "snippet": ""})

    # Snippets: try c-abstract near results (best-effort)
    abs_re = re.compile(
        r'class="c-abstract[^"]*"[^>]*>(.*?)</span>',
        re.DOTALL | re.I,
    )
    abses = [ _html_to_text(x.group(1)) for x in abs_re.finditer(html) ]

    for i, r in enumerate(results):
        if i < len(abses) and abses[i]:
            r["snippet"] = abses[i][:400]

    # Fallback: any baidu link lines
    if not results:
        link_re = re.compile(
            r'href="(https?://www\.baidu\.com/link\?[^"]+)"[^>]*>\s*([^<]{5,200})</a>',
            re.I,
        )
        for m in link_re.finditer(html):
            href = unescape(m.group(1).strip())
            title = _html_to_text(m.group(2))
            if not href.startswith(("http://", "https://")):
                continue
            key = href[:200]
            if key in seen:
                continue
            seen.add(key)
            results.append({"title": title, "url": href, "snippet": ""})

    return results[:12]


def _safe_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def _rank_results(items: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    scored: List[Tuple[float, Dict[str, str]]] = []
    for it in items:
        blob = f"{it.get('title','')} {it.get('snippet','')}"
        s = _relevance_score(blob, query)
        scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored]


def _enough_material(
    accumulated_text: str, query: str, pages_fetched: int, max_pages: int
) -> bool:
    if pages_fetched < 1 or pages_fetched >= max_pages:
        return False
    sc = _relevance_score(accumulated_text, query)
    if len(accumulated_text) >= EARLY_STOP_CHARS and sc >= EARLY_STOP_SCORE:
        return True
    return False


def _build_answer(sentences: List[str], query: str, serp_items: List[Dict[str, str]]) -> str:
    if not sentences:
        return (
            "The current results did not provide enough reliable sentences directly related to the question. "
            "Try shortening the query, changing keywords, or increasing --max-pages."
        )
    # Order sentences by score, keep top, restore reading order
    indexed = list(enumerate(sentences))
    scored = [(i, _relevance_score(s, query), s) for i, s in indexed]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_k = [x[2] for x in scored[:10]]
    # preserve original order among top set
    top_set = set(top_k)
    ordered = [s for s in sentences if s in top_set]
    body = "\n\n".join(ordered[:8])
    if len(body) < 40 and serp_items:
        titles = "; ".join(x["title"] for x in serp_items[:4])
        body = f"Relevant result titles include: {titles}. Open the source links to verify details."
    return body


def run_search(
    query: str,
    max_pages: int,
    use_cache: bool,
    verify_ssl: bool = True,
    cache_dir: Optional[Path] = None,
) -> str:
    lines: List[str] = []

    local_now = datetime.now().astimezone()
    now_naive = datetime.now()
    time_sensitive = _is_time_sensitive_query(query)
    lines.append(f"【Current Local Time】{local_now.isoformat(timespec='seconds')}")
    lines.append("")

    cache_key = _cache_key(query, max_pages)
    from_cache = False
    if use_cache:
        hit = _cache_get(cache_key, cache_dir)
        if hit and isinstance(hit, dict) and hit.get("serp_html"):
            serp_html = hit["serp_html"]
            from_cache = True
        else:
            serp_url = f"https://www.baidu.com/s?wd={quote(query)}"
            _, serp_html = _fetch(serp_url, verify_ssl=verify_ssl)
            _cache_set(
                cache_key,
                {"serp_html": serp_html, "query": query, "max_pages": max_pages},
                cache_dir,
            )
    else:
        serp_url = f"https://www.baidu.com/s?wd={quote(query)}"
        _, serp_html = _fetch(serp_url, verify_ssl=verify_ssl)

    if from_cache:
        lines.append("(SERP results came from this skill directory's .cache and are still within the TTL)")
        lines.append("")

    # Baidu's anti-bot page text is in Chinese; match the actual on-page markers.
    if "安全验证" in serp_html or "请输入验证码" in serp_html or "验证码" in serp_html:
        lines.append("【Search Summary】Baidu returned a verification page and the results could not be parsed. Please retry later or switch networks.")
        lines.append("")
        lines.append("【Answer】Search could not be completed for now.")
        lines.append("")
        lines.append(
            "【AI Review】Please verify whether a captcha or ban occurred. If so, do not repeat the same script request; "
            "advise the user to switch networks or try again later."
        )
        return "\n".join(lines)

    serp_items = _parse_serp(serp_html)
    serp_items = _rank_results(serp_items, query)

    summary_bits: List[str] = []
    for i, it in enumerate(serp_items[:10], 1):
        sn = it.get("snippet") or ""
        summary_bits.append(f"{i}. {it['title']}\n   Link:{it['url']}\n   Snippet:{sn or '(no snippet)'}")

    lines.append("【Search Summary】")
    if not serp_items:
        lines.append("(No standard result entries were parsed; the page structure may have changed or the result set may be empty.)")
    else:
        lines.append("\n\n".join(summary_bits))
    lines.append("")

    fetched_texts: List[str] = []
    recent_pages = 0
    stale_pages = 0
    unknown_date_pages = 0
    pages_done = 0
    acc = ""
    acc_recent = ""

    for it in serp_items:
        if pages_done >= max_pages:
            break
        url = it.get("url") or ""
        if not _safe_http_url(url):
            continue
        try:
            final_u, body_html = _fetch(url, verify_ssl=verify_ssl)
            text = _html_to_text(body_html)
            if len(text) < 80:
                continue
            rel = _relevance_score(text, query)
            pub_dt = _extract_publish_datetime(text)
            if pub_dt is None:
                date_note = "Publish date: unknown"
                unknown_date_pages += 1
                is_recent = False
            else:
                age_days = (now_naive - pub_dt).days
                if age_days <= RECENT_DAYS_FOR_TIME_QUERY:
                    date_note = f"Publish date: {pub_dt.date().isoformat()} (within the last {RECENT_DAYS_FOR_TIME_QUERY} days)"
                    recent_pages += 1
                    is_recent = True
                else:
                    date_note = f"Publish date: {pub_dt.date().isoformat()} (older, about {age_days} days ago)"
                    stale_pages += 1
                    is_recent = False
            fetched_texts.append(
                f"Source:{it.get('title','')}\nFinal URL:{final_u}\nHeuristic relevance score:{rel:.2f}\n{date_note}\nText excerpt:{text[:2800]}"
            )
            acc += "\n" + text
            if is_recent:
                acc_recent += "\n" + text
            pages_done += 1
            effective_acc = acc_recent if time_sensitive else acc
            if _enough_material(effective_acc, query, pages_done, max_pages):
                break
        except RuntimeError as e:
            fetched_texts.append(f"Source: {it.get('title','')} URL: {url}\n(fetch failed: {e})")

    lines.append("【Extracts and Key Points】")
    if not fetched_texts:
        lines.append("(No body text was fetched or no accessible http(s) link was selected; the answer will rely mainly on the search summary.)")
    else:
        lines.append("\n\n---\n\n".join(fetched_texts))
    lines.append("")

    lines.append("【Time Sensitivity Check】")
    if not time_sensitive:
        lines.append("The query did not match strong recency keywords, so the recent-evidence threshold is disabled.")
    else:
        lines.append(
            f"The recent-evidence threshold is enabled (within the last {RECENT_DAYS_FOR_TIME_QUERY} days). "
            f"Recent pages: {recent_pages}; older pages: {stale_pages}; pages with unknown publish dates: {unknown_date_pages}."
        )
    lines.append("")

    all_sents: List[str] = []
    answer_source = acc
    if time_sensitive:
        answer_source = acc_recent
    for chunk in answer_source.split("\n"):
        all_sents.extend(_split_sentences(chunk))

    if time_sensitive and recent_pages < MIN_RECENT_SOURCES_FOR_TIME_QUERY:
        answer = (
            "The current results do not provide enough recent evidence to support a quantitative judgment about the last month or recent trend. "
            "Try more specific and verifiable source keywords, such as an exchange, an authoritative financial terminal, "
            "company announcements, or filing dates, and search again."
        )
    else:
        answer = _build_answer(all_sents, query, serp_items)
    lines.append("【Answer】")
    lines.append(answer)
    lines.append("")
    lines.append(
        "【AI Review】Please verify in context that"
        "the facts in 【Answer】 are supported by the search summary and extracts above;"
        "for strongly time-sensitive questions, compare against 【Current Local Time】 to decide whether an update is needed;"
        "if both 【Answer】 and this 【AI Review】 block are present, wrap up the conversation and do not rerun the same `baidu_search.py` command."
    )
    return "\n".join(lines)


def _configure_stdio() -> None:
    """Avoid UnicodeEncodeError on Windows consoles when page text contains emoji/symbols."""
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


def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Baidu SERP + optional page fetch + extractive summary. "
        "If BAIDU_SKILL_MERGE_OUTPUT is set, write there; else stdout."
    )
    parser.add_argument("query", help="Search query (quote if spaces)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3,
        metavar="N",
        help="Max content pages to fetch after SERP (1–10). Default: 3",
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Cache root directory path (required). Cache is stored under its baidu subdirectory.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (use only if corporate proxy breaks SSL)",
    )
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_pages > 10:
        print("[Error] --max-pages must be between 1 and 10.", file=sys.stderr)
        raise SystemExit(2)

    verify_ssl = not args.insecure
    if _env_truthy(ENV_INSECURE_SSL):
        verify_ssl = False
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    cache_dir = Path(args.cache_dir).resolve()
    use_cache = True
    out = run_search(
        args.query,
        args.max_pages,
        use_cache=use_cache,
        verify_ssl=verify_ssl,
        cache_dir=cache_dir,
    )
    _emit_report(out)


if __name__ == "__main__":
    main()
