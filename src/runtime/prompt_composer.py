import json
import os
import subprocess
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.app_info import get_app_config_dirname
from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, get_display_language, translate
from ..core.config.skills_loader import _list_bundled_script_paths
from ..tooling.handlers.mcp_handlers import MCP_MANAGEMENT_GATED_TOOLS
from ..tooling.handlers.memory_handlers import MEMORY_TOOLS


def _t(language: Any, key: str, **kwargs: Any) -> str:
    return translate(
        key,
        get_display_language(language) if not isinstance(language, str) else language,
        **kwargs,
    )


def _src_root() -> Path:
    """Return absolute src/ root regardless of current module subdirectory."""
    return Path(__file__).resolve().parent.parent


_SYSTEM_FILE_SEARCH_NEARBY_RULE_KEY = "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}"
_TOOLS_FILE_SEARCH_NEARBY_RULE_KEY = "{{TOOLS_FILE_SEARCH_NEARBY_RULE}}"

_SYSTEM_FILE_SEARCH_NEARBY_RULE_FALLBACK = (
    "first run a search such as `Select-String` or `rg`, then read nearby content by line range; do not read the whole file at once; keep each read under 100 lines."
)
_SYSTEM_FILE_SEARCH_NEARBY_RULE_RG_ONLY = (
    "first run `rg`, then read nearby content by line range; do not read the whole file at once; keep each read under 100 lines."
)
_TOOLS_FILE_SEARCH_NEARBY_RULE_FALLBACK = (
    "first locate matches, then read nearby snippets by line range; do not read the whole file at once; keep each read under 100 lines."
)
_TOOLS_FILE_SEARCH_NEARBY_RULE_RG_ONLY = (
    "first use `rg` to locate matches, then read nearby snippets by line range; do not read the whole file at once; keep each read under 100 lines."
)
_AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"
_AGENTS_FILENAME = "AGENTS.md"
_AGENTS_APPEND_MAX_BYTES = 32 * 1024


def _workspace_bin_dir() -> Path:
    return _src_root().parent / "bin"


def _rg_bin_candidates() -> List[Path]:
    bin_dir = _workspace_bin_dir()
    if os.name == "nt":
        names = ("rg.exe", "rg.cmd", "rg.bat", "rg")
    else:
        names = ("rg",)
    return [bin_dir / n for n in names]


