import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.config.skills_loader import _list_bundled_script_paths


def _src_root() -> Path:
    """Return absolute src/ root regardless of current module subdirectory."""
    return Path(__file__).resolve().parent.parent


def build_mcp_system_append(agent: Any) -> str:
    """Build MCP section appended to system prompt (with redacted env values)."""
    servers = (agent.mcp_config or {}).get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        return "\n\n## MCP 配置\n未检测到可用 MCP server（config 目录下无 mcp.json 或配置为空）。"
    status_servers: Dict[str, Any] = {}
    try:
        status_servers = (
            (agent.mcp_manager.get_status().get("servers", {}) or {})
            if agent.mcp_manager
            else {}
        )
    except Exception:
        status_servers = {}
    loaded: List[str] = []
    not_loaded: List[str] = []
    lines: List[str] = [
        "",
        "",
        "## MCP 配置",
        "已从 config 目录下的 mcp.json 加载 MCP servers。调用前请优先选择最匹配的 server。",
        "仅可引用“已加载”server 的工具能力；未加载 server 禁止在自然语言中当作可用能力引用。",
        "决策约束：当“已加载 + 已缓存 tools”中存在可覆盖用户意图的工具时，必须优先走 mcp_call_tool，"
        "不得先创建临时脚本或调用 shell 模拟实现（除非工具调用已明确失败且无等价 MCP 工具）。",
        "可用 servers（敏感 env 已脱敏，仅显示键名）：",
    ]
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            lines.append(f"- {name}: 配置无效（应为 object）")
            continue
        st = status_servers.get(name, {})
        state_raw = str(st.get("state", "pending") or "pending").lower()
        state = "loaded" if state_raw == "success" else state_raw
        if state == "loaded":
            loaded.append(str(name))
        else:
            not_loaded.append(str(name))
        if "url" in conf:
            lines.append(f"- {name}: state={state}, type=remote, url={conf.get('url')}")
        else:
            cmd = str(conf.get("command", "")).strip() or "<missing>"
            args = conf.get("args", [])
            arg_preview = (
                " ".join(str(x) for x in args[:3]) if isinstance(args, list) else ""
            )
            if len(arg_preview) > 120:
                arg_preview = arg_preview[:117] + "..."
            lines.append(
                f"- {name}: state={state}, type=stdio, command={cmd}, args={arg_preview}"
            )
        env = conf.get("env")
        if isinstance(env, dict) and env:
            env_keys = ", ".join(str(k) for k in sorted(env.keys()))
            lines.append(f"  env_keys: {env_keys}")
    lines.append(f"已加载 servers: {', '.join(loaded) if loaded else '无'}")
    lines.append(f"未加载 servers: {', '.join(not_loaded) if not_loaded else '无'}")
    lines.append("已缓存 tools（调用 mcp_list_tools 后更新）：")
    try:
        lines.append(agent.mcp_manager.cached_tools_for_prompt())
    except Exception:
        lines.append("尚无已缓存的 MCP tools。")
    lines.append("已缓存 resources（调用 mcp_list_resources 后更新）：")
    try:
        lines.append(agent.mcp_manager.cached_resources_for_prompt())
    except Exception:
        lines.append("尚无已缓存的 MCP resources。")
    lines.append("已缓存 prompts（调用 mcp_list_prompts 后更新）：")
    try:
        lines.append(agent.mcp_manager.cached_prompts_for_prompt())
    except Exception:
        lines.append("尚无已缓存的 MCP prompts。")
    return "\n".join(lines)


def strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC while preserving string literals."""
    out: List[str] = []
    i = 0
    in_str = False
    esc = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] not in ("\n", "\r"):
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = i + 2 if i + 1 < n else n
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_tools_spec_from_jsonc(agent: Any) -> List[Dict[str, Any]]:
    """Load tool specs from tools.jsonc with comment stripping."""
    path = _src_root() / "config" / "tools.jsonc"
    try:
        raw = path.read_text(encoding="utf-8")
        clean = strip_jsonc_comments(raw)
        parsed = json.loads(clean)
        if not isinstance(parsed, list):
            raise ValueError("tools.jsonc root must be array")
        specs = [x for x in parsed if isinstance(x, dict)]

        if not bool(getattr(agent, "mcp_tools_enabled", False)):
            disabled_mcp_tools = {
                "mcp_server_info",
                "mcp_disable_tools",
                "mcp_enable_tools",
                "mcp_list_disabled_tools",
                "mcp_sampling_create_message",
                "mcp_completion_complete",
            }
            specs = [
                x
                for x in specs
                if str((x.get("function", {}) or {}).get("name", "")).strip()
                not in disabled_mcp_tools
            ]

        if not bool(getattr(agent, "knowledge_tools_enabled", False)):
            disabled_knowledge_tools = {
                "knowledge_sync",
                "knowledge_stats",
            }
            specs = [
                x
                for x in specs
                if str((x.get("function", {}) or {}).get("name", "")).strip()
                not in disabled_knowledge_tools
            ]

        return specs
    except Exception as e:
        print(f"⚠️ tools.jsonc 加载失败: {e}")
        return []


def build_user_preferences_system_append(agent: Any) -> str:
    """持久化用户偏好文件，固定注入 system（在 MCP/tools 之前）。"""
    try:
        from ..core.state import user_preferences_manager as _upm

        return _upm.build_system_append(Path(agent.config_dir))
    except Exception:
        return ""


def build_agents_md_system_append(agent: Any) -> str:
    """Inject AGENTS.md content from config/workspace-related locations."""
    candidates: List[Tuple[str, Path]] = []
    try:
        candidates.append(("config", Path(agent.config_dir) / "AGENTS.md"))
    except Exception:
        pass
    try:
        candidates.append(("workspace", Path(agent.ai_workspace_dir) / "AGENTS.md"))
    except Exception:
        pass
    try:
        candidates.append(
            (
                "workspace/.smartshell",
                Path(agent.ai_workspace_dir) / ".smartshell" / "AGENTS.md",
            )
        )
    except Exception:
        pass

    sections: List[str] = []
    seen_keys: set = set()
    for scope, file_path in candidates:
        try:
            resolved = file_path.expanduser().resolve()
        except Exception:
            resolved = file_path
        key = str(resolved).casefold() if os.name == "nt" else str(resolved)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if not resolved.is_file():
            continue
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not content:
            continue
        sections.append(
            "\n".join(
                [
                    f"### {scope} AGENTS.md",
                    f"Source: `{resolved}`",
                    content,
                ]
            )
        )

    if not sections:
        return ""
    header = (
        "\n\n## User Custom Prompts (AGENTS.md)\n\n"
        "优先级说明：本节用于注入用户自定义提示；当用户在当前请求中**显式指定 skill**"
        "（例如 `/skills/<skill-name>` 或已触发 `request_skill_prompt`）且与本节冲突时，"
        "以显式指定的 skill 正文为准。\n\n"
    )
    return header + "\n\n".join(sections) + "\n"


def compose_system_prompt_snapshot(agent: Any, include_tools: bool) -> str:
    """组装当前可见 system 快照：base + 用户偏好 + MCP [+ 工具目录]。"""
    core = (
        agent._base_system_prompt
        + build_agents_md_system_append(agent)
        + build_user_preferences_system_append(agent)
        + build_mcp_system_append(agent)
        + build_runtime_cache_prompt_append(agent, default_workspace_id="default")
        + build_os_file_ops_prompt_append()
    )
    if include_tools:
        return core + "\n" + build_tools_prompt_append(agent)
    return core


def build_runtime_cache_prompt_append(agent: Any, default_workspace_id: str) -> str:
    """Provide generic runtime cache-dir hints for all skills/scripts."""
    ws_root = Path(getattr(agent, "workspace_root", agent.work_directory))
    ws_id = str(getattr(agent, "workspace_id", "") or "").strip().lower()
    if ws_id == default_workspace_id:
        cache_root = (ws_root / ".cache").resolve()
    else:
        cache_root = (ws_root / ".smartshell" / ".cache").resolve()
    return (
        "\n\n## Runtime Cache Directory Hint\n"
        "- 通用缓存根目录（workspace 级）: "
        f"`{cache_root}`\n"
        "- 若某个脚本支持 `--cache-dir` 参数或其它传递 cache 路径的的参数，则传入此目录。"
        "- 若脚本未声明或不支持 cache 参数，不要强行传参。"
    )


def build_tools_prompt_append(agent: Any) -> str:
    """Build tool catalog text injected into system prompt from external md template."""
    lines: List[str] = [agent.tools_prompt_template.strip(), "", "Available tools:"]
    lines.insert(
        1,
        "当且仅当当前会话尚未注入目标 skill 正文时，先输出："
        "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\"}}；"
        "若该 skill 已注入（例如通过 `/skills/<skill-name>` 显式启用），默认禁止重复调用 request_skill_prompt，直接继续业务步骤；"
        "但当技能正文为分段注入时，可按需调用 "
        "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\",\"section\":<n>}} "
        "加载第 n 段，或用 "
        "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\",\"full\":true}} "
        "加载完整正文。",
    )
    for t in (agent.tool_specs or []):
        fn = (t or {}).get("function", {})
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        if name == "project_context_search" and not agent._project_context_tool_allowed():
            continue
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        arg_keys = ", ".join(sorted(str(k) for k in props.keys())) if props else "-"
        lines.append(f"- {name}: {desc} | args: {arg_keys}")
    return "\n".join(lines)


def build_os_file_ops_prompt_append() -> str:
    """Inject OS-specific shell policy for file operations."""
    if os.name == "nt":
        return (
            "\n\n## File Operation Policy (OS-Specific)\n"
            "- 可通过操作系统命令完成的文件操作（读取、检索、创建、编辑、批量替换）必须使用 `shell` 工具执行。\n"
            "- 命令路由优先级：脚本执行规则 > 文本文件操作规则。若命令目标是执行脚本（如 python/py/node/bash/pwsh 调用脚本文件），必须按脚本执行规则处理。\n"
            '- 当前系统为 Windows：仅当命令目标是文本文件操作（读取、检索、创建、编辑、替换）时，才必须使用 `powershell -ExecutionPolicy Bypass -Command "<command>"` 形式执行；运行脚本不属于文本文件操作。\n'
            "- 禁止使用 `type`、`findstr`、`copy`、`move`、`del`、`cmd /c` 等非该前缀方式处理这些文件操作。\n"
            "- 当需要定位关键词并读取文本附近内容时，必须先检索命中位置，再按行号分段读取附近片段；禁止一次读取整个文件。\n"
            "- 读取文本文件时，单次读取不得超过 100 行；超过时必须拆分为多次分段读取。\n"
            '- 脚本执行时禁止使用多余的 PowerShell 包装。允许示例：`python tools/a.py --x 1`、`py scripts/job.py`；禁止示例：`powershell -ExecutionPolicy Bypass -Command "python tools/a.py --x 1"`。\n'
            "- 在输出命令前必须自检：若命令包含 python/py + 脚本文件，则直接调用 python/py；若命令目标是文本文件操作，则使用 PowerShell 前缀。"
        )
    return (
        "\n\n## File Operation Policy (OS-Specific)\n"
        "- 可通过操作系统命令完成的文件操作（读取、检索、创建、编辑、批量替换）必须使用 `shell` 工具执行。\n"
        "- 当前系统为非 Windows：`shell.command` 使用 POSIX shell 规范（优先 `cat`/`sed`/`awk`/`grep`/`find`，需要修改文件时优先 `sed -i` 或重定向）。\n"
        "- 当需要定位关键词并读取文本附近内容时，必须先检索命中位置，再按行号分段读取附近片段；禁止一次读取整个文件。\n"
        "- 读取文本文件时，单次读取不得超过 100 行；超过时必须拆分为多次分段读取。\n"
    )


def load_tools_prompt_template() -> str:
    """Load tools-related prompt template from external markdown file."""
    path = _src_root() / "prompts" / "tools_prompt.md"
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"⚠️ tools_prompt.md 加载失败: {e}")
        return "## Tool Catalog (prompt-injected)"


def build_local_skill_context_pack(target: Any) -> str:
    """
    Build a compact, structured context pack for one local skill bundle.
    This keeps the model focused on high-signal files before reading long body text.
    """
    try:
        bundle_root = Path(str(getattr(target, "bundle_root", "") or "")).resolve()
    except Exception:
        bundle_root = Path(str(getattr(target, "bundle_root", "") or ""))
    skill_md = bundle_root / "SKILL.md"
    scripts = _list_bundled_script_paths(str(bundle_root), max_files=12)
    refs: List[str] = []
    try:
        refs_dir = bundle_root / "references"
        if refs_dir.is_dir():
            refs = [
                str(p.resolve())
                for p in sorted(refs_dir.glob("*.md"), key=lambda p: p.name.lower())[:8]
            ]
    except Exception:
        refs = []

    body = str(getattr(target, "body", "") or "")
    headings: List[str] = []
    for line in body.splitlines():
        s = str(line).strip()
        if s.startswith("#"):
            headings.append(s)
            if len(headings) >= 10:
                break

    lines: List[str] = [
        "#### Skill Context Pack (compact)",
        f"- skill_id: `{getattr(target, 'skill_id', '')}`",
        f"- bundle_root: `{bundle_root}`",
        f"- skill_md: `{skill_md}`",
        f"- scripts_count: {len(scripts)}",
        f"- references_count: {len(refs)}",
    ]
    if scripts:
        lines.append("- scripts (absolute paths):")
        for p in scripts:
            lines.append(f"  - `{p}`")
    if refs:
        lines.append("- references (absolute paths):")
        for p in refs:
            lines.append(f"  - `{p}`")
    if headings:
        lines.append("- key headings:")
        for h in headings:
            lines.append(f"  - {h}")
    lines.append("- usage_hint: 优先基于上述路径做定点读取/执行，避免无界搜索。")
    return "\n".join(lines)


def default_skill_cache_dir(
    agent: Any,
    skill_id: str,
    default_workspace_id: str,
) -> Path:
    sid = str(skill_id or "").strip().lower() or "skill"
    ws_root = Path(getattr(agent, "workspace_root", agent.work_directory))
    ws_id = str(getattr(agent, "workspace_id", "") or "").strip().lower()
    if ws_id == default_workspace_id:
        base = ws_root / ".cache"
    else:
        base = ws_root / ".smartshell" / ".cache"
    return (base / sid).resolve()


def build_mcp_skill_context_pack(
    server: str,
    skill_id: str,
    rendered_parts: List[str],
) -> str:
    """
    Build a compact context pack for MCP prompt-backed skills.
    """
    char_count = sum(len(str(p or "")) for p in (rendered_parts or []))
    lines = [
        "#### Skill Context Pack (compact)",
        f"- source: `mcp`",
        f"- server: `{server}`",
        f"- skill_id: `{skill_id}`",
        f"- prompt_messages: {len(rendered_parts or [])}",
        f"- rendered_chars: {char_count}",
        "- usage_hint: 先按消息顺序执行首个可落地步骤，再根据结果迭代。",
    ]
    return "\n".join(lines)


def split_skill_body_sections(text: str, max_section_chars: int) -> List[str]:
    """
    Split long SKILL body into semantic sections using markdown headings first.
    Falls back to character windows when headings are not enough.
    """
    body = str(text or "").strip()
    if not body:
        return []
    lines = body.splitlines()
    blocks: List[str] = []
    cur: List[str] = []
    for ln in lines:
        s = str(ln).lstrip()
        if s.startswith("#") and cur:
            blocks.append("\n".join(cur).strip())
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append("\n".join(cur).strip())
    blocks = [b for b in blocks if b.strip()]
    if len(blocks) <= 1:
        chunks: List[str] = []
        start = 0
        while start < len(body):
            end = min(len(body), start + max_section_chars)
            chunks.append(body[start:end].strip())
            start = end
        return [c for c in chunks if c]

    merged: List[str] = []
    acc = ""
    for b in blocks:
        if not acc:
            acc = b
            continue
        if len(acc) + 2 + len(b) <= max_section_chars:
            acc = f"{acc}\n\n{b}"
        else:
            merged.append(acc.strip())
            acc = b
    if acc.strip():
        merged.append(acc.strip())
    return [m for m in merged if m]


def render_skill_section_payload(
    sections: List[str],
    requested_section: Optional[int],
    full: bool,
    initial_sections: int,
) -> Tuple[str, Dict[str, Any]]:
    total = len(sections)
    if total <= 0:
        return "", {"chunked": False, "section": 0, "total": 0, "full": True}
    if full or total <= initial_sections:
        payload = "\n\n".join(sections)
        return payload, {"chunked": False, "section": 1, "total": total, "full": True}

    idx = int(requested_section or 1)
    idx = 1 if idx < 1 else idx
    idx = total if idx > total else idx
    payload = sections[idx - 1]
    hint_lines = [
        "",
        f"[Skill 分段注入] 当前仅注入第 {idx}/{total} 段，以控制 prompt 体积。",
    ]
    if idx < total:
        hint_lines.append(
            "如需下一段，请调用 "
            f'{{"tool":"request_skill_prompt","args":{{"skill_id":"...","section":{idx + 1}}}}}。'
        )
    hint_lines.append(
        '如需完整正文，可调用 {"tool":"request_skill_prompt","args":{"skill_id":"...","full":true}}。'
    )
    return payload + "\n" + "\n".join(hint_lines), {
        "chunked": True,
        "section": idx,
        "total": total,
        "full": False,
    }


def build_single_skill_prompt(
    agent: Any,
    skill_id: str,
    requested_section: Optional[int],
    full: bool,
    long_body_threshold: int,
    initial_sections: int,
    max_section_chars: int,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Build full prompt appendix for one selected skill.

    Resolution order:
    1) Local loaded Agent Skills (`self.skills`)
    2) MCP prompts fallback
    """
    sid = (skill_id or "").strip().lower()
    if not sid:
        return None, {"chunked": False, "section": 0, "total": 0, "full": True}
    target = None
    for s in agent.skills or []:
        if str(getattr(s, "skill_id", "")).strip().lower() == sid:
            target = s
            break
    if target is None:
        # Fallback: treat `skill/...` as MCP prompt id.
        sid_raw = (skill_id or "").strip()
        if not sid_raw:
            return None, {"chunked": False, "section": 0, "total": 0, "full": True}
        mcp = getattr(agent, "mcp_manager", None)
        if mcp is None:
            return None, {"chunked": False, "section": 0, "total": 0, "full": True}
        server_candidates: List[str] = []
        cfg_servers = {}
        try:
            cfg_servers = (
                (mcp.mcp_config or {}).get("mcpServers", {})
                if isinstance(mcp.mcp_config, dict)
                else {}
            )
        except Exception:
            cfg_servers = {}
        if isinstance(cfg_servers, dict):
            for name in cfg_servers.keys():
                n = str(name).strip()
                if not n:
                    continue
                server_candidates.append(n)

        for server in server_candidates:
            srv = str(server).strip()
            if not srv:
                continue
            try:
                # Ensure prompt cache is refreshed at least once for this server.
                mcp.list_prompts(srv, timeout_s=12.0, use_cache=False)
                prompt_obj = mcp.get_prompt(srv, sid_raw, {}, timeout_s=25.0)
                desc = (
                    str(prompt_obj.get("description", "") or "").strip()
                    if isinstance(prompt_obj, dict)
                    else ""
                )
                messages = prompt_obj.get("messages", []) if isinstance(prompt_obj, dict) else []
                rendered_parts: List[str] = []
                if isinstance(messages, list):
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        role = str(msg.get("role", "") or "").strip() or "user"
                        content = msg.get("content")
                        text = ""
                        if isinstance(content, dict):
                            text = str(content.get("text", "") or "").strip()
                        elif isinstance(content, list):
                            chunks: List[str] = []
                            for c in content:
                                if isinstance(c, dict):
                                    t = str(c.get("text", "") or "").strip()
                                    if t:
                                        chunks.append(t)
                            text = "\n\n".join(chunks).strip()
                        elif isinstance(content, str):
                            text = content.strip()
                        if not text:
                            continue
                        rendered_parts.append(f"#### MCP Prompt Message ({role})\n{text}")
                if not rendered_parts and desc:
                    rendered_parts.append(desc)
                if not rendered_parts:
                    continue
                payload_text, meta = render_skill_section_payload(
                    sections=rendered_parts,
                    requested_section=requested_section,
                    full=full,
                    initial_sections=initial_sections,
                )
                lines = [
                    "",
                    "## Agent Skill（按需加载）",
                    f"### MCP Skill Prompt: `{sid_raw}` · server `{srv}`",
                    f"**Description:** {desc or '(no description)'}",
                    "",
                    build_mcp_skill_context_pack(srv, sid_raw, rendered_parts),
                    "",
                    "【优先级】当前请求已显式指定该 skill：若与 AGENTS.md 或通用系统说明冲突，"
                    "按本 skill 正文执行（安全/越权/破坏性硬限制除外）。",
                    "",
                    "以下正文来自 MCP `prompts/get` 返回，请严格按其步骤执行：",
                    "",
                    payload_text,
                    "",
                ]
                return "\n".join(lines), meta
            except Exception:
                continue
        return None, {"chunked": False, "section": 0, "total": 0, "full": True}
    _br = Path(target.bundle_root)
    body = str(getattr(target, "body", "") or "")
    if full or len(body) < long_body_threshold:
        sections = [body]
    else:
        sections = split_skill_body_sections(body, max_section_chars=max_section_chars)
    payload_text, meta = render_skill_section_payload(
        sections=sections,
        requested_section=requested_section,
        full=full,
        initial_sections=initial_sections,
    )
    lines = [
        "",
        "## Agent Skill（按需加载）",
        f"### Skill: `{target.name}` · 目录 `{target.skill_id}`",
        f"**Description:** {target.description}",
        "",
        build_local_skill_context_pack(target),
        "",
        "【优先级】当前请求已显式指定该 skill：若与 AGENTS.md 或通用系统说明冲突，"
        "按本 skill 正文执行（安全/越权/破坏性硬限制除外）。",
        "",
        f"**Skill bundle root (absolute path on this machine):** `{target.bundle_root}`",
        f"**SKILL.md path (same bundle):** `{_br / 'SKILL.md'}`",
        "技能正文中的 `<skill_root>` 即指上文的 **Skill bundle root**。",
        "",
        payload_text,
        "",
    ]
    return "\n".join(lines), meta
