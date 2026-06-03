from __future__ import annotations

import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from typing import Any


def _t(agent: Any, en: str, zh: str) -> str:
    from ..core.localization import get_display_language, text

    return text(en, zh, get_display_language(agent))


def chat_usage(agent: Any) -> str:
    return (
        f"{_t(agent, 'Usage:', '用法：')}\n"
        f"  /chat list\n"
        f"  /chat current\n"
        f"  /chat reload\n"
        f"  /chat new [name]\n"
        f"  /chat switch <index|id|name>\n"
        f"  /chat rename <index|id|name> <new name>\n"
        f"  /chat delete <index|id|name>\n"
        f"  /chat delete all\n"
    )


def print_chat_list(agent: Any) -> None:
    chats = agent._chat_entries()
    if not chats:
        print(_t(agent, "No chats found in the current workspace", "当前工作区中没有聊天记录"))
        return
    print(_t(agent, f"Chats (workspace={agent.workspace_name}):", f"聊天记录（工作区={agent.workspace_name}）："))
    for i, c in enumerate(chats, start=1):
        marker = "*" if str(c.get("id") or "") == agent.active_chat_id else " "
        name = str(c.get("name") or _t(agent, "New Chat", "新聊天"))
        cnt = len(c.get("messages") or [])
        print(_t(agent, f"{marker} [{i}] {name} - {cnt} msgs", f"{marker} [{i}] {name} - {cnt} 条消息"))


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
        print(_t(agent, "❌ There is no active chat to reload", "❌ 当前没有可重新加载的聊天"))
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
        print(_t(agent, f"Current chat: [{agent.active_chat_name}] ({agent.active_chat_id})", f"当前聊天：[{agent.active_chat_name}] ({agent.active_chat_id})"))
        return True
    if sub == "reload":
        current_chat_id = str(getattr(agent, "active_chat_id", "") or "").strip()
        _reload_chat_from_top(agent, current_chat_id)
        return True
    if sub == "new":
        name = " ".join(parts[2:]).strip() if len(parts) > 2 else _t(agent, "New Chat", "新聊天")
        with agent._chat_state_lock:
            cid = agent._next_chat_id()
            agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
            agent._save_chat_state()
        agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
        print(_t(agent, f"✅ Created and switched to chat: [{agent.active_chat_name}] ({agent.active_chat_id})", f"✅ 已创建并切换到聊天：[{agent.active_chat_name}] ({agent.active_chat_id})"))
        return True
    if sub == "switch":
        if len(parts) < 3:
            print(_t(agent, "❌ Usage: /chat switch <index|id|name>", "❌ 用法：/chat switch <索引|id|名称>"))
            return True
        selector = " ".join(parts[2:]).strip()
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, f"❌ Chat not found: {selector}", f"❌ 未找到聊天：{selector}"))
                return True
            cid = str(target.get("id") or "")
        _reload_chat_from_top(agent, cid)
        return True
    if sub == "rename":
        if len(parts) < 4:
            print(_t(agent, "❌ Usage: /chat rename <index|id|name> <new name>", "❌ 用法：/chat rename <索引|id|名称> <新名称>"))
            return True
        selector = parts[2]
        new_name = " ".join(parts[3:]).strip()
        if not new_name:
            print(_t(agent, "❌ Chat name cannot be empty", "❌ 聊天名称不能为空"))
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, f"❌ Chat not found: {selector}", f"❌ 未找到聊天：{selector}"))
                return True
            target["name"] = new_name
            target["name_source"] = "manual"
            target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if str(target.get("id") or "") == agent.active_chat_id:
                agent.active_chat_name = new_name
            agent._save_chat_state()
        print(_t(agent, f"✅ Chat renamed: {new_name}", f"✅ 聊天已重命名：{new_name}"))
        return True
    if sub == "delete":
        if len(parts) < 3:
            print(_t(agent, "❌ Usage: /chat delete <index|id|name>", "❌ 用法：/chat delete <索引|id|名称>"))
            return True
        selector = " ".join(parts[2:]).strip()
        if selector.lower() == "all":
            with agent._chat_state_lock:
                cid = agent._next_chat_id()
                agent._chat_state["chats"] = [agent._new_chat_entry(cid, name=_t(agent, "New Chat", "新聊天"))]
                agent._chat_state["active"] = cid
                agent._save_chat_state()
            agent._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
            print(_t(agent, "✅ Deleted all chats and automatically created a new chat: [New Chat]", "✅ 已删除所有聊天，并自动创建一个新聊天：[新聊天]"))
            return True
        with agent._chat_state_lock:
            target = agent._resolve_chat_selector(selector)
            if not target:
                print(_t(agent, f"❌ Chat not found: {selector}", f"❌ 未找到聊天：{selector}"))
                return True
            chats = agent._chat_entries()
            if len(chats) <= 1:
                print(_t(agent, "❌ At least one chat must be retained; cannot delete the last chat", "❌ 至少需要保留一个聊天，不能删除最后一个聊天"))
                return True
            tid = str(target.get("id") or "")
            chats[:] = [c for c in chats if str(c.get("id") or "") != tid]
            next_id = agent.active_chat_id
            if tid == agent.active_chat_id:
                next_id = str(chats[0].get("id") or "")
            agent._chat_state["chats"] = chats
            agent._save_chat_state()
        print(_t(agent, f"✅ Deleted chat: {target.get('name')} ({target.get('id')})", f"✅ 已删除聊天：{target.get('name')} ({target.get('id')})"))
        if tid == agent.active_chat_id and next_id:
            agent._activate_chat(next_id, announce=False, clear_screen=False, print_history=True)
        return True
    print(_t(agent, f"❌ Unrecognized chat subcommand: {sub}\n{chat_usage(agent)}", f"❌ 无法识别的聊天子命令：{sub}\n{chat_usage(agent)}"))
    return True