def _is_usable_rg_executable(candidate: Path) -> bool:
    try:
        if not candidate.exists() or not candidate.is_file():
            return False
        if os.name != "nt" and not os.access(str(candidate), os.X_OK):
            return False
        proc = subprocess.run(
            [str(candidate), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
        return int(getattr(proc, "returncode", 1)) == 0
    except KeyboardInterrupt:
        # Treat interrupts as an unusable probe result so cancel flows can exit cleanly.
        return False
    except Exception:
        return False


def _usable_workspace_rg_bin_path() -> str:
    for candidate in _rg_bin_candidates():
        if _is_usable_rg_executable(candidate):
            try:
                return str(candidate.resolve())
            except Exception:
                return str(candidate)
    return ""


def has_usable_workspace_rg_bin() -> bool:
    return bool(_usable_workspace_rg_bin_path())


def _rg_prompt_variables() -> Dict[str, str]:
    if has_usable_workspace_rg_bin():
        return {
            _SYSTEM_FILE_SEARCH_NEARBY_RULE_KEY: _SYSTEM_FILE_SEARCH_NEARBY_RULE_RG_ONLY,
            _TOOLS_FILE_SEARCH_NEARBY_RULE_KEY: _TOOLS_FILE_SEARCH_NEARBY_RULE_RG_ONLY,
        }
    return {
        _SYSTEM_FILE_SEARCH_NEARBY_RULE_KEY: _SYSTEM_FILE_SEARCH_NEARBY_RULE_FALLBACK,
        _TOOLS_FILE_SEARCH_NEARBY_RULE_KEY: _TOOLS_FILE_SEARCH_NEARBY_RULE_FALLBACK,
    }


def render_workspace_prompt_variables(prompt_text: str) -> str:
    text = str(prompt_text or "")
    if not text:
        return text
    for key, value in _rg_prompt_variables().items():
        text = text.replace(key, value)
    return text


def build_mcp_system_append(agent: Any) -> str:
    """Build MCP section appended to system prompt (with redacted env values)."""
    servers = (agent.mcp_config or {}).get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        return "\n\n## MCP Configuration\nNo usable MCP server was detected; `mcp.jsonc` is missing or empty under the config directory."
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
        "## MCP Configuration",
        "MCP servers were loaded from `mcp.jsonc` under the config directory. Before calling a tool, choose the most relevant loaded server.",
        "Only capabilities from loaded servers may be treated as available. Do not describe unloaded servers as available capabilities.",
        "Decision constraint: when loaded cached MCP tools can satisfy the user intent, prefer `mcp_call_tool` ",
        "instead of creating a temporary script or simulating the capability through shell, unless the MCP tool clearly failed and no equivalent MCP tool exists.",
        "Available servers (sensitive env values are redacted; only key names are shown):",
    ]
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            lines.append(f"- {name}: invalid configuration; expected an object")
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
    lines.append(f"Loaded servers: {', '.join(loaded) if loaded else 'none'}")
    lines.append(f"Not loaded servers: {', '.join(not_loaded) if not_loaded else 'none'}")
    lines.append(
        "MCP initialize instructions from connected servers (treat these as active guidance; "
        "when you use a server, follow its instructions while planning and executing the task):"
    )
    try:
        lines.append(agent.mcp_manager.cached_initialize_instructions_for_prompt())
    except Exception:
        lines.append("No cached MCP initialize instructions yet.")
    lines.append("Cached tools (updated after `mcp_list_tools`):")
    try:
        lines.append(agent.mcp_manager.cached_tools_for_prompt())
    except Exception:
        lines.append("No cached MCP tools yet.")
    lines.append("Cached resources (updated after `mcp_list_resources`):")
    try:
        lines.append(agent.mcp_manager.cached_resources_for_prompt())
    except Exception:
        lines.append("No cached MCP resources yet.")
    lines.append("Cached prompts (updated after `mcp_list_prompts`):")
    try:
        lines.append(agent.mcp_manager.cached_prompts_for_prompt())
    except Exception:
        lines.append("No cached MCP prompts yet.")
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
    path = _src_root() / "tools" / "tools.jsonc"
    try:
        raw = path.read_text(encoding="utf-8")
        clean = strip_jsonc_comments(raw)
        parsed = json.loads(clean)
        if not isinstance(parsed, list):
            raise ValueError("tools.jsonc root must be array")
        specs = [x for x in parsed if isinstance(x, dict)]

        if not bool(getattr(agent, "mcp_tools_enabled", False)):
            specs = [
                x
                for x in specs
                if str((x.get("function", {}) or {}).get("name", "")).strip()
                not in MCP_MANAGEMENT_GATED_TOOLS
            ]

        return specs
    except Exception as e:
        print(_t(agent, "prompt_composer.tools_jsonc_load_failed", error=e))
        return []


def build_user_preferences_system_append(agent: Any) -> str:
    """Persistent user preferences injected into system before MCP/tools."""
    try:
        from ..core.state import user_preferences_manager as _upm

        return _upm.build_system_append(Path(agent.config_dir))
    except Exception:
        return ""


def _resolve_prompt_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser()


def _normalized_path_key(path: Path) -> str:
    try:
        raw = str(_resolve_prompt_path(path))
    except Exception:
        raw = str(path)
    return raw.casefold() if os.name == "nt" else raw


def _agents_workspace_anchor(agent: Any) -> Path:
    anchor = getattr(agent, "workspace_root", None) or getattr(agent, "work_directory", None)
    if anchor is None:
        anchor = getattr(agent, "config_dir", Path.cwd())
    return _resolve_prompt_path(Path(anchor))


def _find_git_repo_root(anchor: Path) -> Optional[Path]:
    current = _resolve_prompt_path(anchor)
    for candidate in (current, *current.parents):
        git_marker = candidate / ".git"
        try:
            if git_marker.exists():
                return candidate
        except Exception:
            continue
    return None


def _agents_project_dirs(anchor: Path) -> List[Path]:
    repo_root = _find_git_repo_root(anchor)
    if repo_root is None:
        return [anchor]

    chain: List[Path] = []
    current = repo_root
    while True:
        chain.append(current)
        if _normalized_path_key(current) == _normalized_path_key(anchor):
            break
        try:
            rel_parts = anchor.relative_to(repo_root).parts
        except Exception:
            return [anchor]
        next_index = len(chain)
        if next_index > len(rel_parts):
            break
        current = repo_root.joinpath(*rel_parts[:next_index])
    return chain


def _agents_candidate_dirs(agent: Any) -> Tuple[Path, List[Path]]:
    config_dir = _resolve_prompt_path(Path(agent.config_dir))
    workspace_anchor = _agents_workspace_anchor(agent)
    dirs: List[Path] = []
    seen: set[str] = set()
    for directory in [config_dir, *_agents_project_dirs(workspace_anchor)]:
        key = _normalized_path_key(directory)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(directory)
    return workspace_anchor, dirs


def _agents_candidate_paths(directory: Path) -> List[Path]:
    return [directory / _AGENTS_OVERRIDE_FILENAME, directory / _AGENTS_FILENAME]


def _agents_path_state(path: Path) -> Dict[str, Any]:
    resolved = _resolve_prompt_path(path)
    try:
        exists = resolved.exists()
    except Exception:
        exists = False
    is_file = False
    mtime_ns = None
    size = None
    if exists:
        try:
            is_file = resolved.is_file()
        except Exception:
            is_file = False
        try:
            st = resolved.stat()
            mtime_ns = int(getattr(st, "st_mtime_ns", 0) or 0)
            size = int(getattr(st, "st_size", 0) or 0)
        except Exception:
            pass
    return {
        "path": str(resolved),
        "exists": exists,
        "is_file": is_file,
        "mtime_ns": mtime_ns,
        "size": size,
    }


def _select_agents_file_for_dir(directory: Path) -> Optional[Path]:
    for candidate in _agents_candidate_paths(directory):
        state = _agents_path_state(candidate)
        if state["exists"] and state["is_file"]:
            return _resolve_prompt_path(candidate)
    return None


def _truncate_utf8_text(text: str, max_bytes: int) -> str:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or "")
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _render_agents_sections(
    workspace_anchor: Path,
    candidate_dirs: List[Path],
    selected_files: List[Path],
) -> str:
    sections: List[str] = []
    seen_files: set[str] = set()
    global_dir = candidate_dirs[0] if candidate_dirs else None
    repo_root = candidate_dirs[1] if len(candidate_dirs) > 1 else workspace_anchor
    for resolved in selected_files:
        key = _normalized_path_key(resolved)
        if key in seen_files:
            continue
        seen_files.add(key)
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        scope = (
            "global"
            if global_dir is not None
            and _normalized_path_key(resolved.parent) == _normalized_path_key(global_dir)
            else "project"
        )
        if scope == "global":
            section_title = f"### global \u00b7 {resolved.name}"
        else:
            try:
                rel_dir = resolved.parent.relative_to(repo_root)
                rel_label = "." if not rel_dir.parts else rel_dir.as_posix()
            except Exception:
                rel_label = resolved.parent.name or "."
            section_title = f"### project \u00b7 {rel_label} \u00b7 {resolved.name}"
        sections.append(
            "\n".join(
                [
                    section_title,
                    f"Source: `{resolved}`",
                    content,
                ]
            )
        )
    if not sections:
        return ""
    header = (
        "\n\n## User Custom Prompts (AGENTS.md)\n\n"
        "Priority note: this section injects user-defined prompts. If the current request explicitly selects a skill "
        "(for example `/skills/<skill-name>` or a triggered `request_skill_prompt`) and conflicts with this section, "
        "the explicitly selected skill body takes precedence.\n\n"
    )
    return _truncate_utf8_text(header + "\n\n".join(sections) + "\n", _AGENTS_APPEND_MAX_BYTES)


