from __future__ import annotations

import os
import shlex
from datetime import datetime
from typing import Any


def chat_usage() -> str:
    return (
        "Usage:\n"
        "  /chat list\n"
        "  /chat current\n"
        "  /chat reload\n"
        "  /chat new [name]\n"
        "  /chat switch <index|id|name>\n"
        "  /chat rename <index|id|name> <new name>\n"
        "  /chat delete <index|id|name>\n"
        "  /chat delete all\n"
    )


def print_chat_list(agent: Any) -> None:
    chats = agent._chat_entries()
    if not chats:
        print("No chats found in the current workspace")
        return
    print(f"Chats (workspace={agent.workspace_name}):")
    for i, c in enumerate(chats, start=1):
        marker = "*" if str(c.get("id") or "") == agent.active_chat_id else " "
        name = str(c.get("name") or "New Chat")
        cnt = len(c.get("messages") or [])
        print(f"{marker} [{i}] {name} - {cnt} msgs")


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


def _reload_chat_from_top(agent: Any, chat_id: str) -> None:
    reload_chat_id = str(chat_id or "").strip()
    if not reload_chat_id:
        print("❌ There is no active chat to reload")
        return
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
        resize_reload()
        return
    # Fallback: keep historical UX order when shared reload helper is unavailable.
    _clear_terminal_screen()
    _print_startup_overview_safe(agent)
    try:
        agent._print_chat_history(start_index=0)
    except Exception:
        agent._print_chat_history()


def handle_chat_builtin_command(agent: Any, builtin_line: str) -> bool:
    raw = str(builtin_line or "").strip()
    if not raw.lower().startswith("chat"):
        return False
    parts = shlex.split(raw)
    if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
        print(chat_usage())
        return True
    sub = parts[1].lower()
    if sub == "list":
        print_chat_list(agent)
        return True
    if sub == "current":
        print(f"Current chat: [{agent.active_chat_name}] ({agent.active_chat_id})")
        return True
    if sub == "reload":
        current_chat_id = str(getattr(agent, "active_chat_id", "") or "").strip()
        _reload_chat_from_top(agent, current_chat_id)
        return True
    if sub == "new":
        name = " ".join(parts[2:]).strip() if len(parts) > 2 else "New Chat"
        with agent._chat_state_lock:
            cid = agent._next_chat_id()
            agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
            agent._save_chat_state()
        agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
        print(f"✅ Created and switched to chat: [{agent.active_chat_name}] ({agent.active_chat_id})")
        return True
    if sub == "switch":
        if len(parts) < 3:
            print("❌ Usage: /chat switch <index|id|name>")
            return True
        selector = " ".join(parts[2:]).strip()
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ Chat not found: {selector}")
                return True
            cid = str(target.get("id") or "")
        _reload_chat_from_top(agent, cid)
        return True
    if sub == "rename":
        if len(parts) < 4:
            print("❌ Usage: /chat rename <index|id|name> <new name>")
            return True
        selector = parts[2]
        new_name = " ".join(parts[3:]).strip()
        if not new_name:
            print("❌ Chat name cannot be empty")
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ Chat not found: {selector}")
                return True
            target["name"] = new_name
            target["name_source"] = "manual"
            target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if str(target.get("id") or "") == agent.active_chat_id:
                agent.active_chat_name = new_name
            agent._save_chat_state()
        print(f"✅ Chat renamed: {new_name}")
        return True
    if sub == "delete":
        if len(parts) < 3:
            print("❌ Usage: /chat delete <index|id|name>")
            return True
        selector = " ".join(parts[2:]).strip()
        if selector.lower() == "all":
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                agent._chat_state["chats"] = [agent._new_chat_entry(cid, name="New Chat")]
                agent._chat_state["active"] = cid
                agent._save_chat_state()
            agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
            print("✅ Deleted all chats and automatically created a new chat: [New Chat]")
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ Chat not found: {selector}")
                return True
            chats = agent._chat_entries()
            if len(chats) <= 1:
                print("❌ At least one chat must be retained; cannot delete the last chat")
                return True
            tid = str(target.get("id") or "")
            chats[:] = [c for c in chats if str(c.get("id") or "") != tid]
            next_id = agent.active_chat_id
            if tid == agent.active_chat_id:
                next_id = str(chats[0].get("id") or "")
            agent._chat_state["chats"] = chats
            agent._save_chat_state()
        print(f"✅ Deleted chat: {target.get('name')} ({target.get('id')})")
        if tid == agent.active_chat_id and next_id:
            agent._activate_chat(next_id, announce=False, clear_screen=False, print_history=True)
        return True
    print(f"❌ Unrecognized chat subcommand: {sub}\n{chat_usage()}")
    return True
