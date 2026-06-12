from __future__ import annotations

import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from typing import Any


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def chat_usage(agent: Any) -> str:
    return (
        f"{_t(agent, 'common.usage')}\n"
        f"  /chat list\n"
        f"  /chat current\n"
        f"  /chat reload\n"
        f"  /chat new [name]\n"
        f"  /chat switch <index|id|name>\n"
        f"  /chat rename <index|id|name> <new name>\n"
        f"  /chat edit <index>\n"
        f"  /chat delete <index|id|name>\n"
        f"  /chat delete all\n"
    )


def print_chat_list(agent: Any) -> None:
    chats = agent._chat_entries()
    if not chats:
        print(_t(agent, "chat.none_in_workspace"))
        return
    print(_t(agent, "chat.list_header", workspace_name=agent.workspace_name))
    for i, c in enumerate(chats, start=1):
        marker = "*" if str(c.get("id") or "") == agent.active_chat_id else " "
        name = str(c.get("name") or _t(agent, "chat.new.default_name"))
        cnt = len(c.get("messages") or [])
        print(_t(agent, "chat.list_item", marker=marker, index=i, name=name, count=cnt))


def _clear_terminal_screen() -> None:
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def _print_startup_overview_safe(agent: Any) -> None:
    try:
        from ..runtime.runtime_loop import _print_startup_overview

        _print_startup_overview(agent)
    except Exception:
        # Best-effort: reload should still continue even if startup overview fails.
        pass


def _unwrap_nested_output_stream(stream: Any) -> Any:
    """
    Best-effort stream unwrapping for nested output wrappers.
    This lets full-screen chat reload render on the real terminal stream
    instead of inheriting slash-output indentation wrappers.
    """
    cur = stream
    seen = set()
    while cur is not None:
        sid = id(cur)
        if sid in seen:
            break
        seen.add(sid)
        nxt = getattr(cur, "_base_stream", None)
        if nxt is None:
            nxt = getattr(cur, "_primary", None)
        if nxt is None:
            break
        cur = nxt
    return stream if cur is None else cur


def _reload_chat_from_top(agent: Any, chat_id: str) -> None:
    reload_chat_id = str(chat_id or "").strip()
    if not reload_chat_id:
        print(_t(agent, "chat.reload.no_active"))
        return
    # Ephemeral on-screen notices (e.g. multi-attempt model-call errors)
    # are intentionally NOT part of chat history, so a reload must drop
    # them rather than replay them.
    try:
        clear_notices = getattr(agent, "clear_ephemeral_screen_notices", None)
        if callable(clear_notices):
            clear_notices()
    except Exception:
        pass
    agent._load_chat_state()
    reload_result = agent._activate_chat(
        reload_chat_id,
        announce=False,
        clear_screen=False,
        print_history=False,
    )
    if reload_result:
        print(reload_result)
        return
    try:
        remember = getattr(agent, "_remember_active_chat_history_first_visible_index", None)
        if callable(remember):
            remember(0)
    except Exception:
        pass
    resize_reload = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if callable(resize_reload):
        real_stdout = _unwrap_nested_output_stream(sys.stdout)
        real_stderr = _unwrap_nested_output_stream(sys.stderr)
        if (real_stdout is not sys.stdout) or (real_stderr is not sys.stderr):
            with redirect_stdout(real_stdout), redirect_stderr(real_stderr):
                resize_reload()
        else:
            resize_reload()
        return
    # Fallback: keep historical UX order when shared reload helper is unavailable.
    _clear_terminal_screen()
    _print_startup_overview_safe(agent)
    try:
        agent._print_chat_history(start_index=0)
    except Exception:
        agent._print_chat_history()


def _genuine_user_message_positions(agent: Any) -> list:
    """Return the conversation-history indices of genuine user prompts.

    Internal bookkeeping entries that happen to use the ``user`` role (direct
    shell commands, internal slash commands) are excluded so the index the user
    types matches the messages they actually sent.
    """
    from ..agent import (
        DIRECT_SHELL_USER_HISTORY_PREFIX,
        INTERNAL_SLASH_USER_HISTORY_PREFIX,
    )

    positions = []
    history = list(getattr(agent, "conversation_history", None) or [])
    for i, msg in enumerate(history):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").strip().lower() != "user":
            continue
        content = str(msg.get("content") or "")
        if content.startswith(DIRECT_SHELL_USER_HISTORY_PREFIX):
            continue
        if content.startswith(INTERNAL_SLASH_USER_HISTORY_PREFIX):
            continue
        positions.append(i)
    return positions


def _prefill_next_input(agent: Any, text: str) -> None:
    handler = getattr(agent, "input_handler", None)
    if handler is None:
        return
    setter = getattr(handler, "set_pending_prefill", None)
    if callable(setter):
        try:
            setter(text)
            return
        except Exception:
            pass
    try:
        handler._pending_prefill_text = str(text or "")
        handler._pending_prefill_cursor_position = len(str(text or ""))
    except Exception:
        pass