def _refresh_agents_prompt_cache(agent: Any) -> Dict[str, Any]:
    workspace_anchor, candidate_dirs = _agents_candidate_dirs(agent)
    candidate_paths: List[Path] = []
    path_state: Dict[str, Dict[str, Any]] = {}
    selected_files: List[Path] = []
    for directory in candidate_dirs:
        for candidate in _agents_candidate_paths(directory):
            resolved = _resolve_prompt_path(candidate)
            candidate_paths.append(resolved)
            path_state[_normalized_path_key(resolved)] = _agents_path_state(resolved)
        selected = _select_agents_file_for_dir(directory)
        if selected is not None:
            selected_files.append(selected)
    return {
        "workspace_anchor": str(workspace_anchor),
        "candidate_dirs": [str(p) for p in candidate_dirs],
        "candidate_paths": [str(p) for p in candidate_paths],
        "selected_files": [str(p) for p in selected_files],
        "path_state": path_state,
        "rendered_append": _render_agents_sections(workspace_anchor, candidate_dirs, selected_files),
    }


def _agents_prompt_cache_changed(agent: Any, cache: Dict[str, Any]) -> bool:
    workspace_anchor, candidate_dirs = _agents_candidate_dirs(agent)
    if str(workspace_anchor) != str(cache.get("workspace_anchor") or ""):
        return True
    cached_dirs = [str(x) for x in (cache.get("candidate_dirs") or [])]
    current_dirs = [str(p) for p in candidate_dirs]
    if cached_dirs != current_dirs:
        return True
    cached_state = cache.get("path_state")
    if not isinstance(cached_state, dict):
        return True
    for directory in candidate_dirs:
        for candidate in _agents_candidate_paths(directory):
            resolved = _resolve_prompt_path(candidate)
            key = _normalized_path_key(resolved)
            if cached_state.get(key) != _agents_path_state(resolved):
                return True
    return False


