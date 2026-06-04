from __future__ import annotations

import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname, get_app_name


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


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
        return [], {}, _t(_agent, "workspace.args.parse_failed", error=err)
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
                    return [], {}, _t(_agent, "workspace.args.flag_requires_value", flag=matched_value_flag)
                value = parts[i]
            options[key] = value
        elif token in bool_flags:
            options[token[2:].replace("-", "_")] = True
        elif token.startswith("--"):
            return [], {}, _t(_agent, "workspace.args.unknown_parameter", token=token)
        else:
            positionals.append(token)
        i += 1
    return positionals, options, None


def workspace_usage(agent: Any) -> str:
    config_dirname = get_app_config_dirname()
    app_name = get_app_name()
    return (
        _t(agent, "common.usage")
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
            "workspace.usage.remove_files_detail",
            config_dirname=config_dirname,
            app_name=app_name,
        )
    )


def workspace_subcommand_usage(agent: Any, subcommand: str) -> str:
    usages = {
        "help": _t(agent, "workspace.usage.help"),
        "current": _t(agent, "workspace.usage.current"),
        "list": _t(agent, "workspace.usage.list"),
        "create": _t(agent, "workspace.usage.create"),
        "switch": _t(agent, "workspace.usage.switch"),
        "update": _t(agent, "workspace.usage.update"),
        "rename": _t(agent, "workspace.usage.rename"),
        "delete": _t(agent, "workspace.usage.delete"),
    }
    usage = usages.get(str(subcommand or "").strip().lower())
    if usage:
        detail = ""
        if str(subcommand or "").strip().lower() == "delete":
            config_dirname = get_app_config_dirname()
            detail = (
                _t(agent, "workspace.usage.note_delete_remove_files", config_dirname=config_dirname)
            )
        return f"{_t(agent, 'common.usage')} {usage}{detail}"
    return workspace_usage(agent)


def print_workspace_help(_agent: Any) -> None:
    config_dirname = get_app_config_dirname()
    app_name = get_app_name()
    print(workspace_usage(_agent))
    print(_t(_agent, "common.notes"))
    print(_t(_agent, "workspace.help.note.default_workspace"))
    print(_t(_agent, "workspace.help.note.custom_storage", app_name=app_name, config_dirname=config_dirname))
    print(_t(_agent, "workspace.help.note.delete_behavior", config_dirname=config_dirname))
    print(_t(_agent, "workspace.help.note.quote_paths"))


def print_workspace_current(agent: Any) -> None:
    print(_t(agent, "workspace.current.header", workspace_name=agent.workspace_name, workspace_id=agent.workspace_id))
    print(_t(agent, "workspace.current.root", root=agent.workspace_root))
    print(_t(agent, "workspace.current.storage", storage=agent.workspace_config_dir))
    print(_t(agent, "workspace.current.current_directory", work_directory=agent.work_directory))


def print_workspace_list(agent: Any) -> None:
    default_workspace_id = _default_workspace_id()
    workspaces = agent._workspaces_state.get("workspaces", {})
    if not isinstance(workspaces, dict):
        print(_t(agent, "workspace.config_not_found"))
        return
    print(_t(agent, "workspace.list.header"))
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
        print(_t(agent, "workspace.list.item", marker=marker, name=entry.get("name"), workspace_id=entry.get("id")))
        print(_t(agent, "workspace.list.root", root=agent._workspace_root_path(entry)))
        print(_t(agent, "workspace.list.storage", storage=agent._workspace_storage_path(entry)))
        if entry.get("current_dir"):
            print(_t(agent, "workspace.list.current", current_dir=entry.get("current_dir")))


def workspace_create_command(agent: Any, arg_text: str) -> str:
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'create')}"
    if len(positionals) != 1:
        return _t(agent, "workspace.usage.create")
    root = agent._workspace_path_from_arg(positionals[0])
    name = str(options.get("name") or root.name or str(root)).strip()
    if not name:
        return _t(agent, "workspace.name_empty_error")
    if agent._workspace_name_exists(name):
        return _t(agent, "workspace.name_exists_error", name=name)
    existing = agent._workspace_entry_by_root(root)
    if existing:
        return _t(agent, "workspace.create.directory_already_workspace", name=existing.get("name"), workspace_id=existing.get("id"))
    try:
        root.mkdir(parents=True, exist_ok=True)
        storage = root / get_app_config_dirname()
        storage.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _t(agent, "workspace.create.failed_directory", error=e)
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

    # Switch immediately to the newly created workspace.
    agent._save_current_workspace_position()
    agent._apply_workspace_entry(workspaces[workspace_id], agent.work_directory)
    agent._refresh_workspace_runtime()
    agent._save_current_workspace_position()

    return (
        _t(agent, "workspace.create.success", name=name, workspace_id=workspace_id, root=root, storage=storage)
    )


def workspace_switch_command(agent: Any, selector: str) -> str:
    default_workspace_id = _default_workspace_id()
    entry = agent._workspace_entry_by_selector(selector)
    if not entry:
        return _t(agent, "workspace.not_found_error", selector=selector)
    if str(entry.get("id")) == getattr(agent, "workspace_id", default_workspace_id):
        return _t(agent, "workspace.switch.already_in_workspace", workspace_name=agent.workspace_name)
    agent._save_current_workspace_position()
    agent._apply_workspace_entry(entry, agent.work_directory)
    agent._refresh_workspace_runtime()
    agent._save_current_workspace_position()
    return (
        _t(agent, "workspace.switch.success", workspace_name=agent.workspace_name, work_directory=agent.work_directory)
    )