def handle_chat_edit_command(agent: Any, raw_index: str) -> None:
    value = str(raw_index or "").strip()
    try:
        index = int(value)
    except ValueError:
        print(_t(agent, "chat.edit.invalid_index", value=value))
        return
    if index == 0:
        print(_t(agent, "chat.edit.invalid_index", value=value))
        return

    with agent._chat_state_lock:
        positions = _genuine_user_message_positions(agent)
        count = len(positions)
        if count == 0:
            print(_t(agent, "chat.edit.no_user_messages"))
            return
        if index > 0:
            if index > count:
                print(_t(agent, "chat.edit.out_of_range", value=index, count=count))
                return
            pos_in_list = index - 1
        else:
            if -index > count:
                print(_t(agent, "chat.edit.out_of_range", value=index, count=count))
                return
            pos_in_list = count + index
        target_history_index = positions[pos_in_list]
        message_text = str(
            agent.conversation_history[target_history_index].get("content") or ""
        )
        agent.conversation_history = list(
            agent.conversation_history[:target_history_index]
        )
        try:
            agent._sync_active_chat_messages()
        except Exception:
            pass

    current_chat_id = str(getattr(agent, "active_chat_id", "") or "").strip()
    if current_chat_id:
        _reload_chat_from_top(agent, current_chat_id)
    _prefill_next_input(agent, message_text)
    print(_t(agent, "chat.edit.done"))


def handle_chat_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = str(builtin_line or "").strip()
    if not raw.lower().startswith("chat"):
        return False
    parts = shlex.split(raw)
    if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
        print(chat_usage(agent))
        return True
    sub = parts[1].lower()
    if sub == "list":
        print_chat_list(agent)
        return True
    if sub == "current":
        print(_t(agent, "chat.current", name=agent.active_chat_name, id=agent.active_chat_id))
        return True
    if sub == "reload":
        current_chat_id = str(getattr(agent, "active_chat_id", "") or "").strip()
        _reload_chat_from_top(agent, current_chat_id)
        return True
    if sub == "new":
        name = " ".join(parts[2:]).strip() if len(parts) > 2 else _t(agent, "chat.new.default_name")
        try:
            clear_notices = getattr(agent, "clear_ephemeral_screen_notices", None)
            if callable(clear_notices):
                clear_notices()
        except Exception:
            pass
        with agent._chat_state_lock:
            cid = agent._next_chat_id()
            agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
            agent._save_chat_state()
        agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
        print(_t(agent, "chat.new.created_and_switched", name=agent.active_chat_name, id=agent.active_chat_id))
        return True
    if sub == "switch":
        if len(parts) < 3:
            print(_t(agent, "chat.usage.switch_error"))
            return True
        selector = " ".join(parts[2:]).strip()
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, "chat.not_found_error", selector=selector))
                return True
            cid = str(target.get("id") or "")
        _reload_chat_from_top(agent, cid)
        return True
    if sub == "rename":
        if len(parts) < 4:
            print(_t(agent, "chat.usage.rename_error"))
            return True
        selector = parts[2]
        new_name = " ".join(parts[3:]).strip()
        if not new_name:
            print(_t(agent, "chat.name_empty_error"))
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, "chat.not_found_error", selector=selector))
                return True
            target["name"] = new_name
            target["name_source"] = "manual"
            target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if str(target.get("id") or "") == agent.active_chat_id:
                agent.active_chat_name = new_name
            agent._save_chat_state()
        print(_t(agent, "chat.renamed", name=new_name))
        return True
    if sub == "edit":
        if len(parts) != 3:
            print(_t(agent, "chat.usage.edit_error"))
            return True
        handle_chat_edit_command(agent, parts[2])
        return True
    if sub == "delete":
        if len(parts) < 3:
            print(_t(agent, "chat.usage.delete_error"))
            return True
        selector = " ".join(parts[2:]).strip()
        if selector.lower() == "all":
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                agent._chat_state["chats"] = [agent._new_chat_entry(cid, name=_t(agent, "chat.new.default_name"))]
                agent._chat_state["active"] = cid
                agent._save_chat_state()
            agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
            print(_t(agent, "chat.delete_all_and_create"))
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, "chat.not_found_error", selector=selector))
                return True
            chats = agent._chat_entries()
            if len(chats) <= 1:
                print(_t(agent, "chat.delete_last_error"))
                return True
            tid = str(target.get("id") or "")
            chats[:] = [c for c in chats if str(c.get("id") or "") != tid]
            next_id = agent.active_chat_id
            if tid == agent.active_chat_id:
                next_id = str(chats[0].get("id") or "")
            agent._chat_state["chats"] = chats
            agent._save_chat_state()
        print(_t(agent, "chat.deleted", name=target.get("name"), id=target.get("id")))
        if tid == agent.active_chat_id and next_id:
            agent._activate_chat(next_id, announce=False, clear_screen=False, print_history=True)
        return True
    print(_t(agent, "chat.subcommand_invalid_with_usage", subcommand=sub, usage=chat_usage(agent)))
    return True
