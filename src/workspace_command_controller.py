from __future__ import annotations

import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple


def _default_workspace_id() -> str:
    try:
        from . import smart_shell_agent as _ssa

        return str(getattr(_ssa, "DEFAULT_WORKSPACE_ID", "default"))
    except Exception:
        return "default"


def split_workspace_args(text: str) -> Tuple[List[str], Optional[str]]:
    try:
        parts = shlex.split(text or "", posix=False)
    except ValueError as e:
        return [], f"参数解析失败: {e}"
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()], None


def parse_workspace_command_args(
    _agent: Any,
    text: str,
    value_flags: Set[str],
    bool_flags: Set[str],
) -> Tuple[List[str], Dict[str, Any], Optional[str]]:
    parts, err = split_workspace_args(text)
    if err:
        return [], {}, err
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
                    return [], {}, f"{matched_value_flag} 需要一个值"
                value = parts[i]
            options[key] = value
        elif token in bool_flags:
            options[token[2:].replace("-", "_")] = True
        elif token.startswith("--"):
            return [], {}, f"未知参数: {token}"
        else:
            positionals.append(token)
        i += 1
    return positionals, options, None


def workspace_usage() -> str:
    return (
        "用法:\n"
        "  /workspace list\n"
        "  /workspace current\n"
        "  /workspace create <path> [--name <name>]\n"
        "  /workspace switch <name|id|path>\n"
        "  /workspace update <name|id|path> [--name <name>] [--path <path>]\n"
        "  /workspace rename <name|id|path> <new name>\n"
        "  /workspace delete <name|id|path> [--remove-files]\n"
        "    --remove-files: 删除该自定义 workspace 根目录下的 .smartshell/，"
        "包括 history、temp、skills、knowledge、knowledge_db 等 Smart Shell 数据；"
        "不会删除 workspace 根目录或其它项目文件。"
    )


def workspace_subcommand_usage(_agent: Any, subcommand: str) -> str:
    usages = {
        "help": "/workspace help",
        "current": "/workspace current",
        "list": "/workspace list",
        "create": "/workspace create <path> [--name <name>]",
        "switch": "/workspace switch <name|id|path>",
        "update": "/workspace update <name|id|path> [--name <name>] [--path <path>]",
        "rename": "/workspace rename <name|id|path> <new name>",
        "delete": "/workspace delete <name|id|path> [--remove-files]",
    }
    usage = usages.get(str(subcommand or "").strip().lower())
    if usage:
        detail = ""
        if str(subcommand or "").strip().lower() == "delete":
            detail = (
                "\n说明: --remove-files 会删除该自定义 workspace 根目录下的 .smartshell/ "
                "及其所有文件和子目录；不会删除 workspace 根目录或其它项目文件。"
            )
        return f"用法: {usage}{detail}"
    return workspace_usage()


def print_workspace_help(_agent: Any) -> None:
    print(workspace_usage())
    print("说明:")
    print("  - 默认 workspace 固定名为 Default，数据目录仍为 config.json 同级的 workspace/")
    print("  - 自定义 workspace 的 Smart Shell 数据保存在该目录下的 .smartshell/")
    print("  - /workspace delete 默认只移除登记；带 --remove-files 时会删除该自定义 workspace 的 .smartshell/ 及其全部内容，不会删除 workspace 根目录或其它项目文件。")
    print("  - 路径或名称包含空格时请使用引号")


def print_workspace_current(agent: Any) -> None:
    print(f"当前 workspace: {agent.workspace_name} ({agent.workspace_id})")
    print(f"  root: {agent.workspace_root}")
    print(f"  storage: {agent.ai_workspace_dir}")
    print(f"  current directory: {agent.work_directory}")


def print_workspace_list(agent: Any) -> None:
    default_workspace_id = _default_workspace_id()
    workspaces = agent._workspaces_state.get("workspaces", {})
    if not isinstance(workspaces, dict):
        print("未找到 workspace 配置")
        return
    print("Workspaces:")
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
        print(f"{marker} {entry.get('name')} ({entry.get('id')})")
        print(f"    root: {agent._workspace_root_path(entry)}")
        print(f"    storage: {agent._workspace_storage_path(entry)}")
        if entry.get("current_dir"):
            print(f"    current: {entry.get('current_dir')}")


def workspace_create_command(agent: Any, arg_text: str) -> str:
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'create')}"
    if len(positionals) != 1:
        return "用法: /workspace create <path> [--name <name>]"
    root = agent._workspace_path_from_arg(positionals[0])
    name = str(options.get("name") or root.name or str(root)).strip()
    if not name:
        return "❌ workspace 名称不能为空"
    if agent._workspace_name_exists(name):
        return f"❌ workspace 名称已存在: {name}"
    existing = agent._workspace_entry_by_root(root)
    if existing:
        return f"❌ 该目录已经是 workspace: {existing.get('name')} ({existing.get('id')})"
    try:
        root.mkdir(parents=True, exist_ok=True)
        storage = root / ".smartshell"
        storage.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"❌ 创建 workspace 目录失败: {e}"
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
        f"✅ 已创建 workspace: {name} ({workspace_id})\n"
        f"  root: {root}\n"
        f"  storage: {storage}"
    )


