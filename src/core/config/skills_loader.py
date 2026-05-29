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


def _parse_model_context_file_env_from_meta(meta: Optional[dict]) -> Optional[str]:
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
        print(f"⚠️ SKILL.md frontmatter 中 model_context_file_env 无效: {s!r}，已忽略")
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


def _scan_skills_root(skills_root: Path) -> List[SkillRecord]:
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
            print(f"⚠️ 无法读取技能文件 {skill_md}: {e}")
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

        bridge_env = _parse_model_context_file_env_from_meta(meta)
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


def load_skills_from_config_dir(config_dir: Path) -> List[SkillRecord]:
    """Load only external skills: config_dir/skills/."""
    return _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills")


def _workspace_config_skills_root(workspace_dir: Path) -> Path:
    return Path(workspace_dir).expanduser().resolve() / "skills"


def load_skills_merged(
    config_dir: Path,
    builtin_skills_dir: Optional[Path] = None,
    workspace_dir: Optional[Path] = None,
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
        workspace = _scan_skills_root(_workspace_config_skills_root(Path(workspace_dir)))
    external = _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills")
    builtin: List[SkillRecord] = []
    if builtin_skills_dir is not None:
        builtin = _scan_skills_root(builtin_skills_dir)

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
        "## Agent Skills 索引（系统提示最前，必读）",
        "",
        f"下列技能按优先级由低到高合并加载：项目根目录内建 `skills/` → 全局 `config/skills/` → `workspace/{get_app_config_dirname()}/skills/`；**同名技能以后者覆盖前者**。",
        "**在输出任何 JSON 操作指令之前**：若当前用户任务与下文中某项「简述」在语义上相符（含同义表述与相关子任务），你必须：",
        "",
        "1. 在本提示靠后的 **「Agent Skills（详细内容）」** 一节中找到对应技能；",
        "2. **严格按该技能正文**中的步骤、推荐工具、脚本形态与约束执行，不得擅自改用与技能冲突的捷径；",
        "3. 技能正文与上文通用说明冲突时，**以技能正文为准**。",
        "",
        "**已加载技能一览：**",
        "",
    ]
    for s in skills:
        lines.append(f"- **`{s.name}`** · 目录 `{s.skill_id}` — {s.description}")
    lines.extend(
        [
            "",
            "**`tool` 字段与技能（必读）：** 上表中的 **目录名 `skill_id` 不是合法 `tool` 名**（禁止输出如 `{\"tool\":\"weather\"}`）。"
            "必须先调用内置工具 **`request_skill_prompt`**（`args.skill_id` 填目录名）注入 SKILL 全文，再按正文用 `shell` 等执行。"
            "除 `tools.jsonc` / 下文 Available tools 所列名称外，**禁止虚构工具名**（例如把「查天气」映射成不存在的 `weather` 工具）。",
            "",
            "**Bundled files:** 技能正文里的 `scripts/...` 等路径相对于各技能在磁盘上的目录（见下文 **Skill bundle root**）。"
            "`shell` 在**用户工作目录**执行，不会自动进入技能目录；调用随包脚本时必须使用 **绝对路径**（下文已列出本机检测到的 `.py` 时可直接复制）。",
            "",
            "---",
            "",
            f"（以下为 {get_app_name()} 通用能力说明；完整技能正文在文档后部的「Agent Skills（详细内容）」。）",
            "",
        ]
    )
    return "\n".join(lines)
