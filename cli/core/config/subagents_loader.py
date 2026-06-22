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
    # When False the sub-agent is parsed and surfaced in config UIs but is NOT
    # offered to the model (filtered out of the runtime ``agent.subagents``).
    enabled: bool = True


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


def _coerce_enabled(value: object) -> bool:
    """Parse the optional ``enabled`` frontmatter flag (default: True).

    Accepts real booleans and common string spellings so a hand-edited file
    (``enabled: false`` / ``no`` / ``0``) behaves intuitively.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("false", "no", "off", "0", "disabled"):
        return False
    return True


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
        enabled = _coerce_enabled(meta.get("enabled"))
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
                enabled=enabled,
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


# --- Config-UI CRUD over the GLOBAL sub-agents root --------------------------
#
# The desktop GUI manages sub-agents as ``<config_dir>/subagents/<name>.md``
# files. We deliberately scope writes to the global root (never the workspace
# root) so a misconfigured workspace can't be silently rewritten, and so the
# behaviour is predictable regardless of which workspace is active.

import re as _re

import yaml as _yaml


_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


def _sanitize_subagent_filename(name: str) -> str:
    """Map a sub-agent ``name`` to a safe ``<stem>.md`` filename.

    Rejects path separators / traversal by keeping only a conservative
    character set; the result is always a bare filename within the root.
    """
    stem = str(name or "").strip().lower()
    stem = _re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-")
    return stem or "subagent"


def is_valid_subagent_name(name: str) -> bool:
    return bool(_NAME_RE.match(str(name or "").strip()))


def _resolve_within_root(root: Path, filename: str) -> Optional[Path]:
    """Resolve ``filename`` under ``root``, rejecting traversal escapes."""
    root = root.expanduser().resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def subagent_to_dict(rec: SubAgentRecord) -> Dict[str, object]:
    """Serialize a record for the config UI (round-trips through write_*)."""
    return {
        "name": rec.name,
        "description": rec.description,
        "instructions": rec.instructions,
        "model": rec.model_selector,
        "tools": list(rec.tools),
        "toolsSpecified": bool(rec.tools_specified),
        "maxRounds": int(rec.max_rounds),
        "enabled": bool(rec.enabled),
        "sourcePath": rec.source_path,
        # A workspace override lives outside the global root we manage; the UI
        # can surface it read-only so the user isn't surprised that an edit
        # targets the global copy.
        "global": True,
    }


def list_subagents_for_config(config_dir: Path) -> List[Dict[str, object]]:
    """List GLOBAL sub-agents (including disabled ones) for the config UI."""
    records = _scan_subagents_root(_global_subagents_root(config_dir))
    return [subagent_to_dict(r) for r in records]


def _render_subagent_markdown(
    *,
    name: str,
    description: str,
    instructions: str,
    model: str,
    tools: List[str],
    tools_specified: bool,
    max_rounds: int,
    enabled: bool,
) -> str:
    meta: Dict[str, object] = {
        "name": str(name).strip(),
        "description": str(description).strip(),
    }
    if str(model or "").strip():
        meta["model"] = str(model).strip()
    if tools_specified:
        meta["tools"] = [str(t).strip() for t in (tools or []) if str(t).strip()]
    if int(max_rounds) != DEFAULT_SUBAGENT_MAX_ROUNDS:
        meta["max_rounds"] = int(max_rounds)
    if not enabled:
        meta["enabled"] = False
    front = _yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    body = str(instructions or "").strip()
    return f"---\n{front}\n---\n\n{body}\n"


def write_subagent(
    config_dir: Path,
    *,
    original_name: str,
    name: str,
    description: str,
    instructions: str,
    model: str = "",
    tools: Optional[List[str]] = None,
    tools_specified: bool = False,
    max_rounds: int = DEFAULT_SUBAGENT_MAX_ROUNDS,
    enabled: bool = True,
) -> Dict[str, object]:
    """Create or update a global sub-agent file. Returns ``{ok, error}``."""
    clean_name = str(name or "").strip()
    if not is_valid_subagent_name(clean_name):
        return {"ok": False, "error": "invalid_name"}
    if not str(description or "").strip():
        return {"ok": False, "error": "description_required"}
    if not str(instructions or "").strip():
        return {"ok": False, "error": "instructions_required"}

    root = _global_subagents_root(config_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "error": "io_error"}

    new_filename = _sanitize_subagent_filename(clean_name) + ".md"
    new_path = _resolve_within_root(root, new_filename)
    if new_path is None:
        return {"ok": False, "error": "invalid_name"}

    orig = str(original_name or "").strip()
    old_path: Optional[Path] = None
    if orig:
        old_path = _resolve_within_root(root, _sanitize_subagent_filename(orig) + ".md")

    # Renames and brand-new agents must not silently clobber a different file.
    is_rename_or_new = old_path is None or old_path.resolve() != new_path.resolve()
    if is_rename_or_new and new_path.exists():
        return {"ok": False, "error": "name_exists"}

    content = _render_subagent_markdown(
        name=clean_name,
        description=description,
        instructions=instructions,
        model=model,
        tools=list(tools or []),
        tools_specified=bool(tools_specified),
        max_rounds=int(max_rounds),
        enabled=bool(enabled),
    )
    try:
        new_path.write_text(content, encoding="utf-8")
    except OSError:
        return {"ok": False, "error": "io_error"}

    # On a successful rename, drop the old file.
    if old_path is not None and old_path.resolve() != new_path.resolve() and old_path.exists():
        try:
            old_path.unlink()
        except OSError:
            pass
    return {"ok": True, "error": ""}


def delete_subagent(config_dir: Path, name: str) -> Dict[str, object]:
    """Delete a global sub-agent file by name. Returns ``{ok, error}``."""
    root = _global_subagents_root(config_dir)
    path = _resolve_within_root(root, _sanitize_subagent_filename(name) + ".md")
    if path is None:
        return {"ok": False, "error": "invalid_name"}
    if not path.exists():
        return {"ok": False, "error": "not_found"}
    try:
        path.unlink()
    except OSError:
        return {"ok": False, "error": "io_error"}
    return {"ok": True, "error": ""}


def set_subagent_enabled(config_dir: Path, name: str, enabled: bool) -> Dict[str, object]:
    """Flip a global sub-agent's enabled flag in place. Returns ``{ok, error}``."""
    root = _global_subagents_root(config_dir)
    records = _scan_subagents_root(root)
    target = None
    needle = str(name or "").strip().lower()
    for rec in records:
        if rec.name.strip().lower() == needle:
            target = rec
            break
    if target is None:
        return {"ok": False, "error": "not_found"}
    return write_subagent(
        config_dir,
        original_name=target.name,
        name=target.name,
        description=target.description,
        instructions=target.instructions,
        model=target.model_selector,
        tools=list(target.tools),
        tools_specified=target.tools_specified,
        max_rounds=target.max_rounds,
        enabled=bool(enabled),
    )
