"""
Load Agent Skills from Anthropic-style folders: ``<skills_root>/<id>/SKILL.md`` (YAML frontmatter + body).

- **Builtin:** typically ``<project_root>/skills/`` (lowest priority)
- **Global external:** ``<config_dir>/skills/``
- **Workspace external:** ``<workspace_dir>/<app_config_dir>/skills/`` (highest priority)

See: https://github.com/anthropics/skills/blob/main/README.md
"""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from ...config.app_info import get_app_config_dirname, get_app_name
from ..localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, translate


def _t(language: Optional[str], key: str, **kwargs: object) -> str:
    return translate(key, normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE, **kwargs)


def _parse_model_context_file_env_from_meta(meta: Optional[dict], language: Optional[str] = None) -> Optional[str]:
    """
    Optional YAML frontmatter in ``SKILL.md`` may declare how a host passes a temp
    file path to child processes (extended model context). Keys
    ``model_context_file_env`` or ``modelContextFileEnv`` = env var name.
    """
    if not meta or not isinstance(meta, dict):
        return None
    v = meta.get("model_context_file_env")
    if v is None:
        v = meta.get("modelContextFileEnv")
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s):
        print(_t(language, "skills_loader.invalid_model_context_file_env", value=repr(s)))
        return None
    return s


@dataclass(frozen=True)
class SkillRecord:
    """One skill folder with a parsed SKILL.md."""

    name: str
    description: str
    body: str
    skill_id: str
    # Absolute path to the skill directory (sibling of SKILL.md); bundled scripts live under here.
    bundle_root: str
    # From optional SKILL.md YAML frontmatter (model_context_file_env): host-supplied temp file path env.
    model_context_file_env: Optional[str] = None


