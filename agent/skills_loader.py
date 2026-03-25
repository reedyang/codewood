"""
Load Agent Skills from Anthropic-style folders: ``<skills_root>/<id>/SKILL.md`` (YAML frontmatter + body).

- **Workspace:** ``<workspace_dir>/skills/`` (lowest priority)
- **Builtin:** typically ``<main.py dir>/skills/``
- **External:** ``<config_dir>/skills/`` (beside ``config.json``, highest priority)

See: https://github.com/anthropics/skills/blob/main/README.md
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass(frozen=True)
class SkillRecord:
    """One skill folder with a parsed SKILL.md."""

    name: str
    description: str
    body: str
    skill_id: str
    # Absolute path to the skill directory (sibling of SKILL.md); bundled scripts live under here.
    bundle_root: str


def _split_frontmatter(text: str) -> Tuple[Optional[dict], str]:
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
            return None, text
        return meta, body
    except yaml.YAMLError:
        return None, text


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
        if meta is None:
            # No valid frontmatter: use whole file as body, id from folder name
            if raw.lstrip("\ufeff").startswith("---"):
                print(f"⚠️ 跳过技能（frontmatter 无效）: {skill_md}")
                continue
            out.append(
                SkillRecord(
                    name=child.name,
                    description="",
                    body=raw.strip(),
                    skill_id=child.name,
                    bundle_root=str(child.resolve()),
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

        out.append(
            SkillRecord(
                name=name,
                description=desc_s,
                body=body.strip(),
                skill_id=child.name,
                bundle_root=str(child.resolve()),
            )
        )
    return out


def load_skills_from_config_dir(config_dir: Path) -> List[SkillRecord]:
    """Load only external skills: config_dir/skills/."""
    return _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills")


def load_skills_merged(
    config_dir: Path,
    builtin_skills_dir: Optional[Path] = None,
    workspace_dir: Optional[Path] = None,
) -> List[SkillRecord]:
    """
    Merge workspace, builtin and external Agent Skills.

    Priority (low -> high):
    - Workspace: ``<workspace_dir>/skills/`` (workspace-side skills)
    - Builtin: ``builtin_skills_dir`` (typically ``<main.py dir>/skills/``)
    - External: ``<config_dir>/skills/`` (user / config-side skills)

    Same ``skill_id`` (folder name): higher-priority source overrides lower-priority source.
    """
    workspace: List[SkillRecord] = []
    if workspace_dir is not None:
        workspace = _scan_skills_root(Path(workspace_dir).expanduser().resolve() / "skills")
    external = _scan_skills_root(Path(config_dir).expanduser().resolve() / "skills")
    builtin: List[SkillRecord] = []
    if builtin_skills_dir is not None:
        builtin = _scan_skills_root(builtin_skills_dir)

    by_id: Dict[str, SkillRecord] = {}
    for s in workspace:
        by_id[s.skill_id] = s
    for s in builtin:
        by_id[s.skill_id] = s
    for s in external:
        by_id[s.skill_id] = s
    return sorted(by_id.values(), key=lambda x: x.skill_id.lower())


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
        "下列技能按优先级由低到高合并加载：`workspace/skills/` → 内建 `skills/`（与 `main.py` 同目录）→ `config.json` 同目录下的外部 `skills/`；**同名技能以后者覆盖前者**。",
        "**在输出任何 JSON 操作指令之前**：若当前用户任务与下文中某项「简述」在语义上相符（含同义表述与相关子任务，例如「CSV/表格/Excel/xlsx/转表」与表格处理类技能），你必须：",
        "**表格类任务特别提示**：若已加载名为 **`xlsx`** 的技能，则「CSV/TSV 转 xlsx、编辑表格、读写 `.xlsx`」等均属该技能；打开其正文后必须先按其中的 **Task routing (mandatory)** 选工具。**禁止**用该技能包内的 `scripts/recalc.py` 作为 CSV→xlsx 的第一步（`recalc.py` 仅用于已有 `.xlsx` 内公式的重算）。CSV→xlsx 应使用 **pandas**（`read_csv` + `to_excel`）或等价 `script`。",
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
            "**Bundled files:** 技能正文里的 `scripts/...` 等路径相对于各技能在磁盘上的目录（见下文 **Skill bundle root**）。"
            "`shell` 在**用户工作目录**执行，不会自动进入技能目录；调用随包脚本时必须使用 **绝对路径**（下文已列出本机检测到的 `.py` 时可直接复制）。",
            "",
            "---",
            "",
            "（以下为 Smart Shell 通用能力说明；完整技能正文在文档后部的「Agent Skills（详细内容）」。）",
            "",
        ]
    )
    return "\n".join(lines)


def build_skills_system_append(skills: List[SkillRecord]) -> str:
    """Full skill bodies: placed after system_prompt.md so details follow general rules."""
    if not skills:
        return ""

    lines = [
        "",
        "## Agent Skills（详细内容）",
        "",
        "以下为各技能完整正文，与文首 **「Agent Skills 索引」** 一一对应；任务匹配时须按此处正文执行。"
        "规范参见 [Agent Skills / anthropics/skills](https://github.com/anthropics/skills/blob/main/README.md)。",
        "",
    ]
    for s in skills:
        lines.append(f"### Skill: `{s.name}` · 目录 `{s.skill_id}`")
        lines.append(f"**Description:** {s.description}")
        lines.append("")
        lines.append(f"**Skill bundle root (absolute path on this machine):** `{s.bundle_root}`")
        lines.append(
            "正文中的 `scripts/` 等路径均相对于该目录。`shell` 在用户当前工作目录执行，"
            "**不会**切换到技能目录；调用正文中的脚本时，请使用 **bundle root + 相对路径** 组成的绝对路径（或对下列已检测脚本逐字使用路径）。"
        )
        bundled = _list_bundled_script_paths(s.bundle_root)
        if bundled:
            lines.append("**Detected bundled `scripts/*.py` (use these full paths in `shell`):**")
            for p in bundled:
                lines.append(f"- `{p}`")
            lines.append("")
        lines.append(s.body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
