from __future__ import annotations

import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname, get_app_name


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


def _default_workspace_id() -> str:
    try:
        from .. import agent as _ssa

        return str(getattr(_ssa, "DEFAULT_WORKSPACE_ID", "default"))
    except Exception:
        return "default"


def split_workspace_args(text: str) -> Tuple[List[str], Optional[str]]:
    try:
        parts = shlex.split(text or "", posix=False)
    except ValueError as e:
        return [], f"Failed to parse arguments: {e}"
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()], None


def parse_workspace_command_args(
    _agent: Any,
    text: str,
    value_flags: Set[str],
    bool_flags: Set[str],
) -> Tuple[List[str], Dict[str, Any], Optional[str]]:
    parts, err = split_workspace_args(text)
    if err:
        return [], {}, _t(_agent, f"Failed to parse arguments: {err}", f"参数解析失败：{err}")
    positionals: List[str] = []
    options: Dict[str, Any] = {}
    i = 0
    while i < len(parts):
        token = parts[i]
        matched_value_flag = None
        for flag in value_flags:
            if token == flag or token.startswith(f"{flag}="):
                matched_value_flag = flag
                break
        if matched_value_flag:
            key = matched_value_flag[2:].replace("-", "_")
            if token.startswith(f"{matched_value_flag}="):
                value = token.split("=", 1)[1].strip()
            else:
                i += 1
                if i >= len(parts):
                    return [], {}, _t(_agent, f"{matched_value_flag} requires a value", f"{matched_value_flag} 需要一个值")
                value = parts[i]
            options[key] = value
        elif token in bool_flags:
            options[token[2:].replace("-", "_")] = True
        elif token.startswith("--"):
            return [], {}, _t(_agent, f"Unknown parameter: {token}", f"未知参数：{token}")
        else:
            positionals.append(token)
        i += 1
    return positionals, options, None


def workspace_usage(agent: Any) -> str:
    config_dirname = get_app_config_dirname()
    app_name = get_app_name()
    return (
        _t(agent, "Usage:", "用法：")
        + "\n"
        + "  /workspace list\n"
        + "  /workspace current\n"
        + "  /workspace create <path> [--name <name>]\n"
        + "  /workspace switch <name|id|path>\n"
        + "  /workspace update <name|id|path> [--name <name>] [--path <path>]\n"
        + "  /workspace rename <name|id|path> <new name>\n"
        + "  /workspace delete <name|id|path> [--remove-files]\n"
        + _t(
            agent,
            f"    --remove-files: Deletes {config_dirname}/ under this custom workspace root, including {app_name} data such as history, temp, skills, memory, and indexes; it will not delete the workspace root or other project files.",
            f"    --remove-files：会删除当前自定义工作区根目录下的 {config_dirname}/，包括 {app_name} 的历史、临时文件、技能、记忆和索引等数据；不会删除工作区根目录或其他项目文件。",
        )
    )


def workspace_subcommand_usage(agent: Any, subcommand: str) -> str:
    usages = {
        "help": _t(agent, "/workspace help", "/workspace help"),
        "current": _t(agent, "/workspace current", "/workspace current"),
        "list": _t(agent, "/workspace list", "/workspace list"),
        "create": _t(agent, "/workspace create <path> [--name <name>]", "/workspace create <path> [--name <名称>]"),
        "switch": _t(agent, "/workspace switch <name|id|path>", "/workspace switch <名称|id|路径>"),
        "update": _t(agent, "/workspace update <name|id|path> [--name <name>] [--path <path>]", "/workspace update <名称|id|路径> [--name <名称>] [--path <路径>]"),
        "rename": _t(agent, "/workspace rename <name|id|path> <new name>", "/workspace rename <名称|id|路径> <新名称>"),
        "delete": _t(agent, "/workspace delete <name|id|path> [--remove-files]", "/workspace delete <名称|id|路径> [--remove-files]"),
    }
    usage = usages.get(str(subcommand or "").strip().lower())
    if usage:
        detail = ""
        if str(subcommand or "").strip().lower() == "delete":
            config_dirname = get_app_config_dirname()
            detail = (
                _t(
                    agent,
                    f"\nNote: --remove-files deletes {config_dirname}/ under this custom workspace root and all of its files and subdirectories; it will not delete the workspace root or other project files.",
                    f"\n注意：--remove-files 会删除当前自定义工作区根目录下的 {config_dirname}/ 及其所有文件和子目录，但不会删除工作区根目录本身或其他项目文件。",
                )
            )
        return f"{_t(agent, 'Usage:', '用法：')} {usage}{detail}"
    return workspace_usage(agent)


