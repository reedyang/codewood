from __future__ import annotations

import os
import shlex
from datetime import datetime
from typing import Any


def chat_usage() -> str:
    return (
        "用法:\n"
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
        print("当前 workspace 下没有 chat")
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
        print(f"当前 Chat: [{agent.active_chat_name}] ({agent.active_chat_id})")
        return True
    if sub == "reload":
        current_chat_id = str(getattr(agent, "active_chat_id", "") or "").strip()
        if not current_chat_id:
            print("❌ 当前没有可重载的 Chat")
            return True
        # Required UX order:
        # 1) clear terminal 2) print startup overview 3) reload current chat history.
        _clear_terminal_screen()
        _print_startup_overview_safe(agent)
        agent._load_chat_state()
        reload_result = agent._activate_chat(
            current_chat_id,
            announce=False,
            clear_screen=False,
            print_history=True,
        )
        if reload_result:
            print(reload_result)
            return True
        return True
    if sub == "new":
        name = " ".join(parts[2:]).strip() if len(parts) > 2 else "New Chat"
        with agent._chat_state_lock:
            cid = agent._next_chat_id()
            agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
            agent._save_chat_state()
        agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
        print(f"✅ 已创建并切换到 Chat: [{agent.active_chat_name}] ({agent.active_chat_id})")
        return True
    if sub == "switch":
        if len(parts) < 3:
            print("❌ 用法: /chat switch <index|id|name>")
            return True
        selector = " ".join(parts[2:]).strip()
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ 未找到 chat: {selector}")
                return True
            cid = str(target.get("id") or "")
        print(agent._activate_chat(cid, announce=True, clear_screen=False, print_history=True))
        return True
    if sub == "rename":
        if len(parts) < 4:
            print("❌ 用法: /chat rename <index|id|name> <new name>")
            return True
        selector = parts[2]
        new_name = " ".join(parts[3:]).strip()
        if not new_name:
            print("❌ Chat 名称不能为空")
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ 未找到 chat: {selector}")
                return True
            target["name"] = new_name
            target["name_source"] = "manual"
            target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if str(target.get("id") or "") == agent.active_chat_id:
                agent.active_chat_name = new_name
            agent._save_chat_state()
        print(f"✅ 已重命名 Chat: {new_name}")
        return True
    if sub == "delete":
        if len(parts) < 3:
            print("❌ 用法: /chat delete <index|id|name>")
            return True
        selector = " ".join(parts[2:]).strip()
        if selector.lower() == "all":
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                agent._chat_state["chats"] = [agent._new_chat_entry(cid, name="New Chat")]
                agent._chat_state["active"] = cid
                agent._save_chat_state()
            agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
            print("✅ 已删除所有 Chat，并自动创建新的 Chat: [New Chat]")
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(f"❌ 未找到 chat: {selector}")
                return True
            chats = agent._chat_entries()
            if len(chats) <= 1:
                print("❌ 至少保留一个 chat，不能删除最后一个")
                return True
            tid = str(target.get("id") or "")
            chats[:] = [c for c in chats if str(c.get("id") or "") != tid]
            next_id = agent.active_chat_id
            if tid == agent.active_chat_id:
                next_id = str(chats[0].get("id") or "")
            agent._chat_state["chats"] = chats
            agent._save_chat_state()
        print(f"✅ 已删除 Chat: {target.get('name')} ({target.get('id')})")
        if tid == agent.active_chat_id and next_id:
            agent._activate_chat(next_id, announce=False, clear_screen=False, print_history=True)
            print(f"✅ 已切换到 Chat: [{agent.active_chat_name}]")
        return True
    print(f"❌ 未识别的 chat 子命令: {sub}\n{chat_usage()}")
    return True
