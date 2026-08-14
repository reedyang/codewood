from __future__ import annotations

import logging
import re
import shlex
import shutil
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname, get_app_logger_root, get_app_name


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
        archived = bool(entry.get("archived", False))
        name_shown = (
            f"{entry.get('name')} (archived)" if archived else entry.get("name")
        )
        print(_t(agent, "workspace.list.item", marker=marker, name=name_shown, workspace_id=entry.get("id")))
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
    # ``storage`` and ``current_dir`` are derived by rule at use time, so only
    # the stable identity (id/name/kind/root) is persisted here.
    workspaces[workspace_id] = {
        "id": workspace_id,
        "name": name,
        "kind": "custom",
        "root": str(root),
    }
    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()

    # Switch immediately to the newly created workspace.
    # Don't auto-create a default chat — the GUI will enter draft mode
    # (empty composer) when there are no chats, while the TUI will create
    # one via the runtime loop on first input.
    agent._save_current_workspace_position()
    agent._apply_workspace_entry(workspaces[workspace_id], agent.work_directory)
    agent._refresh_workspace_runtime(create_default_chat=False)
    # Post-apply: globals point at the new workspace but the session still
    # carries the previous chat. Save position metadata only (see
    # ``workspace_switch_command``).
    agent._save_current_workspace_position(sync_messages=False)

    return (
        _t(agent, "workspace.create.success", name=name, workspace_id=workspace_id, root=root, storage=storage)
    )


def workspace_switch_command(
    agent: Any,
    selector: str,
    *,
    create_default_chat: bool = True,
    lazy_records: bool = False,
) -> str:
    default_workspace_id = _default_workspace_id()
    entry = agent._workspace_entry_by_selector(selector)
    if not entry:
        return _t(agent, "workspace.not_found_error", selector=selector)
    if str(entry.get("id")) == getattr(agent, "workspace_id", default_workspace_id):
        return _t(agent, "workspace.switch.already_in_workspace", workspace_name=agent.workspace_name)
    logger = logging.getLogger(f"{get_app_logger_root()}.workspace_switch")
    prev_ws = str(getattr(agent, "workspace_id", "") or "")
    prev_root = str(getattr(agent, "workspace_root", "") or "")
    target_ws = str(entry.get("id") or "")
    target_root = str(entry.get("root") or "")
    logger.debug(
        "workspace_switch start: prev_ws=%s prev_root=%s -> target_ws=%s target_root=%s",
        prev_ws, prev_root, target_ws, target_root,
    )
    import time as _time

    _t0 = _time.perf_counter()
    agent._save_current_workspace_position()
    _t1 = _time.perf_counter()
    logger.debug("ws-switch-timing save_position=%.3fs", _t1 - _t0)
    agent._apply_workspace_entry(entry, agent.work_directory)
    _t2 = _time.perf_counter()
    logger.debug("ws-switch-timing apply_entry=%.3fs", _t2 - _t1)
    refresh = getattr(agent, "_refresh_workspace_runtime", None)
    if refresh is None:
        raise AttributeError("agent._refresh_workspace_runtime is required")
    try:
        import inspect

        sig = inspect.signature(refresh)
        if "lazy_records" in sig.parameters:
            refresh(
                create_default_chat=create_default_chat, lazy_records=lazy_records
            )
        else:
            refresh(create_default_chat=create_default_chat)
    except (TypeError, ValueError):
        refresh(create_default_chat=create_default_chat)
    _t3 = _time.perf_counter()
    logger.debug("ws-switch-timing refresh_runtime=%.3fs", _t3 - _t2)
    # Globals now point at the target workspace, but the session still carries
    # the previous chat's id/history (its active chat is bound later by
    # ``_activate_chat``). Persist only the position metadata here; syncing
    # messages would duplicate the previous chat's history into a same-id chat
    # of the target workspace.
    agent._save_current_workspace_position(sync_messages=False)
    _t4 = _time.perf_counter()
    logger.debug("ws-switch-timing save_position_no_sync=%.3fs total=%.3fs", _t4 - _t3, _t4 - _t0)
    # Keep the sandbox usable in the newly activated workspace: a brand-new
    # root has no capability-SID grants, so sandboxed shell commands would be
    # unable to write it. Best-effort, no elevation needed; no-op when the
    # sandbox is not provisioned or the level is full_access.
    try:
        from ..core.sandbox import refresh_workspace_acls

        # ACL application shells out to ``powershell.exe`` (0.5-1.5s per
        # invocation) and is idempotent, so it must never block the workspace
        # switch. Run it on a background thread; the sandbox self-heals by
        # the time the user issues the first sandboxed command.
        import threading

        threading.Thread(
            target=refresh_workspace_acls,
            args=(agent, target_root),
            name=f"{get_app_logger_root()}-workspace-acl-{target_ws}",
            daemon=True,
        ).start()
    except Exception:
        pass
    logger.debug(
        "workspace_switch done: target_ws=%s target_root=%s",
        target_ws, target_root,
    )
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
        # Only ``root`` is persisted; storage is moved on disk above and then
        # re-derived from the new root, and the working directory follows the
        # by-rule fallback when the workspace is applied.
        entry.pop("current_dir", None)
        entry.pop("storage", None)
        entry["root"] = str(new_root)
        messages.append(_t(agent, "workspace.update.message.path", path=new_root))

    agent._save_workspace_state()
    agent._refresh_input_handler_skill_completions()
    if active_workspace:
        agent._apply_workspace_entry(entry, agent.work_directory)
        agent._refresh_workspace_runtime()
        # The workspace root may have moved; re-grant the sandbox capability
        # SIDs on the new root so sandboxed shell commands stay writable.
        try:
            from ..core.sandbox import refresh_workspace_acls

            refresh_workspace_acls(agent, str(new_root))
        except Exception:
            pass
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
    # The workspace is NOT removed from the registry: it is flagged as
    # archived so its chat data stays in the global chats directory and
    # remains reachable — and deletable — from the 设置/已归档 settings page.
    # ``--remove-files`` still deletes the workspace's own config/cache
    # directory (``<root>/<config dirname>``), which no longer contains any
    # chat records.
    workspaces = agent._workspaces_state.get("workspaces", {})
    if isinstance(workspaces, dict):
        if workspace_id in workspaces:
            workspaces[workspace_id]["archived"] = True
    from ..managers.chat_state_manager import archive_workspace_chats

    archive_workspace_chats(workspace_id, agent)
    # Revoke the sandbox users/group/capability SIDs' ACLs on the forgotten
    # workspace tree (best-effort, never raises). This walks the whole tree
    # and can be slow on large projects, so it runs in the background AFTER
    # the registry entry is gone — deleting must not block on ACL cleanup.
    deleted_root = str(entry.get("root") or "")
    if deleted_root:
        try:
            from ..core.sandbox import cleanup_workspace_acls

            def _cleanup_in_background() -> None:
                try:
                    cleanup_workspace_acls(agent, deleted_root)
                except Exception:
                    pass

            threading.Thread(target=_cleanup_in_background, daemon=True).start()
        except Exception:
            pass
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
        # Post-apply: session still carries the deleted workspace's chat; save
        # position metadata only (see ``workspace_switch_command``).
        agent._save_current_workspace_position(sync_messages=False)
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