def print_workspace_help(_agent: Any) -> None:
    config_dirname = get_app_config_dirname()
    app_name = get_app_name()
    print(workspace_usage(_agent))
    print(_t(_agent, "Notes:", "说明："))
    print(_t(_agent, "  - The default workspace is always named Default, and its data directory remains workspace/ next to config.jsonc", "  - 默认工作区始终名为 Default，其数据目录仍为 config.jsonc 同级的 workspace/"))
    print(_t(_agent, f"  - {app_name} data for custom workspaces is stored under that workspace's {config_dirname}/", f"  - 自定义工作区的 {app_name} 数据存储在对应工作区的 {config_dirname}/ 下"))
    print(_t(_agent, f"  - /workspace delete removes registry entries by default; with --remove-files it deletes that custom workspace's {config_dirname}/ and all contents, but not the workspace root or other project files.", f"  - /workspace delete 默认只删除注册信息；加上 --remove-files 后会删除该自定义工作区的 {config_dirname}/ 及其所有内容，但不会删除工作区根目录或其他项目文件。"))
    print(_t(_agent, "  - Use quotes when a path or name contains spaces", "  - 路径或名称包含空格时请使用引号"))


def print_workspace_current(agent: Any) -> None:
    print(_t(agent, f"Current workspace: {agent.workspace_name} ({agent.workspace_id})", f"当前工作区：{agent.workspace_name} ({agent.workspace_id})"))
    print(_t(agent, f"  root: {agent.workspace_root}", f"  根目录：{agent.workspace_root}"))
    print(_t(agent, f"  storage: {agent.workspace_config_dir}", f"  存储目录：{agent.workspace_config_dir}"))
    print(_t(agent, f"  current directory: {agent.work_directory}", f"  当前目录：{agent.work_directory}"))


def print_workspace_list(agent: Any) -> None:
    default_workspace_id = _default_workspace_id()
    workspaces = agent._workspaces_state.get("workspaces", {})
    if not isinstance(workspaces, dict):
        print(_t(agent, "Workspace configuration not found", "未找到工作区配置"))
        return
    print(_t(agent, "Workspaces:", "工作区："))
    ordered = sorted(
        workspaces.values(),
        key=lambda e: (
            0
            if isinstance(e, dict) and e.get("id") == default_workspace_id
            else 1,
            str(e.get("name") if isinstance(e, dict) else ""),
        ),
    )
    for entry in ordered:
        if not isinstance(entry, dict):
            continue
        marker = (
            "*"
            if str(entry.get("id")) == getattr(agent, "workspace_id", default_workspace_id)
            else " "
        )
        print(_t(agent, f"{marker} {entry.get('name')} ({entry.get('id')})", f"{marker} {entry.get('name')} ({entry.get('id')})"))
        print(_t(agent, f"    root: {agent._workspace_root_path(entry)}", f"    根目录：{agent._workspace_root_path(entry)}"))
        print(_t(agent, f"    storage: {agent._workspace_storage_path(entry)}", f"    存储目录：{agent._workspace_storage_path(entry)}"))
        if entry.get("current_dir"):
            print(_t(agent, f"    current: {entry.get('current_dir')}", f"    当前：{entry.get('current_dir')}"))


