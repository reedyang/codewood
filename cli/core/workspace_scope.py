"""Per-thread workspace-context resolution shared by tools and prompt builders.

A chat whose loop keeps running in the background must resolve relative
paths, cache dirs and system-prompt roots against ITS OWN workspace even
while the user focuses another workspace (which swaps the agent's global
workspace attributes). Real agents expose ``_effective_workspace_root`` and
friends that prefer a thread-local per-chat override (installed by
``ServeApp._run_chat_loop``); these helpers prefer those accessors and fall
back to the legacy global attributes, so bare test doubles and threads
without an override keep their historical behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def effective_workspace_root(agent: Any) -> Path:
    """Resolve the calling thread's workspace root (override wins)."""
    resolver = getattr(agent, "_effective_workspace_root", None)
    if callable(resolver):
        try:
            resolved = resolver()
            if resolved:
                return Path(str(resolved))
        except Exception:
            pass
    raw = getattr(agent, "workspace_root", None)
    if raw:
        try:
            return Path(str(raw)).expanduser().resolve()
        except Exception:
            return Path(str(raw))
    return Path(getattr(agent, "work_directory", Path.cwd()))


def effective_workspace_config_dir(agent: Any) -> Path:
    """Resolve the calling thread's workspace data dir (override wins)."""
    resolver = getattr(agent, "_effective_workspace_config_dir", None)
    if callable(resolver):
        try:
            resolved = resolver()
            if resolved:
                return Path(str(resolved))
        except Exception:
            pass
    raw = getattr(agent, "workspace_config_dir", None)
    if raw:
        return Path(str(raw))
    return Path(getattr(agent, "config_dir", Path.cwd()))


def effective_workspace_id(agent: Any) -> str:
    """Resolve the calling thread's workspace id (override wins)."""
    resolver = getattr(agent, "_effective_workspace_id", None)
    if callable(resolver):
        try:
            value = resolver()
            if value:
                return str(value)
        except Exception:
            pass
    return str(getattr(agent, "workspace_id", "") or "")


def effective_workspace_name(agent: Any) -> str:
    """Resolve the calling thread's workspace name (override wins)."""
    resolver = getattr(agent, "_effective_workspace_name", None)
    if callable(resolver):
        try:
            value = resolver()
            if value:
                return str(value)
        except Exception:
            pass
    return str(getattr(agent, "workspace_name", "") or "")


def effective_work_directory(agent: Any) -> Path:
    """Resolve the calling thread's working directory (override wins)."""
    resolver = getattr(agent, "_effective_work_directory", None)
    if callable(resolver):
        try:
            resolved = resolver()
            if resolved:
                return Path(str(resolved))
        except Exception:
            pass
    return Path(getattr(agent, "work_directory", Path.cwd()))