def build_agents_md_system_append(agent: Any) -> str:
    """Inject AGENTS prompt content from config and project directories."""
    cache = getattr(agent, "_agents_prompt_cache", None)
    if not isinstance(cache, dict) or _agents_prompt_cache_changed(agent, cache):
        cache = _refresh_agents_prompt_cache(agent)
        try:
            agent._agents_prompt_cache = cache
        except Exception:
            pass
    return str(cache.get("rendered_append") or "")


def compose_system_prompt_snapshot(agent: Any, include_tools: bool) -> str:
    """Assemble the current model-visible system snapshot."""
    core = (
        agent._base_system_prompt
        + build_agents_md_system_append(agent)
        + build_user_preferences_system_append(agent)
        + build_mcp_system_append(agent)
        + build_runtime_cache_prompt_append(agent, default_workspace_id="default")
        + build_os_file_ops_prompt_append()
    )
    snapshot = core
    if include_tools:
        snapshot = core + "\n" + build_tools_prompt_append(agent)
    return render_workspace_prompt_variables(snapshot)


def build_runtime_cache_prompt_append(agent: Any, default_workspace_id: str) -> str:
    """Provide generic runtime cache-dir hints for all skills/scripts."""
    ws_root = Path(getattr(agent, "workspace_root", agent.work_directory))
    ws_id = str(getattr(agent, "workspace_id", "") or "").strip().lower()
    if ws_id == default_workspace_id:
        cache_root = (ws_root / ".cache").resolve()
    else:
        cache_root = (ws_root / get_app_config_dirname() / ".cache").resolve()
    return (
        "\n\n## Runtime Cache Directory Hint\n"
        "- General cache root directory (workspace-level): "
        f"`{cache_root}`\n"
        "- If a script supports `--cache-dir` or another cache path parameter, pass this directory.\n"
        "- If a script does not declare or support a cache parameter, do not force one."
    )