def workspace_create_command(agent: Any, arg_text: str) -> str:
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'create')}"
    if len(positionals) != 1:
        return _t(agent, "Usage: /workspace create <path> [--name <name>]", "用法：/workspace create <路径> [--name <名称>]")
    root = agent._workspace_path_from_arg(positionals[0])
    name = str(options.get("name") or root.name or str(root)).strip()
    if not name:
        return _t(agent, "❌ Workspace name cannot be empty", "❌ 工作区名称不能为空")
    if agent._workspace_name_exists(name):
        return _t(agent, f"❌ Workspace name already exists: {name}", f"❌ 工作区名称已存在：{name}")
    existing = agent._workspace_entry_by_root(root)
    if existing:
        return _t(agent, f"❌ This directory is already a workspace: {existing.get('name')} ({existing.get('id')})", f"❌ 该目录已经是工作区：{existing.get('name')} ({existing.get('id')})")
    try:
        root.mkdir(parents=True, exist_ok=True)
        storage = root / get_app_config_dirname()
        storage.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _t(agent, f"❌ Failed to create workspace directory: {e}", f"❌ 创建工作区目录失败：{e}")
    workspace_id = agent._workspace_id_for_path(root)
    base_id = workspace_id
    counter = 2
    workspaces = agent._workspaces_state.setdefault("workspaces", {})
    while workspace_id in workspaces:
        workspace_id = f"{base_id}_{counter}"
        counter += 1
    workspaces[workspace_id] = {
        "id": workspace_id,
        "name": name,
        "kind": "custom",
        "root": str(root),
        "storage": str(storage),
        "current_dir": str(root),
    }
    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()
    return (
        _t(agent, f"✅ Workspace created: {name} ({workspace_id})\n  root: {root}\n  storage: {storage}", f"✅ 工作区已创建：{name} ({workspace_id})\n  根目录：{root}\n  存储目录：{storage}")
    )


def workspace_switch_command(agent: Any, selector: str) -> str:
    default_workspace_id = _default_workspace_id()
    entry = agent._workspace_entry_by_selector(selector)
    if not entry:
        return _t(agent, f"❌ Workspace not found: {selector}", f"❌ 未找到工作区：{selector}")
    if str(entry.get("id")) == getattr(agent, "workspace_id", default_workspace_id):
        return _t(agent, f"ℹ️ Already in workspace: {agent.workspace_name}", f"ℹ️ 已在当前工作区：{agent.workspace_name}")
    agent._save_current_workspace_position()
    agent._apply_workspace_entry(entry, agent.work_directory)
    agent._refresh_workspace_runtime()
    agent._save_current_workspace_position()
    return (
        _t(agent, f"✅ Switched to workspace: {agent.workspace_name}\n  current directory: {agent.work_directory}", f"✅ 已切换到工作区：{agent.workspace_name}\n  当前目录：{agent.work_directory}")
    )