def workspace_update_command(agent: Any, arg_text: str) -> str:
    default_workspace_id = _default_workspace_id()
    positionals, options, err = parse_workspace_command_args(
        agent, arg_text, {"--name", "--path"}, set()
    )
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'update')}"
    if len(positionals) != 1 or not options:
        return _t(agent, "workspace.usage.update")
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return _t(agent, "workspace.not_found_error", selector=positionals[0])
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return _t(agent, "workspace.update.default_workspace_fixed")
    active_workspace = workspace_id == getattr(agent, "workspace_id", default_workspace_id)
    if active_workspace:
        agent._save_current_workspace_position()

    old_root = agent._workspace_root_path(entry)
    old_storage = agent._workspace_storage_path(entry)
    messages: List[str] = []
    if "name" in options:
        new_name = str(options.get("name") or "").strip()
        if not new_name:
            return _t(agent, "workspace.name_empty_error")
        if agent._workspace_name_exists(new_name, ignore_id=workspace_id):
            return _t(agent, "workspace.name_exists_error", name=new_name)
        entry["name"] = new_name
        messages.append(_t(agent, "workspace.update.message.name", name=new_name))

    if "path" in options:
        new_root = agent._workspace_path_from_arg(str(options.get("path") or ""))
        duplicate = agent._workspace_entry_by_root(new_root, ignore_id=workspace_id)
        if duplicate:
            return _t(agent, "workspace.update.target_directory_already_workspace", name=duplicate.get("name"), workspace_id=duplicate.get("id"))
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
                messages.append(_t(agent, "workspace.update.message.storage_moved"))
            else:
                new_storage.mkdir(parents=True, exist_ok=True)
                if old_storage.exists() and agent._path_identity_key(
                    old_storage
                ) != agent._path_identity_key(new_storage):
                    messages.append(_t(agent, "workspace.update.message.storage_kept_existing_new_location"))
        except Exception as e:
            return _t(agent, "workspace.update.failed_path", error=e)
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
        messages.append(_t(agent, "workspace.update.message.path", path=new_root))

    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()
    if active_workspace:
        agent._apply_workspace_entry(entry, agent.work_directory)
        agent._refresh_workspace_runtime()
    details = ", ".join(messages) if messages else ""
    return _t(agent, "workspace.update.success", name=entry.get("name"), workspace_id=workspace_id, details=details)


def workspace_rename_command(agent: Any, arg_text: str) -> str:
    positionals, _options, err = parse_workspace_command_args(agent, arg_text, set(), set())
    if err:
        return f"❌ {err}\n{workspace_subcommand_usage(agent, 'rename')}"
    if len(positionals) < 2:
        return _t(agent, "workspace.usage.rename")
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
        return _t(agent, "workspace.delete.usage_error", usage=workspace_subcommand_usage(agent, "delete"))
    entry = agent._workspace_entry_by_selector(positionals[0])
    if not entry:
        return _t(agent, "workspace.not_found_error", selector=positionals[0])
    workspace_id = str(entry.get("id") or "")
    if workspace_id == default_workspace_id:
        return _t(agent, "workspace.delete.default_workspace_forbidden")

    storage = agent._workspace_storage_path(entry)
    remove_files = bool(options.get("remove_files"))
    if remove_files and storage.exists():
        confirm = (
            input(
                _t(agent, "workspace.delete.confirm_remove_data", storage=storage, config_dirname=get_app_config_dirname())
            )
            .strip()
            .lower()
        )
        if confirm != "y":
            return _t(agent, "workspace.delete.remove_data_cancelled")

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
            return _t(agent, "workspace.delete.remove_data_failed_after_registry", error=e)
    suffix = _t(agent, "workspace.delete.deleted_data_directory", storage=storage) if removed_data else ""
    return _t(agent, "workspace.delete.success", name=entry.get("name"), workspace_id=workspace_id, suffix=suffix)


def handle_workspace_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = (builtin_line or "").strip()
    if not raw.lower().startswith("workspace"):
        return False
    parts, err = split_workspace_args(raw)
    if err:
        print(_t(agent, "workspace.command_error_with_usage", error=err, usage=workspace_usage(agent)))
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
            print(_t(agent, "workspace.subcommand_usage_error", usage=workspace_subcommand_usage(agent, "help")))
        else:
            print_workspace_help(agent)
        return True
    if sub == "current":
        if arg_text.strip():
            print(_t(agent, "workspace.subcommand_usage_error", usage=workspace_subcommand_usage(agent, "current")))
        else:
            print_workspace_current(agent)
        return True
    if sub == "list":
        if arg_text.strip():
            print(_t(agent, "workspace.subcommand_usage_error", usage=workspace_subcommand_usage(agent, "list")))
        else:
            print_workspace_list(agent)
        return True
    if sub == "create":
        print(workspace_create_command(agent, arg_text.strip()))
        return True
    if sub == "switch":
        if not arg_text.strip():
            print(_t(agent, "workspace.subcommand_usage_error", usage=workspace_subcommand_usage(agent, "switch")))
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

    print(_t(agent, "workspace.subcommand_invalid_with_usage", subcommand=parts[1], usage=workspace_usage(agent)))
    return True