def build_tools_prompt_append(agent: Any) -> str:
    """Build tool catalog text injected into system prompt from external md template."""
    template = str(getattr(agent, "tools_prompt_template", "") or "").strip()
    template = re.sub(
        r"\n?First-turn output template example:[\s\S]*$",
        "",
        template,
        flags=re.IGNORECASE,
    ).strip()

    mcp_tools_enabled = bool(getattr(agent, "mcp_tools_enabled", False))
    if mcp_tools_enabled:
        # Append the gated MCP-management section only when those tools are actually exposed.
        side_template = str(getattr(agent, "tools_prompt_mcp_management_template", "") or "").strip()
        if side_template:
            template = (template + "\n\n" + side_template).strip()

    memory_enabled = bool(getattr(agent, "memory_enabled", True))
    if memory_enabled:
        # Append the gated experiential-memory section only when memory tools are actually exposed.
        memory_side = str(getattr(agent, "tools_prompt_memory_template", "") or "").strip()
        if memory_side:
            template = (template + "\n\n" + memory_side).strip()

    lines: List[str] = [
        template,
        "",
        _build_tool_call_mode_prompt(),
        "",
        "Available tools:",
    ]
    lines.insert(
        1,
        "Call `request_skill_prompt` through standard tools only when the target skill body has not yet been injected. "
        "If the skill was already injected, for example by `/skills/<skill-name>`, do not repeat `request_skill_prompt`; continue the business steps directly. "
        "If the skill body was chunked, you may call `request_skill_prompt` for a specific section or for the full body as needed.",
    )
    if getattr(agent, "_project_context_tool_allowed", None) and agent._project_context_tool_allowed():
        lines.insert(
            2,
            "For software development tasks (debugging, implementation, refactoring, tests, source browsing, or change-impact analysis), use `project_context_search` as the first retrieval step before shell search or file reads. If the index is empty or stale, refresh it and retry once before falling back.",
        )
    for t in (agent.tool_specs or []):
        fn = (t or {}).get("function", {})
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        if name in MCP_MANAGEMENT_GATED_TOOLS and not mcp_tools_enabled:
            continue
        if name in MEMORY_TOOLS and not memory_enabled:
            continue
        if name == "project_context_search" and not agent._project_context_tool_allowed():
            continue
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        arg_keys = ", ".join(sorted(str(k) for k in props.keys())) if props else "-"
        lines.append(f"- {name}: {desc} | args: {arg_keys}")
    return "\n".join(lines)


def _build_tool_call_mode_prompt() -> str:
    return (
        "## Tool Call Mode: Standard API tool_calls\n"
        "- When more tool work is needed, the assistant message must include the next standard API `tool_calls` entry alongside any visible plan/status content.\n"
        "- When no further tool work is needed, reply in natural language with no tool_calls. The host returns to the command prompt automatically; there is no separate finish/done tool.\n"
        "- The current model must invoke tools through the API-standard `tool_calls` field.\n"
        "- Visible text may contain only user-visible natural-language explanation, step status, or result summary.\n"
        "- Never print any tool-call representation in visible text, including JSON tool objects, XML/tags, markdown code blocks, "
        "`tool`/`args` examples, or any other pseudo tool-call format.\n"
        "- If the runtime detects pseudo tool-call text instead of standard `tool_calls`, you will be asked to retry; resend the same intent using real `tool_calls`."
    )


def build_os_file_ops_prompt_append() -> str:
    """Inject OS-specific shell policy for file operations."""
    search_policy_rule = (
        _TOOLS_FILE_SEARCH_NEARBY_RULE_RG_ONLY
        if has_usable_workspace_rg_bin()
        else _TOOLS_FILE_SEARCH_NEARBY_RULE_FALLBACK
    )
    search_policy_line = (
        "- When locating keywords and reading nearby text, "
        + search_policy_rule
        + "\n"
    )
    if os.name == "nt":
        return (
            "\n\n## File Operation Policy (OS-Specific)\n"
            "- File operations that can be done through OS commands (read, search, create, edit, bulk replace) must use `shell`.\n"
            "- Command routing priority: script execution rules override text-file operation rules. If the target is script execution, such as python/py/node/bash/pwsh running a script file, follow script execution rules.\n"
            '- Current OS is Windows: only text-file operations (read, search, create, edit, replace) must use `powershell -ExecutionPolicy Bypass -Command "<command>"`; running scripts is not a text-file operation.\n'
            "- Do not use `type`, `findstr`, `copy`, `move`, `del`, `cmd /c`, or other non-prefix forms for those file operations.\n"
            + search_policy_line
            + "- Keep each text-file read under 100 lines; split larger reads into multiple ranges.\n"
            + '- Do not wrap script execution in unnecessary PowerShell. Allowed: `python tools/a.py --x 1`, `py scripts/job.py`; forbidden: `powershell -ExecutionPolicy Bypass -Command "python tools/a.py --x 1"`.\n'
            + "- Before issuing a command, self-check: python/py plus script file means direct python/py call; text-file operation means PowerShell prefix."
        )
    return (
        "\n\n## File Operation Policy (OS-Specific)\n"
        "- File operations that can be done through OS commands (read, search, create, edit, bulk replace) must use `shell`.\n"
        "- Current OS is not Windows: `shell.command` uses POSIX shell conventions; prefer `cat`/`sed`/`awk`/`grep`/`find`, and prefer `sed -i` or redirection when editing files.\n"
        + search_policy_line
        + "- Keep each text-file read under 100 lines; split larger reads into multiple ranges.\n"
    )