def workspace_update_command(agent: Any, arg_text: str) -> str:
    default_workspace_id = _default_workspace_id()
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name", "--path"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'update')}"
    if len(positionals) != 1 or not options:
        return _t(agent, "Usage: /workspace update <name|id|path> [--name <name>] [--path <path>]", "用法：/workspace update <名称|id|路径> [--name <名称>] [--path <路径>]")
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return _t(agent, f"❌ Workspace not found: {positionals[0]}", f"❌ 未找到工作区：{positionals[0]}")
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return _t(agent, "❌ Default workspace name and path are fixed and cannot be modified", "❌ 默认工作区的名称和路径是固定的，不能修改")
    active_workspace = workspace_id == getattr(agent, "workspace_id", default_workspace_id)
    if active_workspace:
        agent._save_current_workspace_position()

    old_root = agent._workspace_root_path(entry)
    old_storage = agent._workspace_storage_path(entry)
    messages: List[str] = []
    if "name" in options:
        new_name = str(options.get("name") or "").strip()
        if not new_name:
            return _t(agent, "❌ Workspace name cannot be empty", "❌ 工作区名称不能为空")
        if agent._workspace_name_exists(new_name, ignore_id=workspace_id):
            return _t(agent, f"❌ Workspace name already exists: {new_name}", f"❌ 工作区名称已存在：{new_name}")
        entry["name"] = new_name
        messages.append(_t(agent, f"name={new_name}", f"名称={new_name}"))

    if "path" in options:
        new_root = agent._workspace_path_from_arg(str(options.get("path") or ""))
        duplicate = agent._workspace_entry_by_root(new_root, ignore_id=workspace_id)
        if duplicate:
            return _t(agent, f"❌ Target directory is already a workspace: {duplicate.get('name')} ({duplicate.get('id')})", f"❌ 目标目录已经是工作区：{duplicate.get('name')} ({duplicate.get('id')})")
        new_storage = new_root / get_app_config_dirname()
        if active_workspace:
            agent._shutdown_mcp_runtime()
            agent._shutdown_workspace_services(wait=True)
        try:
            new_root.mkdir(parents=True, exist_ok=True)
            if (
                old_storage.exists()
                and agent._path_identity_key(old_storage)
                != agent._path_identity_key(new_storage)
                and not new_storage.exists()
            ):
                new_storage.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_storage), str(new_storage))
                messages.append(_t(agent, "storage=moved", "存储=已移动"))
            else:
                new_storage.mkdir(parents=True, exist_ok=True)
                if old_storage.exists() and agent._path_identity_key(
                    old_storage
                ) != agent._path_identity_key(new_storage):
                    messages.append(_t(agent, "storage=kept-existing-new-location", "存储=保留已有新位置"))
        except Exception as e:
            return _t(agent, f"❌ Failed to update workspace path: {e}", f"❌ 更新工作区路径失败：{e}")
        current_dir = agent._workspace_current_dir_path(entry)
        try:
            rel = current_dir.relative_to(old_root) if current_dir is not None else None
        except Exception:
            rel = None
        if rel is not None:
            candidate = new_root / rel
            entry["current_dir"] = str(candidate if candidate.exists() else new_root)
        else:
            entry["current_dir"] = str(new_root)
        entry["root"] = str(new_root)
        entry["storage"] = str(new_storage)
        messages.append(_t(agent, f"path={new_root}", f"路径={new_root}"))

    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()
    if active_workspace:
        agent._apply_workspace_entry(entry, agent.work_directory)
        agent._refresh_workspace_runtime()
    return _t(agent, f"✅ Workspace updated: {entry.get('name')} ({workspace_id})\n  " + ", ".join(messages), f"✅ 工作区已更新：{entry.get('name')} ({workspace_id})\n  " + "，".join(messages))


def workspace_rename_command(agent: Any, arg_text: str) -> str:
    positionals, _options, err = parse_workspace_command_args(agent, arg_text, set(), set())
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'rename')}"
    if len(positionals) < 2:
        return _t(agent, "Usage: /workspace rename <name|id|path> <new name>", "用法：/workspace rename <名称|id|路径> <新名称>")
    selector = positionals[0]
    new_name = " ".join(positionals[1:]).strip()
    return workspace_update_command(agent, f'"{selector}" --name "{new_name}"')


