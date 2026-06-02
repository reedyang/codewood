"""
用户偏好持久化：单文件 Markdown + YAML frontmatter，固定注入 system（与经验记忆库分工）。
路径：<config_dir>/user_preferences.md
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_FILENAME = "user_preferences.md"
MAX_BODY_CHARS = 12000
SCHEMA_VERSION = 1

# 简单拒绝明显密钥行写入（防误录）
_SECRET_LINE_PAT = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S{8,}",
    re.I,
)


def _preferences_path(config_dir: Path) -> Path:
    return Path(config_dir) / DEFAULT_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_meta() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": _utc_now_iso()}


def _read_file(path: Path) -> Tuple[Dict[str, Any], str]:
    if not path.is_file():
        return _default_meta(), ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = raw.strip()
    if not raw:
        return _default_meta(), ""
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end >= 0:
            fm_text = raw[3:end].strip()
            body = raw[end + 4 :].lstrip("\n")
            try:
                meta = yaml.safe_load(fm_text) or {}
                if not isinstance(meta, dict):
                    meta = _default_meta()
            except Exception:
                meta = _default_meta()
            return meta, body
    return _default_meta(), raw


def _write_file(path: Path, meta: Dict[str, Any], body: str) -> None:
    meta = dict(meta or {})
    meta["schema_version"] = SCHEMA_VERSION
    meta["updated_at"] = _utc_now_iso()
    fm = yaml.safe_dump(
        meta,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    text = f"---\n{fm}\n---\n\n{(body or '').strip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _reject_secrets(text: str) -> Optional[str]:
    for line in (text or "").splitlines():
        if _SECRET_LINE_PAT.search(line):
            return "Content appears to contain a secret/token line, so it was rejected. Please summarize it instead of pasting the full secret."
    return None


def parse_sections(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    """拆成 (前言, [(小节标题不含 #, 小节正文)])。"""
    lines = (body or "").splitlines()
    preamble_lines: List[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("##"):
        preamble_lines.append(lines[i])
        i += 1
    preamble = "\n".join(preamble_lines).strip()
    sections: List[Tuple[str, str]] = []
    while i < len(lines):
        line = lines[i]
        if not line.startswith("##"):
            i += 1
            continue
        title = line.lstrip("#").strip()
        i += 1
        buf: List[str] = []
        while i < len(lines) and not lines[i].startswith("##"):
            buf.append(lines[i])
            i += 1
        sections.append((title, "\n".join(buf).rstrip()))
    return preamble, sections


def merge_sections(
    preamble: str,
    sections: List[Tuple[str, str]],
    heading: str,
    new_content: str,
) -> str:
    """插入或替换同名小节（标题按 strip 后匹配，忽略大小写）。"""
    h = (heading or "").strip()
    if h.startswith("#"):
        h = re.sub(r"^#+\s*", "", h).strip()
    key = h.lower()
    new_list: List[Tuple[str, str]] = []
    replaced = False
    for title, content in sections:
        if title.strip().lower() == key:
            new_list.append((title, new_content.strip()))
            replaced = True
        else:
            new_list.append((title, content))
    if not replaced:
        new_list.append((h, new_content.strip()))
    parts: List[str] = []
    if preamble:
        parts.append(preamble.strip())
        parts.append("")
    for title, content in new_list:
        parts.append(f"## {title}")
        if content:
            parts.append(content)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_system_append(config_dir: Path, max_chars: int = MAX_BODY_CHARS) -> str:
    """For system injection: return text with a heading prefix; empty files return an empty string."""
    path = _preferences_path(config_dir)
    _, body = _read_file(path)
    body = (body or "").strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "..."
    note = (
        "\n\n(The above section contains persistent user preferences. If it conflicts with scattered experiential memory entries, "
        "this section takes precedence for forms of address, interaction habits, and long-term agreements. "
        "Factual details still depend on mutual confirmation and newer records.)"
    )
    block = (
        "\n\n## [User Preferences (Persistent, Must Follow)]\n\n"
        + body
        + note
    )
    return block

def read_body(config_dir: Path) -> Tuple[Dict[str, Any], str]:
    return _read_file(_preferences_path(Path(config_dir)))


def replace_body(config_dir: Path, new_body: str) -> Dict[str, Any]:
    err = _reject_secrets(new_body)
    if err:
        return {"success": False, "error": err}
    body = (new_body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        return {
            "success": False,
            "error": f"Body exceeds the limit of {MAX_BODY_CHARS} characters. Merge or shorten the content before writing.",
        }
    path = _preferences_path(Path(config_dir))
    meta, _ = _read_file(path)
    _write_file(path, meta, body)
    return {"success": True, "path": str(path), "bytes": len(body.encode("utf-8"))}


def upsert_section(
    config_dir: Path, section_heading: str, section_body: str
) -> Dict[str, Any]:
    err = _reject_secrets((section_body or "") + "\n" + (section_heading or ""))
    if err:
        return {"success": False, "error": err}
    path = _preferences_path(Path(config_dir))
    meta, body = _read_file(path)
    preamble, secs = parse_sections(body)
    merged = merge_sections(preamble, secs, section_heading, section_body or "")
    if len(merged) > MAX_BODY_CHARS:
        return {
            "success": False,
            "error": f"Merged content exceeds the limit of {MAX_BODY_CHARS} characters. Delete other sections or shorten the content.",
        }
    _write_file(path, meta, merged)
    return {"success": True, "path": str(path), "section": section_heading.strip()}
