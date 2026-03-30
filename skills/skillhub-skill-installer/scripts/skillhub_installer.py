#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Search and install skills from SkillHub with conflict guard."""

from __future__ import annotations

import argparse
import json
import re
import ssl
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

try:
    import requests
    import urllib3
except ImportError as exc:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise SystemExit(1) from exc

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


SKILLHUB_LIST_URL = "https://www.skillhub.club/skills"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class SkillCard:
    name: str
    detail_url: str
    snippet: str


@dataclass
class LoadedSkill:
    source: str
    skill_id: str
    skill_name: str
    path: str


class _SSLContextAdapter(requests.adapters.HTTPAdapter):
    """Requests adapter that injects a custom SSL context."""

    def __init__(self, ssl_context: ssl.SSLContext, *args, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _prompt_inline(prompt: str) -> str:
    # Use Python's native inline prompt path for best TTY compatibility.
    return input(prompt)


def _fetch(url: str, timeout_sec: int = 20, verify_ssl: bool = True) -> str:
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            r = requests.get(url, timeout=timeout_sec, headers={"User-Agent": UA}, verify=False)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            raise RuntimeError(f"Network request failed: {exc}") from exc

    # Use OS trust store (same trust source as browser/system) for TLS validation.
    ctx = ssl.create_default_context()
    session = requests.Session()
    adapter = _SSLContextAdapter(ctx)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        r = session.get(url, timeout=timeout_sec, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.text
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "TLS certificate verification failed for SkillHub using OS trust store. "
            "If your network requires interception certs, import the enterprise root CA into the OS store. "
            "Use --no-verify only when you explicitly accept insecure TLS."
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network request failed: {exc}") from exc


def _text_only(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _slugify(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return out or "skillhub-skill"


def _parse_frontmatter_name(skill_md_text: str) -> str:
    if not skill_md_text.startswith("---"):
        return ""
    parts = skill_md_text.split("---", 2)
    if len(parts) < 3:
        return ""
    raw = parts[1]
    m = re.search(r"^\s*name\s*:\s*(.+?)\s*$", raw, flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def _has_frontmatter(skill_md_text: str) -> bool:
    text = (skill_md_text or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    meta = parts[1]
    has_name = re.search(r"^\s*name\s*:\s*.+$", meta, flags=re.MULTILINE) is not None
    has_desc = re.search(r"^\s*description\s*:\s*.+$", meta, flags=re.MULTILINE) is not None
    return has_name and has_desc


def _parse_heading_name(skill_md_text: str) -> str:
    m = re.search(r"^\s*#\s+(.+?)\s*$", skill_md_text, flags=re.MULTILINE)
    if not m:
        return ""
    name = m.group(1).strip()
    return re.sub(r"\s+", " ", name)


def _scan_skills_root(root: Path, source: str) -> List[LoadedSkill]:
    out: List[LoadedSkill] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            txt = skill_md.read_text(encoding="utf-8")
        except OSError:
            txt = ""
        out.append(
            LoadedSkill(
                source=source,
                skill_id=d.name,
                skill_name=_parse_frontmatter_name(txt) or d.name,
                path=str(d.resolve()),
            )
        )
    return out


def _extract_cards_from_list_html(html: str) -> List[SkillCard]:
    cards: List[SkillCard] = []
    seen_urls: set[str] = set()
    pattern = re.compile(
        r'<h3[^>]*>\s*<a[^>]*href="(?P<href>/skills/[^"]+)"[^>]*>(?P<name>[^<]+)</a>\s*</h3>(?P<body>.*?)</section>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(html):
        name = _text_only(m.group("name"))
        detail_url = urljoin(SKILLHUB_LIST_URL, m.group("href"))
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        snippet = _text_only(m.group("body"))
        cards.append(SkillCard(name=name, detail_url=detail_url, snippet=snippet[:220]))

    # Alternate page layout: <h3>name</h3> and skill link appears later in the section.
    alt_pattern = re.compile(
        r'<h3[^>]*>(?P<name>[^<]+)</h3>(?P<body>.*?)</section>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in alt_pattern.finditer(html):
        body = m.group("body")
        href_m = re.search(r'href="(?P<href>/skills/[^"?#]+)"', body, flags=re.IGNORECASE)
        if not href_m:
            continue
        detail_url = urljoin(SKILLHUB_LIST_URL, href_m.group("href"))
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        name = _text_only(m.group("name"))
        snippet = _text_only(body)
        cards.append(SkillCard(name=name, detail_url=detail_url, snippet=snippet[:220]))

    # Fallback: generic href scan from rendered page.
    if not cards:
        hrefs = re.findall(r'href="(/skills/[^"?#"]+)"', html, flags=re.IGNORECASE)
        seen: set[str] = set()
        for href in hrefs:
            if href in ("/skills", "/skills/kol", "/skills/skills"):
                continue
            if href.startswith("/skills?page=") or href.startswith("/skills?"):
                continue
            slug = href.split("/")[-1].strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            name_guess = slug.replace("_", "-")
            cards.append(
                SkillCard(
                    name=name_guess,
                    detail_url=urljoin(SKILLHUB_LIST_URL, href),
                    snippet=f"slug: {slug}",
                )
            )
    return cards


def _detail_matches_query(detail_url: str, query: str, verify_ssl: bool) -> bool:
    try:
        html = _fetch(detail_url, verify_ssl=verify_ssl)
    except RuntimeError:
        return False
    skill_md = _extract_skill_md(html)
    if not skill_md:
        return False
    return query.lower() in skill_md.lower()


def _search(
    query: str,
    page: int,
    verify_ssl: bool,
    scan_pages: int,
    deep_match: bool = True,
    max_detail_fetch: int = 60,
    max_results: int = 5,
) -> List[SkillCard]:
    del page, scan_pages, deep_match, max_detail_fetch  # keep CLI compatibility
    q = query.strip()
    if not q:
        return []

    # Use the same source as the website search dropdown.
    api_url = f"https://www.skillhub.club/api/search?q={quote_plus(q)}"
    raw = _fetch(api_url, verify_ssl=verify_ssl)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from SkillHub dropdown API: {exc}") from exc

    skills = payload.get("skills", [])
    if not isinstance(skills, list):
        return []

    result_limit = max(1, min(max_results, 30))
    cards: List[SkillCard] = []
    seen_urls: set[str] = set()
    for item in skills:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug", "")).strip()
        if not slug:
            continue
        detail_url = f"{SKILLHUB_LIST_URL}/{slug}"
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        name = str(item.get("name") or slug).strip()
        desc = str(item.get("description") or "").strip()
        stars = item.get("github_stars")
        snippet = desc if desc else f"slug: {slug}"
        if isinstance(stars, int):
            snippet = f"{snippet} (stars: {stars})"
        cards.append(SkillCard(name=name, detail_url=detail_url, snippet=snippet[:220]))
        if len(cards) >= result_limit:
            break
    return cards


def _extract_skill_md(detail_html: str) -> str:
    """Extract SKILL.md text as faithfully as possible. Never synthesize content."""
    # Preferred: fenced block under "SKILL.md"
    m = re.search(r"SKILL\.md.*?```(?:markdown)?\s*(---.*?)(?:```)", detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        txt = m.group(1).strip()
        if txt.startswith("---"):
            return txt + "\n"

    # Fallback: section between "SKILL.md" and "Source:"
    m = re.search(r"SKILL\.md(.*?)(?:Source:|Content curated from)", detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        candidate = m.group(1)
        candidate = candidate.replace("&quot;", '"')
        candidate = candidate.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        candidate = re.sub(r"</?code[^>]*>", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"</?pre[^>]*>", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"<br\s*/?>", "\n", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"</p>", "\n", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"<[^>]+>", "", candidate)
        candidate = candidate.strip()
        if candidate.startswith("---") and "\nname:" in candidate:
            return candidate + ("\n" if not candidate.endswith("\n") else "")
        # Some SkillHub entries provide SKILL body without frontmatter.
        if candidate.startswith("#") and len(candidate) > 80:
            return candidate + ("\n" if not candidate.endswith("\n") else "")
    return ""


def _extract_skill_md_from_prose_html(detail_html: str) -> str:
    """
    Extract SKILL.md tab content rendered as HTML (<div class="... prose-skill ...">).
    Convert common tags to markdown-like text while preserving structure.
    """
    m = re.search(
        r'<div class="p-6 prose-skill overflow-x-auto">(.*?)</div>\s*</div>\s*</div>',
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    block = m.group(1)
    txt = block
    txt = txt.replace("\r\n", "\n")
    txt = re.sub(r"<h1[^>]*>(.*?)</h1>", lambda x: f"# {unescape(_text_only(x.group(1)))}\n\n", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"<h2[^>]*>(.*?)</h2>", lambda x: f"## {unescape(_text_only(x.group(1)))}\n\n", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"<h3[^>]*>(.*?)</h3>", lambda x: f"### {unescape(_text_only(x.group(1)))}\n\n", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"<li[^>]*>(.*?)</li>", lambda x: f"- {unescape(_text_only(x.group(1)))}\n", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", lambda x: f"```\n{unescape(x.group(1)).strip()}\n```\n\n", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"</p>", "\n\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<br\\s*/?>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = unescape(txt)
    txt = txt.replace("\u00a0", " ")
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if not txt:
        return ""
    if not txt.endswith("\n"):
        txt += "\n"
    return txt


def _extract_github_link(detail_html: str) -> str:
    m = re.search(r'href="(https://github\.com/[^"]+)"', detail_html, flags=re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_github_repo_links(detail_html: str) -> List[str]:
    links = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", detail_html)
    out: List[str] = []
    seen: set[str] = set()
    for u in links:
        clean = u.rstrip("/").strip()
        if clean.lower().endswith(".git"):
            clean = clean[:-4]
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _extract_github_repo_links_from_text(text: str) -> List[str]:
    links = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text or "")
    out: List[str] = []
    seen: set[str] = set()
    for u in links:
        clean = u.rstrip("/").strip()
        if clean.lower().endswith(".git"):
            clean = clean[:-4]
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _parse_github_skill_ref(github_url: str) -> Tuple[str, str, str, str]:
    """
    Return: (repo_owner, repo_name, skill_owner, skill_id)
    """
    m = re.match(r"https://github\.com/([^/]+)/([^/#]+)(?:#(.+))?$", github_url.strip(), flags=re.IGNORECASE)
    if not m:
        return "", "", "", ""
    repo_owner, repo_name, anchor = m.group(1), m.group(2), (m.group(3) or "")

    skill_owner = ""
    skill_id = ""
    for p in (r"skills-(.+)$", r"\.opencode~skill~(.+)$"):
        am = re.match(p, anchor, flags=re.IGNORECASE)
        if am:
            skill_id = am.group(1).strip()
            break

    if not skill_id and "~" in anchor:
        parts = [x.strip() for x in anchor.split("~") if x.strip()]
        if len(parts) >= 3 and parts[0].lower() == "skills":
            skill_owner = parts[1]
            skill_id = parts[2]
        elif len(parts) >= 2 and parts[0].lower() == "skills":
            skill_id = parts[1]

    return repo_owner, repo_name, skill_owner, skill_id


def _normalize_rel_path(path_text: str) -> str:
    p = path_text.strip().replace("\\", "/")
    if not p:
        return ""
    if p.startswith("/") or re.match(r"^[a-zA-Z]:", p):
        return ""
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        return ""
    return "/".join(parts)


def _looks_like_companion_filename(rel: str) -> bool:
    base = rel.split("/")[-1]
    if not base:
        return False
    if re.search(r"\s", base):
        return False
    if "." in base:
        return True
    if base.startswith("_"):
        return True
    return False


def _extract_companion_files(detail_html: str) -> List[Tuple[str, str]]:
    """
    Extract companion files from SkillHub detail page, excluding SKILL.md.
    """
    out: List[Tuple[str, str]] = []
    seen: set[str] = set()

    section = detail_html

    # Markdown-like sections rendered in page source.
    md_pat = re.compile(
        r"###\s+([^\n`]+?)\s*```[^\n]*\n(.*?)```",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in md_pat.finditer(section):
        rel = _normalize_rel_path(unescape(_text_only(m.group(1))))
        if not rel or rel.lower() == "skill.md":
            continue
        if not _looks_like_companion_filename(rel):
            continue
        content = unescape(m.group(2)).strip("\n")
        if not content:
            continue
        if rel in seen:
            continue
        seen.add(rel)
        out.append((rel, content + "\n"))

    # HTML layout sections: <h3>file</h3><pre><code>...</code></pre>
    html_pat = re.compile(
        r"<h3[^>]*>\s*([^<]+?)\s*</h3>\s*<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in html_pat.finditer(section):
        rel = _normalize_rel_path(unescape(_text_only(m.group(1))))
        if not rel or rel.lower() == "skill.md":
            continue
        if not _looks_like_companion_filename(rel):
            continue
        if rel in seen:
            continue
        content = unescape(m.group(2)).strip("\n")
        if not content:
            continue
        seen.add(rel)
        out.append((rel, content + "\n"))

    return out


def _fetch_github_companion_files(github_url: str) -> List[Tuple[str, str]]:
    repo_owner, repo_name, skill_owner, skill_id = _parse_github_skill_ref(github_url)
    if not repo_owner or not repo_name:
        return []

    # Fallback for plain repo URLs without skill anchor:
    # collect top-level files from repository root.
    if not skill_id:
        out_root: List[Tuple[str, str]] = []
        seen_root: set[str] = set()
        try:
            raw = _fetch(f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents", timeout_sec=8, verify_ssl=True)
            items = json.loads(raw)
        except Exception:
            items = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "file":
                    continue
                rel = _normalize_rel_path(str(item.get("name", "")))
                if not rel or rel.lower() == "skill.md":
                    continue
                if not _looks_like_companion_filename(rel):
                    continue
                if rel in seen_root:
                    continue
                download_url = str(item.get("download_url") or "").strip()
                if not download_url:
                    continue
                try:
                    txt = _fetch(download_url, timeout_sec=8, verify_ssl=True)
                except RuntimeError:
                    continue
                if "\x00" in txt:
                    continue
                seen_root.add(rel)
                out_root.append((rel, txt if txt.endswith("\n") else txt + "\n"))

        # Fallback: parse GitHub HTML when API is rate-limited.
        if not out_root:
            try:
                html = _fetch(f"https://github.com/{repo_owner}/{repo_name}", timeout_sec=8, verify_ssl=True)
            except RuntimeError:
                html = ""
            for m in re.finditer(
                rf'href="/{re.escape(repo_owner)}/{re.escape(repo_name)}/blob/(?P<branch>[^/]+)/(?P<path>[^"#?]+)"',
                html,
                flags=re.IGNORECASE,
            ):
                branch = m.group("branch").strip()
                rel = _normalize_rel_path(m.group("path"))
                if not rel or "/" in rel:
                    continue  # root files only
                if rel.lower() == "skill.md":
                    continue
                if not _looks_like_companion_filename(rel):
                    continue
                if rel in seen_root:
                    continue
                raw_url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/{branch}/{rel}"
                try:
                    txt = _fetch(raw_url, timeout_sec=8, verify_ssl=True)
                except RuntimeError:
                    continue
                if "\x00" in txt:
                    continue
                seen_root.add(rel)
                out_root.append((rel, txt if txt.endswith("\n") else txt + "\n"))
        return out_root

    dir_candidates = [f"skills/{skill_id}", f"workspace/skills/{skill_id}"]
    if skill_owner:
        dir_candidates.insert(0, f"skills/{skill_owner}/{skill_id}")
        dir_candidates.insert(1, f"workspace/skills/{skill_owner}/{skill_id}")

    out: List[Tuple[str, str]] = []
    seen: set[str] = set()
    try:
        tree_raw = _fetch(
            f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/trees/main?recursive=1",
            timeout_sec=8,
            verify_ssl=True,
        )
        tree_payload = json.loads(tree_raw)
        tree_items = tree_payload.get("tree", [])
    except Exception:
        tree_items = []

    if not isinstance(tree_items, list):
        tree_items = []

    for d in dir_candidates:
        prefix = f"{d}/"
        matched: List[Tuple[str, str]] = []
        for item in tree_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "blob":
                continue
            path = str(item.get("path", ""))
            if not path.startswith(prefix):
                continue
            rel = _normalize_rel_path(path[len(prefix) :])
            if not rel or rel.lower() == "skill.md":
                continue
            if rel in seen:
                continue
            # For nested files, keep reasonably safe text-like files only.
            if not _looks_like_companion_filename(rel):
                continue
            download_url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/main/{path}"
            try:
                txt = _fetch(download_url, timeout_sec=8, verify_ssl=True)
            except RuntimeError:
                continue
            if "\x00" in txt:
                continue
            seen.add(rel)
            matched.append((rel, txt if txt.endswith("\n") else txt + "\n"))
        if matched:
            out.extend(matched)
            break
    return out


def _fetch_github_skill_md(github_url: str) -> str:
    """
    Try fetching original SKILL.md from GitHub raw URL using common path conventions.
    Example github_url: https://github.com/openclaw/openclaw#skills-summarize
    """
    if not github_url:
        return ""
    repo_owner, repo_name, skill_owner, skill_id = _parse_github_skill_ref(github_url)
    if not repo_owner or not repo_name or not skill_id:
        return ""

    candidates = [
        f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/main/skills/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/master/skills/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/main/workspace/skills/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/master/workspace/skills/{skill_id}/SKILL.md",
    ]
    if skill_owner:
        candidates.extend(
            [
                f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/main/skills/{skill_owner}/{skill_id}/SKILL.md",
                f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/master/skills/{skill_owner}/{skill_id}/SKILL.md",
                f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/main/workspace/skills/{skill_owner}/{skill_id}/SKILL.md",
                f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/master/workspace/skills/{skill_owner}/{skill_id}/SKILL.md",
            ]
        )
    for u in candidates:
        try:
            txt = _fetch(u, verify_ssl=True)
        except RuntimeError:
            continue
        if (txt.startswith("---") and "\nname:" in txt) or txt.lstrip().startswith("# "):
            return txt if txt.endswith("\n") else txt + "\n"
    return ""


def _collect_loaded_skills(config_dir: Path, builtin_root: Optional[Path], workspace_root: Optional[Path]) -> List[LoadedSkill]:
    all_skills: List[LoadedSkill] = []
    if builtin_root:
        all_skills.extend(_scan_skills_root(builtin_root, "builtin"))
    if workspace_root:
        all_skills.extend(_scan_skills_root(workspace_root, "workspace"))
    all_skills.extend(_scan_skills_root(config_dir / "skills", "config"))
    return all_skills


def _detect_conflicts(loaded: List[LoadedSkill], new_skill_id: str, new_skill_name: str) -> List[LoadedSkill]:
    out: List[LoadedSkill] = []
    sid = new_skill_id.lower()
    sname = (new_skill_name or "").strip().lower()
    for s in loaded:
        if s.skill_id.lower() == sid:
            out.append(s)
            continue
        if sname and s.skill_name.strip().lower() == sname:
            out.append(s)
    return out


def cmd_search(args: argparse.Namespace) -> int:
    verify_ssl = not (args.insecure or args.no_verify)
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        cards = _search(
            query=args.query,
            page=args.page,
            verify_ssl=verify_ssl,
            scan_pages=args.scan_pages,
            deep_match=not bool(args.no_deep_match),
            max_detail_fetch=args.max_detail_fetch,
            max_results=args.max_results,
        )
    except RuntimeError as exc:
        print(f"Search failed: {exc}")
        return 1
    print("【SkillHub Search Results】")
    print(f"query: {args.query}")
    print(f"count: {len(cards)}")
    print("")
    for i, c in enumerate(cards, 1):
        print(f"{i}. {c.name}")
        print(f"   detail_url: {c.detail_url}")
        if c.snippet:
            print(f"   snippet: {c.snippet}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.confirm.strip().upper() != "YES":
        print("Installation aborted: explicit confirmation required. Re-run with --confirm YES.")
        return 2
    verify_ssl = not (args.insecure or args.no_verify)
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    detail_url = args.detail_url.strip() if args.detail_url else ""
    query = (args.query or "").strip()
    if not detail_url:
        if not query:
            print("Invalid install arguments: provide --detail-url or --query.")
            return 2
        try:
            cards = _search(
                query=query,
                page=1,
                verify_ssl=verify_ssl,
                scan_pages=1,
                deep_match=False,
                max_detail_fetch=0,
                max_results=max(1, min(args.max_results, 30)),
            )
        except RuntimeError as exc:
            print(f"Install aborted: search failed: {exc}")
            return 1
        if not cards:
            print(f"Install aborted: no skill found for query: {query}")
            return 1

        print("Interactive selection required.", flush=True)
        print(f"query: {query}", flush=True)
        print(f"count: {len(cards)}", flush=True)
        for i, c in enumerate(cards, 1):
            print(f"{i}. {c.name}", flush=True)
            print(f"   detail_url: {c.detail_url}", flush=True)
            if c.snippet:
                print(f"   snippet: {c.snippet}", flush=True)
        try:
            picked = _prompt_inline(f"Type index to install (1-{len(cards)}): ").strip()
        except EOFError:
            print("Installation aborted: no interactive index received.")
            return 2
        if not picked.isdigit():
            print("Installation aborted: invalid index input.")
            return 2
        idx = int(picked)
        if idx < 1 or idx > len(cards):
            print("Installation aborted: index out of range.")
            return 2
        detail_url = cards[idx - 1].detail_url

    parsed = urlparse(detail_url)
    if parsed.scheme not in ("http", "https") or parsed.netloc != "www.skillhub.club":
        print("Invalid --detail-url. It must be a SkillHub detail URL under https://www.skillhub.club/skills/...")
        return 2

    config_dir = Path(args.config_dir).expanduser().resolve()
    config_skills_root = config_dir / "skills"
    config_skills_root.mkdir(parents=True, exist_ok=True)

    script_path = Path(__file__).resolve()
    inferred_builtin_root = script_path.parents[2]
    inferred_workspace_root = config_dir / "workspace" / "skills"
    builtin_root = Path(args.builtin_skills_root).resolve() if args.builtin_skills_root else inferred_builtin_root
    workspace_root = Path(args.workspace_skills_root).resolve() if args.workspace_skills_root else inferred_workspace_root

    try:
        detail_html = _fetch(detail_url, verify_ssl=verify_ssl)
    except RuntimeError as exc:
        print(f"Install aborted: {exc}")
        return 1

    github_url = _extract_github_link(detail_html)
    skill_md = _fetch_github_skill_md(github_url) or _extract_skill_md(detail_html) or _extract_skill_md_from_prose_html(detail_html)
    if not skill_md:
        print("Install aborted: failed to extract source SKILL.md exactly.")
        return 1

    skill_name = _parse_frontmatter_name(skill_md) or _parse_heading_name(skill_md)
    if not skill_name:
        # Final fallback: use URL tail segment to avoid losing installability.
        skill_name = parsed.path.rstrip("/").split("/")[-1]
    skill_id = _slugify(skill_name)
    needs_conversion = not _has_frontmatter(skill_md)

    if needs_conversion:
        print("Install aborted: source SKILL.md is not standard frontmatter format (requires name and description).")
        return 1

    print("Interactive confirmation required.", flush=True)
    print(f"detail_url: {detail_url}", flush=True)
    print(f"detected_skill_name: {skill_name}", flush=True)
    try:
        typed = _prompt_inline("Confirm installation Yes(y)/No(n): ").strip().lower()
    except EOFError:
        print("Installation aborted: no interactive confirmation received.")
        return 2
    if typed not in ("y", "n"):
        print("Installation aborted: invalid confirmation input (use y/n).")
        return 2
    if typed != "y":
        print("Installation aborted by user.")
        return 2

    target_dir = config_skills_root / skill_id
    def _resolve_dir_conflict(paths: List[Path]) -> int:
        while True:
            try:
                choice = _prompt_inline("Conflict resolution required. Choose overwrite(o)/rename(r)/cancel(c): ").strip().lower()
            except EOFError:
                print("Install aborted: no conflict resolution input received.")
                return 3
            if choice in ("c", "cancel"):
                print("Install cancelled by user.")
                return 3
            if choice in ("o", "overwrite"):
                for p in paths:
                    if p.exists() and p.is_dir():
                        shutil.rmtree(p, ignore_errors=False)
                return 0
            if choice in ("r", "rename"):
                suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
                for p in paths:
                    if p.exists() and p.is_dir():
                        dst = p.with_name(f"{p.name}-backup-{suffix}")
                        p.rename(dst)
                        print(f"Renamed existing skill dir: {p} -> {dst}")
                return 0
            print("Invalid choice. Please type o/r/c.")

    loaded = _collect_loaded_skills(config_dir=config_dir, builtin_root=builtin_root, workspace_root=workspace_root)
    conflicts = _detect_conflicts(loaded, new_skill_id=skill_id, new_skill_name=skill_name)
    if conflicts:
        print("【Conflict Detected】")
        print(f"candidate skill_id: {skill_id}")
        print(f"candidate skill_name: {skill_name}")
        print("conflicts:")
        for c in conflicts:
            print(f"- source={c.source} skill_id={c.skill_id} skill_name={c.skill_name} path={c.path}")
        print("")
        # Only config-side conflicts can be handled interactively.
        non_config_conflicts = [c for c in conflicts if c.source != "config"]
        if non_config_conflicts:
            print("Install aborted due to non-config conflict (builtin/workspace cannot be auto-resolved).")
            return 3

        rc = _resolve_dir_conflict([Path(c.path) for c in conflicts])
        if rc != 0:
            return rc

    if target_dir.exists():
        print("【Conflict Detected】")
        print(f"candidate skill_id: {skill_id}")
        print(f"candidate skill_name: {skill_name}")
        print("conflicts:")
        print(f"- source=config skill_id={skill_id} skill_name={skill_name} path={target_dir}")
        rc = _resolve_dir_conflict([target_dir])
        if rc != 0:
            return rc
        if target_dir.exists():
            print(f"Install aborted: target directory still exists: {target_dir}")
            return 3
    target_dir.mkdir(parents=True, exist_ok=False)

    (target_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    # Merge companion files from GitHub and SkillHub detail page.
    merged_companion: dict[str, str] = {}
    try:
        primary_companion = _fetch_github_companion_files(github_url)
    except Exception:
        primary_companion = []
    for rel, content in primary_companion:
        merged_companion[rel] = content
    for rel, content in _extract_companion_files(detail_html):
        merged_companion.setdefault(rel, content)
    # Merge additional companion files from extra GitHub repo links on detail page.
    alt_links_1 = _extract_github_repo_links(detail_html)[:3]
    for alt_repo in alt_links_1:
        if github_url and alt_repo.rstrip("/") in github_url.rstrip("/"):
            continue
        try:
            alt_files = _fetch_github_companion_files(alt_repo)
        except Exception:
            alt_files = []
        for rel, content in alt_files:
            merged_companion.setdefault(rel, content)

    # Also discover fallback repos from extracted companion text (e.g. README clone URL).
    blob = "\n".join(merged_companion.values())
    alt_links_2 = _extract_github_repo_links_from_text(blob)[:3]
    for alt_repo in alt_links_2:
        if github_url and alt_repo.rstrip("/") in github_url.rstrip("/"):
            continue
        try:
            alt_files = _fetch_github_companion_files(alt_repo)
        except Exception:
            alt_files = []
        for rel, content in alt_files:
            merged_companion.setdefault(rel, content)

    companion_files = sorted(merged_companion.items(), key=lambda x: x[0].lower())
    for rel, content in companion_files:
        dst = (target_dir / rel).resolve()
        try:
            dst.relative_to(target_dir.resolve())
        except ValueError:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    print("【Install Success】")
    print(f"skill_id: {skill_id}")
    print(f"skill_name: {skill_name}")
    print(f"target_dir: {target_dir}")
    print(f"companion_files: {len(companion_files)}")
    if github_url:
        print(f"github_url: {github_url}")
    print("next_step: reload skills in host app if needed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SkillHub search/install helper.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="Search skills from SkillHub dropdown API")
    p_search.add_argument("--query", default="", help="Keyword for filtering")
    p_search.add_argument("--page", type=int, default=1, help="SkillHub list page number")
    p_search.add_argument("--scan-pages", type=int, default=12, help="How many list pages to scan (1-50)")
    p_search.add_argument("--max-detail-fetch", type=int, default=60, help="Max detail pages to fetch for deep matching (1-200)")
    p_search.add_argument("--max-results", type=int, default=8, help="Max number of matched skills to return (1-30)")
    p_search.add_argument("--no-deep-match", action="store_true", help="Disable detail page deep matching")
    p_search.add_argument("--insecure", action="store_true", help="Disable TLS verification for HTTPS")
    p_search.add_argument("--no-verify", action="store_true", help="Alias of --insecure for TLS verification")
    p_search.set_defaults(func=cmd_search)

    p_install = sub.add_parser("install", help="Install one skill from SkillHub (detail URL or query)")
    p_install.add_argument("--detail-url", default="", help="Skill detail URL on skillhub.club")
    p_install.add_argument("--query", default="", help="Search query for interactive index selection")
    p_install.add_argument("--max-results", type=int, default=8, help="Max candidates for interactive selection (1-30)")
    p_install.add_argument("--config-dir", required=True, help="Host config directory (contains skills/)")
    p_install.add_argument("--confirm", default="", help='Must be exactly "YES" to continue')
    p_install.add_argument("--insecure", action="store_true", help="Disable TLS verification for HTTPS")
    p_install.add_argument("--no-verify", action="store_true", help="Alias of --insecure for TLS verification")
    p_install.add_argument("--builtin-skills-root", default="", help="Optional override builtin skills root")
    p_install.add_argument("--workspace-skills-root", default="", help="Optional override workspace skills root")
    p_install.set_defaults(func=cmd_install)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(args)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