def _split_frontmatter(text: str) -> Tuple[Optional[dict], str]:
    def _fallback_parse_meta(raw_meta: str) -> dict:
        """
        Lenient parser for common SKILL frontmatter seen in Cursor/Codex exports.
        Supports simple `key: value` pairs and appends list lines under description.
        """
        out: Dict[str, str] = {}
        last_key = ""
        for raw_line in str(raw_meta or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                key = str(k).strip()
                val = str(v).strip().strip('"').strip("'")
                if key:
                    out[key] = val
                    last_key = key
                continue
            if line.startswith("- ") and last_key == "description":
                prev = out.get("description", "")
                out["description"] = (prev + ("\n" if prev else "") + line).strip()
        return out

    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    raw_meta = parts[1].strip()
    body = parts[2]
    try:
        meta = yaml.safe_load(raw_meta) or {}
        if not isinstance(meta, dict):
            meta = _fallback_parse_meta(raw_meta)
            if not meta:
                return None, text
        return meta, body
    except yaml.YAMLError:
        meta = _fallback_parse_meta(raw_meta)
        if not meta:
            return None, text
        return meta, body


def _scan_skills_root(skills_root: Path, language: Optional[str] = None) -> List[SkillRecord]:
    """Scan <skills_root>/*/SKILL.md and return SkillRecord list (sorted by folder name)."""
    root = Path(skills_root).expanduser().resolve()
    if not root.is_dir():
        return []

    out: List[SkillRecord] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError as e:
            print(_t(language, "skills_loader.read_failed", path=skill_md, error=e))
            continue

        meta, body = _split_frontmatter(raw)
        bundle_path = child.resolve()
        if meta is None:
            # No valid frontmatter: use whole file as body, id from folder name
            out.append(
                SkillRecord(
                    name=child.name,
                    description="",
                    body=raw.strip(),
                    skill_id=child.name,
                    bundle_root=str(bundle_path),
                    model_context_file_env=None,
                )
            )
            continue

        name = (meta.get("name") or "").strip()
        if not name:
            name = child.name
        desc = meta.get("description")
        if desc is None:
            desc_s = ""
        elif isinstance(desc, str):
            desc_s = desc.strip()
        else:
            desc_s = str(desc).strip()

        bridge_env = _parse_model_context_file_env_from_meta(meta, language=language)
        out.append(
            SkillRecord(
                name=name,
                description=desc_s,
                body=body.strip(),
                skill_id=child.name,
                bundle_root=str(bundle_path),
                model_context_file_env=bridge_env,
            )
        )
    return out


def load_skills_from_config_dir(config_dir: Path, language: Optional[str] = None) -> List[SkillRecord]:
    """Load only external skills: config_dir/skills/."""
    return _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills", language=language)


def _workspace_config_skills_root(workspace_dir: Path) -> Path:
    return Path(workspace_dir).expanduser().resolve() / "skills"


def load_skills_merged(
    config_dir: Path,
    builtin_skills_dir: Optional[Path] = None,
    workspace_dir: Optional[Path] = None,
    language: Optional[str] = None,
) -> List[SkillRecord]:
    """
    Merge builtin, global external, and workspace external Agent Skills.

    Priority (low -> high):
    - Builtin: ``builtin_skills_dir`` (typically ``<project_root>/skills/``)
    - Global external: ``<config_dir>/skills/`` (user / config-side skills)
    - Workspace external: ``<workspace_storage_dir>/skills/`` (workspace-local skills)

    Same ``skill_id`` (folder name): higher-priority source overrides lower-priority source.
    """
    workspace: List[SkillRecord] = []
    if workspace_dir is not None:
        workspace = _scan_skills_root(_workspace_config_skills_root(Path(workspace_dir)), language=language)
    external = _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills", language=language)
    builtin: List[SkillRecord] = []
    if builtin_skills_dir is not None:
        builtin = _scan_skills_root(builtin_skills_dir, language=language)

    by_id: Dict[str, SkillRecord] = {}
    for s in builtin:
        by_id[s.skill_id] = s
    for s in external:
        by_id[s.skill_id] = s
    for s in workspace:
        by_id[s.skill_id] = s
    return sorted(by_id.values(), key=lambda x: x.skill_id.lower())


def _skills_root_fingerprint_part(skills_root: Path) -> Dict[str, object]:
    root = Path(skills_root).expanduser().resolve()
    if not root.is_dir():
        return {"exists": False, "root": str(root), "skills": []}
    skills: List[Dict[str, object]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            st = skill_md.stat()
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            size = int(st.st_size)
        except OSError:
            mtime_ns = -1
            size = -1
        skills.append(
            {
                "id": child.name,
                "skill_md_mtime_ns": mtime_ns,
                "skill_md_size": size,
            }
        )
    return {"exists": True, "root": str(root), "skills": skills}


def calc_skills_dirs_fingerprint(
    config_dir: Path,
    builtin_skills_dir: Optional[Path] = None,
    workspace_dir: Optional[Path] = None,
) -> str:
    """
    Build a deterministic fingerprint for all skill roots so caller can skip
    expensive reload when nothing changed.
    """
    payload: Dict[str, object] = {
        "global_external": _skills_root_fingerprint_part(Path(config_dir).expanduser().resolve() / "skills"),
        "builtin": _skills_root_fingerprint_part(builtin_skills_dir) if builtin_skills_dir is not None else None,
        "workspace_external": _skills_root_fingerprint_part(_workspace_config_skills_root(Path(workspace_dir)))
        if workspace_dir is not None
        else None,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _list_bundled_script_paths(bundle_root: str, max_files: int = 20) -> List[str]:
    """Return absolute paths to *.py under <bundle_root>/scripts/, if present."""
    scripts = Path(bundle_root) / "scripts"
    if not scripts.is_dir():
        return []
    out = sorted(scripts.glob("*.py"), key=lambda p: p.name.lower())
    return [str(p.resolve()) for p in out[:max_files]]


def build_skills_routing_prefix(skills: List[SkillRecord]) -> str:
    """
    Short block placed *before* system_prompt.md so models attend to skills (routing)
    before long JSON rules. Does not duplicate full SKILL bodies.
    """
    if not skills:
        return ""

    lines = [
        "## Agent Skills Index (front of system prompt, required reading)",
        "",
        f"The following skills are merged from low to high priority: project built-in `skills/` -> global `config/skills/` -> `workspace/{get_app_config_dirname()}/skills/`; skills with the same name are overridden by later sources.",
        "Before any tool action: if the current user task semantically matches a skill description below, including synonyms and related subtasks, you must:",
        "",
        "1. Find the corresponding skill in the later **Agent Skills (Detailed Content)** section;",
        "2. Strictly follow that skill body's steps, recommended tools, script forms, and constraints; do not replace it with shortcuts that conflict with the skill;",
        "3. If a skill body conflicts with general instructions above, follow the skill body, except for safety, privilege, and destructive-action hard limits.",
        "",
        "**Loaded skills:**",
        "",
    ]
    for s in skills:
        lines.append(f"- **`{s.name}`** · directory `{s.skill_id}` - {s.description}")
    lines.extend(
        [
            "",
            "**The `tool` field and skills (required):** the directory `skill_id` values above are not legal tool names. Do not output a skill directory name as a tool name. "
            "First call the built-in tool **`request_skill_prompt`** with `args.skill_id` set to the directory name to inject the full SKILL body, then follow the body using business tools such as `shell`. "
            "Do not invent tool names outside `tools.jsonc` / the later Available tools list, for example mapping a weather request to a nonexistent `weather` tool.",
            "",
            "**Bundled files:** paths such as `scripts/...` inside a skill body are relative to that skill's on-disk directory (see **Skill bundle root** below). "
            "`shell` runs in the user's working directory and does not automatically enter the skill directory; when calling bundled scripts, use absolute paths. Detected `.py` script paths below may be copied directly.",
            "",
            "---",
            "",
            f"(The following is the general {get_app_name()} capability guidance; full skill bodies appear later in **Agent Skills (Detailed Content)**.)",
            "",
        ]
    )
    return "\n".join(lines)
