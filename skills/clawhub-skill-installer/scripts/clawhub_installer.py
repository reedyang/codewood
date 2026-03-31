#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Search and install skills from ClawHub with conflict guard."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import ssl
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

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


CLAWHUB_LIST_URL = "https://clawhub.ai/skills"
CLAWHUB_SEARCH_API = "https://clawhub.ai/api/search"
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
    return input(prompt)


def _make_session(verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    if verify_ssl:
        ctx = ssl.create_default_context()
        adapter = _SSLContextAdapter(ctx)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def _fetch_text(url: str, timeout_sec: int = 20, verify_ssl: bool = True) -> str:
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = _make_session(verify_ssl=verify_ssl)
    try:
        resp = session.get(url, timeout=timeout_sec, verify=verify_ssl)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "TLS certificate verification failed for ClawHub using OS trust store. "
            "If your network requires interception certs, import the enterprise root CA into the OS store. "
            "Use --no-verify only when you explicitly accept insecure TLS."
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network request failed: {exc}") from exc


def _fetch_bytes(url: str, timeout_sec: int = 30, verify_ssl: bool = True) -> bytes:
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = _make_session(verify_ssl=verify_ssl)
    try:
        resp = session.get(url, timeout=timeout_sec, verify=verify_ssl)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        raise RuntimeError(f"Network request failed: {exc}") from exc


def _slugify(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return out or "clawhub-skill"


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


def _parse_heading_name(skill_md_text: str) -> str:
    m = re.search(r"^\s*#\s+(.+?)\s*$", skill_md_text, flags=re.MULTILINE)
    if not m:
        return ""
    name = m.group(1).strip()
    return re.sub(r"\s+", " ", name)


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


def _is_clawhub_detail_url(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    try:
        p = urlparse(s)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if p.netloc not in ("clawhub.ai", "www.clawhub.ai"):
        return False
    return p.path.startswith("/skills/") and len(p.path.strip("/").split("/")) >= 2


def _cards_from_search_payload(payload: dict, max_results: int) -> List[SkillCard]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []

    out: List[SkillCard] = []
    seen_urls: set[str] = set()
    limit = max(1, min(max_results, 30))
    for item in results:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        detail_url = f"{CLAWHUB_LIST_URL}/{slug}"
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        name = str(item.get("displayName") or slug).strip()
        summary = str(item.get("summary") or "").strip()
        out.append(SkillCard(name=name, detail_url=detail_url, snippet=summary[:220]))
        if len(out) >= limit:
            break
    return out


def _search(query: str, verify_ssl: bool, max_results: int = 8) -> List[SkillCard]:
    q = query.strip()
    if not q:
        return []
    api_url = f"{CLAWHUB_SEARCH_API}?q={quote_plus(q)}"
    raw = _fetch_text(api_url, verify_ssl=verify_ssl)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from ClawHub search API: {exc}") from exc
    return _cards_from_search_payload(payload=payload, max_results=max_results)


def _extract_skill_md(detail_html: str) -> str:
    # Preferred: fenced markdown.
    m = re.search(
        r"##\s*SKILL\.md.*?```(?:markdown)?\s*(---.*?)(?:```)",
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        txt = m.group(1).strip()
        if txt.startswith("---"):
            return txt + "\n"

    # Fallback: plain rendered content between SKILL.md and Files/Comments section.
    m = re.search(
        r"##\s*SKILL\.md\s*(?P<body>.*?)(?:###\s*Files|##\s*Comments|Select a file)",
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    body = m.group("body").strip()
    if body.startswith("---") and "\nname:" in body:
        return body + ("\n" if not body.endswith("\n") else "")
    return ""


def _extract_download_zip_url(detail_html: str) -> str:
    m = re.search(
        r"\[Download zip\]\((https://[^)\s]+/api/v1/download\?slug=[^)]+)\)",
        detail_html,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'href="(https://[^"]+/api/v1/download\?slug=[^"]+)"',
        detail_html,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def _extract_github_link(detail_html: str) -> str:
    m = re.search(r'href="(https://github\.com/[^"]+)"', detail_html, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#[^\s)]+)?)", detail_html)
    return m.group(1).strip() if m else ""


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


def _extract_zip_companion_files(zip_data: bytes) -> Dict[str, bytes]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
    except zipfile.BadZipFile:
        return {}

    out: Dict[str, bytes] = {}
    root_prefix = ""
    top_levels = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        top = name.split("/", 1)[0]
        top_levels.append(top)
    if top_levels and len(set(top_levels)) == 1:
        root_prefix = f"{top_levels[0]}/"

    for info in zf.infolist():
        raw_name = info.filename.replace("\\", "/")
        if not raw_name or raw_name.endswith("/"):
            continue
        rel_name = raw_name[len(root_prefix) :] if root_prefix and raw_name.startswith(root_prefix) else raw_name
        rel = _normalize_rel_path(rel_name)
        if not rel:
            continue
        if rel.lower() == "skill.md":
            continue
        try:
            data = zf.read(info)
        except Exception:
            continue
        out[rel] = data
    return out


def _extract_skill_md_from_zip(zip_data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
    except zipfile.BadZipFile:
        return ""

    root_prefix = ""
    top_levels = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        top = name.split("/", 1)[0]
        top_levels.append(top)
    if top_levels and len(set(top_levels)) == 1:
        root_prefix = f"{top_levels[0]}/"

    candidates: List[str] = []
    for info in zf.infolist():
        raw_name = info.filename.replace("\\", "/")
        if not raw_name or raw_name.endswith("/"):
            continue
        rel_name = raw_name[len(root_prefix) :] if root_prefix and raw_name.startswith(root_prefix) else raw_name
        rel = _normalize_rel_path(rel_name)
        if not rel:
            continue
        if rel.lower() == "skill.md" or rel.lower().endswith("/skill.md"):
            candidates.append(raw_name)

    for name in candidates:
        try:
            data = zf.read(name)
        except Exception:
            continue
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                txt = data.decode(enc)
                break
            except Exception:
                txt = ""
        if not txt:
            continue
        if not txt.endswith("\n"):
            txt += "\n"
        return txt
    return ""


def cmd_search(args: argparse.Namespace) -> int:
    verify_ssl = not (args.insecure or args.no_verify)
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        cards = _search(query=args.query, verify_ssl=verify_ssl, max_results=args.max_results)
    except RuntimeError as exc:
        print(f"Search failed: {exc}")
        return 1
    print("【ClawHub Search Results】")
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
        print('Installation aborted: explicit confirmation required. Re-run with --confirm "YES".')
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
        if _is_clawhub_detail_url(query):
            detail_url = query
        else:
            try:
                cards = _search(query=query, verify_ssl=verify_ssl, max_results=args.max_results)
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
    if parsed.scheme not in ("http", "https") or parsed.netloc not in ("clawhub.ai", "www.clawhub.ai"):
        print("Invalid --detail-url. It must be a ClawHub detail URL under https://clawhub.ai/skills/...")
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
        detail_html = _fetch_text(detail_url, verify_ssl=verify_ssl)
    except RuntimeError as exc:
        print(f"Install aborted: {exc}")
        return 1

    zip_url = _extract_download_zip_url(detail_html)
    zip_data = b""
    skill_md = ""
    if zip_url:
        try:
            zip_data = _fetch_bytes(zip_url, verify_ssl=verify_ssl)
            skill_md = _extract_skill_md_from_zip(zip_data)
        except RuntimeError:
            zip_data = b""

    if not skill_md:
        skill_md = _extract_skill_md(detail_html)
    if not skill_md:
        print("Install aborted: failed to extract source SKILL.md exactly.")
        return 1
    if not _has_frontmatter(skill_md):
        print("Install aborted: source SKILL.md is not standard frontmatter format (requires name and description).")
        return 1

    skill_name = _parse_frontmatter_name(skill_md) or _parse_heading_name(skill_md) or parsed.path.rstrip("/").split("/")[-1]
    skill_id = _slugify(skill_name)

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
                choice = _prompt_inline(
                    "Conflict resolution required. Choose overwrite(o)/rename(r)/cancel(c): "
                ).strip().lower()
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

    companion_count = 0
    if zip_url and zip_data:
        companion_files = _extract_zip_companion_files(zip_data)
        for rel, content in sorted(companion_files.items(), key=lambda x: x[0].lower()):
            dst = (target_dir / rel).resolve()
            try:
                dst.relative_to(target_dir.resolve())
            except ValueError:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
            companion_count += 1
    elif zip_url:
        try:
            zip_data = _fetch_bytes(zip_url, verify_ssl=verify_ssl)
            companion_files = _extract_zip_companion_files(zip_data)
            for rel, content in sorted(companion_files.items(), key=lambda x: x[0].lower()):
                dst = (target_dir / rel).resolve()
                try:
                    dst.relative_to(target_dir.resolve())
                except ValueError:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(content)
                companion_count += 1
        except RuntimeError:
            pass

    github_url = _extract_github_link(detail_html)
    print("【Install Success】")
    print(f"skill_id: {skill_id}")
    print(f"skill_name: {skill_name}")
    print(f"target_dir: {target_dir}")
    print(f"companion_files: {companion_count}")
    if zip_url:
        print(f"download_zip: {zip_url}")
    if github_url:
        print(f"github_url: {github_url}")
    print("next_step: reload skills in host app if needed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClawHub search/install helper.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="Search skills from ClawHub API")
    p_search.add_argument("--query", default="", help="Keyword for filtering")
    p_search.add_argument("--max-results", type=int, default=8, help="Max number of matched skills to return (1-30)")
    p_search.add_argument("--insecure", action="store_true", help="Disable TLS verification for HTTPS")
    p_search.add_argument("--no-verify", action="store_true", help="Alias of --insecure for TLS verification")
    p_search.set_defaults(func=cmd_search)

    p_install = sub.add_parser("install", help="Install one skill from ClawHub (detail URL or query)")
    p_install.add_argument("--detail-url", default="", help="Skill detail URL on clawhub.ai")
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
