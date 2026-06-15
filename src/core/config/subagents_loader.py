"""
Load sub-agent definitions from Markdown files with YAML frontmatter.

Each sub-agent is a single ``<subagents_root>/<name>.md`` file:

    ---
    name: code-reviewer
    description: Use to review a diff or file for bugs and style issues.
    model: openai:gpt-4o            # optional; default = main model
    tools: [shell, apply_patch]      # optional allowlist; default = core tools
    max_rounds: 20                   # optional
    ---
    <body = the sub-agent's independent system instructions / prompt>

Roots (merged, later overrides earlier by ``name``):
- Global external: ``<config_dir>/subagents/``
- Workspace external: ``<workspace_config_dir>/subagents/`` (highest priority)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate
from .skills_loader import _split_frontmatter

SUBAGENTS_DIRNAME = "subagents"
DEFAULT_SUBAGENT_MAX_ROUNDS = 20


def _t(language: Optional[str], key: str, **kwargs: object) -> str:
    return translate(key, normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE, **kwargs)


@dataclass(frozen=True)
class SubAgentRecord:
    """One sub-agent definition parsed from a Markdown file."""

    name: str
    description: str
    instructions: str
    # Optional ``provider:name`` selector referencing ``model_providers``;
    # empty means "reuse the main agent's current model".
    model_selector: str = ""
    # Optional allowlist of tool names. When ``tools_specified`` is False the
    # default core set is used; when True the ``tools`` list is honored exactly
    # (an explicit empty list means "no tools").
    tools: List[str] = field(default_factory=list)
    tools_specified: bool = False
    max_rounds: int = DEFAULT_SUBAGENT_MAX_ROUNDS
    source_path: str = ""


def _coerce_tools_list(value: object) -> List[str]:
    if value is None:
        return []
    items: List[str]
    if isinstance(value, str):
        # Allow comma/space separated strings in addition to YAML lists.
        raw = value.replace(",", " ").split()
        items = raw
    elif isinstance(value, (list, tuple)):
        items = [str(x) for x in value]
    else:
        return []
    out: List[str] = []
    seen = set()
    for item in items:
        name = str(item or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def _coerce_max_rounds(value: object) -> int:
    try:
        parsed = int(value)
    except Exception:
        return DEFAULT_SUBAGENT_MAX_ROUNDS
    if parsed <= 0:
        return DEFAULT_SUBAGENT_MAX_ROUNDS
    return parsed


def _scan_subagents_root(subagents_root: Path, language: Optional[str] = None) -> List[SubAgentRecord]:
    """Scan ``<subagents_root>/*.md`` and return parsed records (sorted by name)."""
    root = Path(subagents_root).expanduser().resolve()
    if not root.is_dir():
        return []

    out: List[SubAgentRecord] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_file() or child.suffix.lower() != ".md":
            continue
        try:
            raw = child.read_text(encoding="utf-8")
        except OSError as e:
            print(_t(language, "subagents_loader.read_failed", path=child, error=e))
            continue

        meta, body = _split_frontmatter(raw)
        fallback_name = child.stem.strip()
        if meta is None:
            # No frontmatter: skip, since a sub-agent requires at least a
            # description to drive main-model selection.
            print(_t(language, "subagents_loader.missing_frontmatter", path=child))
            continue

        name = str(meta.get("name") or "").strip() or fallback_name
        description = str(meta.get("description") or "").strip()
        if not name or not description:
            print(_t(language, "subagents_loader.missing_fields", path=child))
            continue

        model_selector = str(meta.get("model") or "").strip()
        tools_specified = "tools" in meta
        tools = _coerce_tools_list(meta.get("tools"))
        max_rounds = _coerce_max_rounds(meta.get("max_rounds"))
        instructions = body.strip()
        if not instructions:
            print(_t(language, "subagents_loader.missing_instructions", path=child))
            continue

        out.append(
            SubAgentRecord(
                name=name,
                description=description,
                instructions=instructions,
                model_selector=model_selector,
                tools=tools,
                tools_specified=tools_specified,
                max_rounds=max_rounds,
                source_path=str(child.resolve()),
            )
        )
    return out


def _global_subagents_root(config_dir: Path) -> Path:
    return Path(config_dir).expanduser().resolve() / SUBAGENTS_DIRNAME


def _workspace_subagents_root(workspace_dir: Path) -> Path:
    return Path(workspace_dir).expanduser().resolve() / SUBAGENTS_DIRNAME


def load_subagents_merged(
    config_dir: Path,
    workspace_dir: Optional[Path] = None,
    language: Optional[str] = None,
) -> List[SubAgentRecord]:
    """Merge global and workspace sub-agents.

    Priority (low -> high):
    - Global external: ``<config_dir>/subagents/``
    - Workspace external: ``<workspace_config_dir>/subagents/``

    Same ``name``: higher-priority source overrides lower-priority source.
    """
    external = _scan_subagents_root(_global_subagents_root(config_dir), language=language)
    workspace: List[SubAgentRecord] = []
    if workspace_dir is not None:
        workspace = _scan_subagents_root(_workspace_subagents_root(Path(workspace_dir)), language=language)

    by_name: Dict[str, SubAgentRecord] = {}
    for rec in external:
        by_name[rec.name.lower()] = rec
    for rec in workspace:
        by_name[rec.name.lower()] = rec
    return sorted(by_name.values(), key=lambda x: x.name.lower())


def _subagents_root_fingerprint_part(subagents_root: Path) -> Dict[str, object]:
    root = Path(subagents_root).expanduser().resolve()
    if not root.is_dir():
        return {"exists": False, "root": str(root), "files": []}
    files: List[Dict[str, object]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_file() or child.suffix.lower() != ".md":
            continue
        try:
            st = child.stat()
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            size = int(st.st_size)
        except OSError:
            mtime_ns = -1
            size = -1
        files.append({"name": child.name, "mtime_ns": mtime_ns, "size": size})
    return {"exists": True, "root": str(root), "files": files}


def calc_subagents_dirs_fingerprint(
    config_dir: Path,
    workspace_dir: Optional[Path] = None,
) -> str:
    """Deterministic fingerprint over all sub-agent roots for cheap reload checks."""
    payload: Dict[str, object] = {
        "global_external": _subagents_root_fingerprint_part(_global_subagents_root(config_dir)),
        "workspace_external": _subagents_root_fingerprint_part(_workspace_subagents_root(Path(workspace_dir)))
        if workspace_dir is not None
        else None,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