def workspace_delete_command(agent: Any, arg_text: str) -> str:
    default_workspace_id = _default_workspace_id()
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, set(), {"--remove-files"}
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'delete')}"
    if len(positionals) != 1:
        return _t(agent, f"❌ {workspace_subcommand_usage(agent, 'delete')}", f"❌ {workspace_subcommand_usage(agent, 'delete')}")
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return _t(agent, f"❌ Workspace not found: {positionals[0]}", f"❌ 未找到工作区：{positionals[0]}")
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return _t(agent, "❌ Default workspace cannot be deleted", "❌ 默认工作区不能删除")

    storage = agent._workspace_storage_path(entry)
    remove_files = bool(options.get("remove_files"))
    if remove_files and storage.exists():
        confirm = (
            input(
                _t(agent, f"Confirm deletion of workspace data directory '{storage}'? Only {get_app_config_dirname()} will be deleted, not the workspace root. (y/n): ", f"确认删除工作区数据目录“{storage}”吗？只会删除 {get_app_config_dirname()}，不会删除工作区根目录。 (y/n): ")
            )
            .strip()
            .lower()
        )
        if confirm != "y":
            return _t(agent, "Workspace data directory deletion cancelled", "工作区数据目录删除已取消")

    active_deleted = workspace_id == getattr(agent, "workspace_id", default_workspace_id)
    if active_deleted:
        agent._save_current_workspace_position()
    workspaces = agent._workspaces_state.get("workspaces", {})
    if isinstance(workspaces, dict):
        workspaces.pop(workspace_id, None)
    if active_deleted:
        default_entry = (
            workspaces.get(default_workspace_id)
            if isinstance(workspaces, dict)
            and isinstance(workspaces.get(default_workspace_id), dict)
            else agent._default_workspace_entry()
        )
        if isinstance(workspaces, dict):
            workspaces[default_workspace_id] = default_entry
        agent._apply_workspace_entry(default_entry, agent.work_directory)
        agent._save_current_workspace_position()
        agent._refresh_workspace_runtime()
    else:
        agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()

    removed_data = False
    if remove_files and storage.exists():
        try:
            shutil.rmtree(storage)
            removed_data = True
        except OSError as e:
            return _t(agent, f"⚠️ Workspace was removed from the list, but failed to delete data directory: {e}", f"⚠️ 工作区已从列表中移除，但删除数据目录失败：{e}")
    suffix = _t(agent, f"\n  Deleted data directory: {storage}", f"\n  已删除数据目录：{storage}") if removed_data else ""
    return _t(agent, f"✅ Workspace deleted: {entry.get('name')} ({workspace_id}){suffix}", f"✅ 工作区已删除：{entry.get('name')} ({workspace_id}){suffix}")


def handle_workspace_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = (builtin_line or "").strip()
    if not raw.lower().startswith("workspace"):
        return False
    parts, err = split_workspace_args(raw)
    if err:
        print(_t(agent, f"❌ {err}\n{workspace_usage(agent)}", f"❌ {err}\n{workspace_usage(agent)}"))
        return True
    if not parts or parts[0].lower() != "workspace":
        return False
    if len(parts) == 1:
        print_workspace_help(agent)
        return True

    sub = parts[1].lower()
    match = re.match(r"(?is)^workspace\s+\S+(?:\s+(.*))?$", raw)
    arg_text = (match.group(1) if match else "") or ""

    if sub == "help":
        if arg_text.strip():
            print(_t(agent, f"❌ {workspace_subcommand_usage(agent, 'help')}", f"❌ {workspace_subcommand_usage(agent, 'help')}"))
        else:
            print_workspace_help(agent)
        return True
    if sub == "current":
        if arg_text.strip():
            print(_t(agent, f"❌ {workspace_subcommand_usage(agent, 'current')}", f"❌ {workspace_subcommand_usage(agent, 'current')}"))
        else:
            print_workspace_current(agent)
        return True
    if sub == "list":
        if arg_text.strip():
            print(_t(agent, f"❌ {workspace_subcommand_usage(agent, 'list')}", f"❌ {workspace_subcommand_usage(agent, 'list')}"))
        else:
            print_workspace_list(agent)
        return True
    if sub == "create":
        print(workspace_create_command(agent, arg_text.strip()))
        return True
    if sub == "switch":
        if not arg_text.strip():
            print(_t(agent, f"❌ {workspace_subcommand_usage(agent, 'switch')}", f"❌ {workspace_subcommand_usage(agent, 'switch')}"))
        else:
            print(workspace_switch_command(agent, arg_text.strip()))
        return True
    if sub == "update":
        print(workspace_update_command(agent, arg_text.strip()))
        return True
    if sub == "rename":
        print(workspace_rename_command(agent, arg_text.strip()))
        return True
    if sub == "delete":
        print(workspace_delete_command(agent, arg_text.strip()))
        return True

    print(_t(agent, f"❌ Invalid workspace subcommand: {parts[1]}\n{workspace_usage(agent)}", f"❌ 无效的工作区子命令：{parts[1]}\n{workspace_usage(agent)}"))
    return True