def workspace_switch_command(agent: Any, selector: str) -> str:
    default_workspace_id = _default_workspace_id()
    entry = agent._workspace_entry_by_selector(selector)
    if not entry:
        return f"❌ 未找到 workspace: {selector}"
    if str(entry.get("id")) == getattr(agent, "workspace_id", default_workspace_id):
        return f"ℹ️ 已经在 workspace: {agent.workspace_name}"
    agent._save_current_workspace_position()
    agent._apply_workspace_entry(entry, agent.work_directory)
    agent._refresh_workspace_runtime()
    return (
        f"✅ 已切换到 workspace: {agent.workspace_name}\n"
        f"  current directory: {agent.work_directory}"
    )


def workspace_update_command(agent: Any, arg_text: str) -> str:
    default_workspace_id = _default_workspace_id()
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name", "--path"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'update')}"
    if len(positionals) != 1 or not options:
        return "用法: /workspace update <name|id|path> [--name <name>] [--path <path>]"
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return f"❌ 未找到 workspace: {positionals[0]}"
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return "❌ 默认 workspace 的名称和目录固定，不能修改"
    active_workspace = workspace_id == getattr(agent, "workspace_id", default_workspace_id)
    if active_workspace:
        agent._save_current_workspace_position()

    old_root = agent._workspace_root_path(entry)
    old_storage = agent._workspace_storage_path(entry)
    messages: List[str] = []
    if "name" in options:
        new_name = str(options.get("name") or "").strip()
        if not new_name:
            return "❌ workspace 名称不能为空"
        if agent._workspace_name_exists(new_name, ignore_id=workspace_id):
            return f"❌ workspace 名称已存在: {new_name}"
        entry["name"] = new_name
        messages.append(f"name={new_name}")

    if "path" in options:
        new_root = agent._workspace_path_from_arg(str(options.get("path") or ""))
        duplicate = agent._workspace_entry_by_root(new_root, ignore_id=workspace_id)
        if duplicate:
            return f"❌ 目标目录已经是 workspace: {duplicate.get('name')} ({duplicate.get('id')})"
        new_storage = new_root / ".smartshell"
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
                messages.append("storage=moved")
            else:
                new_storage.mkdir(parents=True, exist_ok=True)
                if old_storage.exists() and agent._path_identity_key(
                    old_storage
                ) != agent._path_identity_key(new_storage):
                    messages.append("storage=kept-existing-new-location")
        except Exception as e:
            return f"❌ 修改 workspace 目录失败: {e}"
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
        messages.append(f"path={new_root}")

    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()
    if active_workspace:
        agent._apply_workspace_entry(entry, agent.work_directory)
        agent._refresh_workspace_runtime()
    return f"✅ 已修改 workspace: {entry.get('name')} ({workspace_id})\n  " + ", ".join(
        messages
    )


def workspace_rename_command(agent: Any, arg_text: str) -> str:
    positionals, _options, err = parse_workspace_command_args(agent, arg_text, set(), set())
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'rename')}"
    if len(positionals) < 2:
        return "用法: /workspace rename <name|id|path> <new name>"
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
        return f"❌ {workspace_subcommand_usage(agent, 'delete')}"
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return f"❌ 未找到 workspace: {positionals[0]}"
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return "❌ 默认 workspace 不能删除"

    storage = agent._workspace_storage_path(entry)
    remove_files = bool(options.get("remove_files"))
    if remove_files and storage.exists():
        confirm = (
            input(
                f"确认删除 workspace 数据目录 '{storage}'？只会删除 .smartshell，不会删除 workspace 根目录。(y/n): "
            )
            .strip()
            .lower()
        )
        if confirm != "y":
            return "已取消删除 workspace 数据目录"

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
            return f"⚠️ workspace 已从列表删除，但删除数据目录失败: {e}"
    suffix = f"\n  已删除数据目录: {storage}" if removed_data else ""
    return f"✅ 已删除 workspace: {entry.get('name')} ({workspace_id}){suffix}"


def handle_workspace_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = (builtin_line or "").strip()
    if not raw.lower().startswith("workspace"):
        return False
    parts, err = split_workspace_args(raw)
    if err:
        print(f"❌ {err}\n{workspace_usage()}")
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
            print(f"❌ {workspace_subcommand_usage(agent, 'help')}")
        else:
            print_workspace_help(agent)
        return True
    if sub == "current":
        if arg_text.strip():
            print(f"❌ {workspace_subcommand_usage(agent, 'current')}")
        else:
            print_workspace_current(agent)
        return True
    if sub == "list":
        if arg_text.strip():
            print(f"❌ {workspace_subcommand_usage(agent, 'list')}")
        else:
            print_workspace_list(agent)
        return True
    if sub == "create":
        print(workspace_create_command(agent, arg_text.strip()))
        return True
    if sub == "switch":
        if not arg_text.strip():
            print(f"❌ {workspace_subcommand_usage(agent, 'switch')}")
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

    print(f"❌ 无效 workspace 子命令: {parts[1]}\n{workspace_usage()}")
    return True