def load_tools_prompt_template() -> str:
    """Load tools-related prompt template from external markdown file."""
    path = _src_root() / "prompts" / "tools_prompt.md"
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        print(_t(DEFAULT_DISPLAY_LANGUAGE, "prompt_composer.tools_prompt_load_failed", error=e))
        return "## Tool Catalog (prompt-injected)"


def load_tools_prompt_mcp_management_template() -> str:
    """Load the optional MCP-management prompt section.

    This block describes the `mcp_server_info` selection boundaries and
    rendering template. It is appended to the tools prompt only when
    `mcp_tools_enabled` is true; otherwise the gated tools are filtered
    out of the catalog and this section must not be injected.
    """
    path = _src_root() / "prompts" / "tools_prompt_mcp_management.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as e:
        print(_t(DEFAULT_DISPLAY_LANGUAGE, "prompt_composer.tools_prompt_load_failed", error=e))
        return ""


def load_tools_prompt_memory_template() -> str:
    """Load the optional experiential-memory prompt section.

    This block describes how to use the `memory_*` tools. It is appended to
    the tools prompt only when `memory_enabled` is true; otherwise the
    memory tools are filtered out of the catalog and this section must
    not be injected.
    """
    path = _src_root() / "prompts" / "tools_prompt_memory.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as e:
        print(_t(DEFAULT_DISPLAY_LANGUAGE, "prompt_composer.tools_prompt_load_failed", error=e))
        return ""


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
    lines.append("- usage_hint: Prefer targeted reads/execution based on the paths above; avoid unbounded search.")
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
        base = ws_root / get_app_config_dirname() / ".cache"
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
        "- usage_hint: Execute the first actionable step in message order, then iterate based on results.",
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
        f"[Chunked skill injection] Only section {idx}/{total} is currently injected to control prompt size.",
    ]
    if idx < total:
        hint_lines.append(
            f"If the next section is needed, call `request_skill_prompt` through standard tools with `skill_id` and `section={idx + 1}`."
        )
    hint_lines.append(
        "If the full body is needed, call `request_skill_prompt` through standard tools with `skill_id` and `full=true`."
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
                    "## Agent Skill (On-Demand)",
                    f"### MCP Skill Prompt: `{sid_raw}` · server `{srv}`",
                    f"**Description:** {desc or '(no description)'}",
                    "",
                    build_mcp_skill_context_pack(srv, sid_raw, rendered_parts),
                    "",
                    "Priority: the current request explicitly selected this skill. If it conflicts with AGENTS.md or general system instructions, "
                    "follow this skill body except for safety, privilege, and destructive-action hard limits.",
                    "",
                    "The body below comes from MCP `prompts/get`; follow its steps strictly:",
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
        "## Agent Skill (On-Demand)",
        f"### Skill: `{target.name}` · directory `{target.skill_id}`",
        f"**Description:** {target.description}",
        "",
        build_local_skill_context_pack(target),
        "",
        "Priority: the current request explicitly selected this skill. If it conflicts with AGENTS.md or general system instructions, "
        "follow this skill body except for safety, privilege, and destructive-action hard limits.",
        "",
        f"**Skill bundle root (absolute path on this machine):** `{target.bundle_root}`",
        f"**SKILL.md path (same bundle):** `{_br / 'SKILL.md'}`",
        "`<skill_root>` in the skill body refers to the **Skill bundle root** above.",
        "",
        payload_text,
        "",
    ]
    return "\n".join(lines), meta
