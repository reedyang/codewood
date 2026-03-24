#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baidu web search helper: SERP first screen, optional page fetches, extractive summary.
Full report to stdout only; uses local cache under <skill_dir>/.cache/
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
    print("【错误】需要 requests 库：pip install requests", file=sys.stderr)
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


def _cache_dir() -> Path:
    d = _skill_root() / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path() -> Path:
    return _cache_dir() / "serp_cache.json"


def _cache_key(query: str, max_pages: int) -> str:
    # Bump version when decoding / output format changes to invalidate stale cache.
    raw = f"v2\n{query.strip().lower()}\n{max_pages}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_cache() -> List[Dict[str, Any]]:
    p = _cache_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_cache(entries: List[Dict[str, Any]]) -> None:
    try:
        _cache_path().write_text(
            json.dumps(entries, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    entries = _load_cache()
    for e in entries:
        if e.get("key") != key:
            continue
        ts = float(e.get("ts", 0))
        if now - ts > CACHE_TTL_SEC:
            return None
        return e.get("payload")
    return None


def _cache_set(key: str, payload: Dict[str, Any]) -> None:
    now = time.time()
    entries = [e for e in _load_cache() if e.get("key") != key]
    entries.append({"key": key, "ts": now, "payload": payload})
    # keep newest 20
    entries.sort(key=lambda x: float(x.get("ts", 0)), reverse=True)
    entries = entries[:CACHE_MAX_ENTRIES]
    _save_cache(entries)


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
            raise RuntimeError(f"请求失败: {e}") from e
        if not _ssl_insecure_fallback_printed:
            print(
                "【警告】HTTPS 证书校验失败（常见于 Python 未关联 CA、或企业代理替换证书），"
                "已自动改为不校验证书重试。可安装 certifi、配置系统/代理 CA，"
                "或设置环境变量 SMARTSHELL_BAIDU_INSECURE_SSL=1 以始终跳过校验。",
                file=sys.stderr,
            )
            _ssl_insecure_fallback_printed = True
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            r = _get(False)
            r.raise_for_status()
            return _response_to_text(r)
        except requests.RequestException as e2:
            raise RuntimeError(f"请求失败: {e2}") from e2
    except requests.RequestException as e:
        raise RuntimeError(f"请求失败: {e}") from e


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
    )
    return any(re.search(p, q) for p in patterns)


def _extract_publish_datetime(text: str) -> Optional[datetime]:
    if not text:
        return None
    patterns = [
        r"(?P<y>20\d{2}|19\d{2})[-/\.](?P<m>0?[1-9]|1[0-2])[-/\.](?P<d>0?[1-9]|[12]\d|3[01])",
        r"(?P<y>20\d{2}|19\d{2})年(?P<m>0?[1-9]|1[0-2])月(?P<d>0?[1-9]|[12]\d|3[01])日",
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
            "根据当前检索结果，未能从正文提取到足够可靠、与问题直接相关的语句。"
            "可尝试缩短查询词、换关键词，或适当增加 --max-pages。"
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
        titles = "；".join(x["title"] for x in serp_items[:4])
        body = f"检索到的相关结果标题包括：{titles}。建议点开来源链接核对细节。"
    return body


def run_search(
    query: str, max_pages: int, use_cache: bool, verify_ssl: bool = True
) -> str:
    lines: List[str] = []

    local_now = datetime.now().astimezone()
    now_naive = datetime.now()
    time_sensitive = _is_time_sensitive_query(query)
    lines.append(f"【当前本机时间】{local_now.isoformat(timespec='seconds')}")
    lines.append("")

    cache_key = _cache_key(query, max_pages)
    from_cache = False
    if use_cache:
        hit = _cache_get(cache_key)
        if hit and isinstance(hit, dict) and hit.get("serp_html"):
            serp_html = hit["serp_html"]
            from_cache = True
        else:
            serp_url = f"https://www.baidu.com/s?wd={quote(query)}"
            _, serp_html = _fetch(serp_url, verify_ssl=verify_ssl)
            _cache_set(
                cache_key,
                {"serp_html": serp_html, "query": query, "max_pages": max_pages},
            )
    else:
        serp_url = f"https://www.baidu.com/s?wd={quote(query)}"
        _, serp_html = _fetch(serp_url, verify_ssl=verify_ssl)

    if from_cache:
        lines.append("（SERP 结果来自本技能目录下 .cache，仍在 TTL 内）")
        lines.append("")

    if "安全验证" in serp_html or "请输入验证码" in serp_html:
        lines.append("【检索摘要】百度搜索返回验证页，无法解析结果。请稍后重试或更换网络环境。")
        lines.append("")
        lines.append("【回答】暂时无法完成检索。")
        lines.append("")
        lines.append(
            "【AI 审核】请宿主模型核对：是否出现验证码或封禁；若有，勿重复同一脚本请求，"
            "应提示用户更换网络或稍后再试。"
        )
        return "\n".join(lines)

    serp_items = _parse_serp(serp_html)
    serp_items = _rank_results(serp_items, query)

    summary_bits: List[str] = []
    for i, it in enumerate(serp_items[:10], 1):
        sn = it.get("snippet") or ""
        summary_bits.append(f"{i}. {it['title']}\n   链接：{it['url']}\n   摘要：{sn or '（无摘要）'}")

    lines.append("【检索摘要】")
    if not serp_items:
        lines.append("（未解析到常规结果条目，页面结构可能已变更或结果为空。）")
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
                date_note = "发布时间：未知"
                unknown_date_pages += 1
                is_recent = False
            else:
                age_days = (now_naive - pub_dt).days
                if age_days <= RECENT_DAYS_FOR_TIME_QUERY:
                    date_note = f"发布时间：{pub_dt.date().isoformat()}（近{RECENT_DAYS_FOR_TIME_QUERY}天内）"
                    recent_pages += 1
                    is_recent = True
                else:
                    date_note = f"发布时间：{pub_dt.date().isoformat()}（较旧，距今约{age_days}天）"
                    stale_pages += 1
                    is_recent = False
            fetched_texts.append(
                f"来源：{it.get('title','')}\n最终URL：{final_u}\n相关性得分（启发式）：{rel:.2f}\n{date_note}\n正文摘录：{text[:2800]}"
            )
            acc += "\n" + text
            if is_recent:
                acc_recent += "\n" + text
            pages_done += 1
            effective_acc = acc_recent if time_sensitive else acc
            if _enough_material(effective_acc, query, pages_done, max_pages):
                break
        except RuntimeError as e:
            fetched_texts.append(f"来源：{it.get('title','')} URL：{url}\n（抓取失败：{e}）")

    lines.append("【正文摘录与要点】")
    if not fetched_texts:
        lines.append("（未抓取正文或未选择到可访问的 http(s) 链接；回答将主要依据检索摘要。）")
    else:
        lines.append("\n\n---\n\n".join(fetched_texts))
    lines.append("")

    lines.append("【时效性检查】")
    if not time_sensitive:
        lines.append("查询未命中强时效关键词，未启用近期待证据门槛。")
    else:
        lines.append(
            f"已启用近期待证据门槛（近{RECENT_DAYS_FOR_TIME_QUERY}天）。"
            f"近期待证据页：{recent_pages}；较旧页：{stale_pages}；发布时间未知页：{unknown_date_pages}。"
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
            "当前检索结果里，近期待证据不足（发布时间满足“最近一段时间”的来源过少），"
            "无法可靠支持“最近一个月/近期走势”的定量判断。"
            "建议改用更具体且可验证的数据源关键词（例如：交易所官方、权威财经终端、"
            "公司公告、财报日期）后再检索。"
        )
    else:
        answer = _build_answer(all_sents, query, serp_items)
    lines.append("【回答】")
    lines.append(answer)
    lines.append("")
    lines.append(
        "【AI 审核】请宿主模型在上下文中核对："
        "【回答】中的事实是否可由上方【检索摘要】与【正文摘录】支持；"
        "对强时效问题请对照【当前本机时间】判断是否需更新；"
        "若已同时出现【回答】与本段【AI 审核】，请收束对话，勿重复执行同一 `baidu_search.py` 命令。"
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
        description="Baidu SERP + optional page fetch + extractive summary (stdout only)."
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
        "--no-cache",
        action="store_true",
        help="Skip read/write of local .cache",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (use only if corporate proxy breaks SSL)",
    )
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_pages > 10:
        print("【错误】--max-pages 必须在 1–10 之间。", file=sys.stderr)
        raise SystemExit(2)

    verify_ssl = not args.insecure
    if os.environ.get("SMARTSHELL_BAIDU_INSECURE_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        verify_ssl = False
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    out = run_search(
        args.query, args.max_pages, use_cache=not args.no_cache, verify_ssl=verify_ssl
    )
    print(out)


if __name__ == "__main__":
    main()
