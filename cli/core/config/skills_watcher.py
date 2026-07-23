"""File-system watcher for skill directories and skills.jsonc configs.

Monitors all non-builtin skill roots for SKILL.md changes plus skills.jsonc
files, debounces events, and triggers a live skills reload on the agent.

Supports dynamic workspace path updates via ``update_workspace_paths()`` so
workspace switches don't require restarting the watcher.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False


_SKILL_MD = "SKILL.md"
_SKILLS_JSONC = "skills.jsonc"


def _is_skill_md(path: str) -> bool:
    return Path(path).name == _SKILL_MD


def _is_skills_jsonc(path: str) -> bool:
    return Path(path).name == _SKILLS_JSONC


def _resolve_watch_dir(path: Optional[Path]) -> Optional[Path]:
    """Resolve a directory for watching; returns None if it doesn't exist."""
    if path is None:
        return None
    p = Path(path).expanduser().resolve()
    # For directories that don't exist yet, return the parent if it exists
    # so the watcher can pick up creation events.
    if p.is_dir():
        return p
    parent = p.parent
    if parent.is_dir():
        return parent
    return None


def _resolve_watch_file(path: Optional[Path]) -> Optional[Path]:
    """Resolve a file path for watching; returns the file path if it exists,
    or its parent directory so creation events are caught."""
    if path is None:
        return None
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return p
    parent = p.parent
    if parent.is_dir():
        return parent
    return None


class _SkillsEventHandler(FileSystemEventHandler):
    """Batch file-system events and call a reload callback after a debounce."""

    def __init__(self, reload_fn: Callable[[], None], debounce_s: float = 1.0):
        super().__init__()
        self._reload_fn = reload_fn
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._pending = False

    def _on_any_event(self) -> None:
        with self._lock:
            if self._pending:
                return
            self._pending = True
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce_s, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._pending = False
            self._timer = None
        try:
            self._reload_fn()
        except Exception:
            pass

    def _relevant(self, event: Any) -> bool:
        src = getattr(event, "src_path", "") or ""
        dest = getattr(event, "dest_path", "") or ""
        path = src or dest or ""
        if not path:
            return False
        if _is_skill_md(path):
            return True
        if _is_skills_jsonc(path):
            return True
        if getattr(event, "is_directory", False):
            return True
        return False

    def on_created(self, event: Any) -> None:
        if self._relevant(event):
            self._on_any_event()

    def on_modified(self, event: Any) -> None:
        if self._relevant(event):
            self._on_any_event()

    def on_deleted(self, event: Any) -> None:
        if self._relevant(event):
            self._on_any_event()

    def on_moved(self, event: Any) -> None:
        if self._relevant(event):
            self._on_any_event()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = False


def start_skills_watcher(
    reload_fn: Callable[[], None],
    builtin: Optional[Path],
    agents: Optional[Path],
    global_dir: Optional[Path],
    ws_agents: Optional[Path],
    ws_dir: Optional[Path],
    global_jsonc: Optional[Path],
    ws_jsonc: Optional[Path],
    debounce_s: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Start a watchdog Observer monitoring skill dirs and skills.jsonc.

    Returns a handle dict with keys ``observer``, ``handler``, ``global_dirs``,
    ``ws_dirs`` for use with ``update_workspace_paths`` and ``stop_skills_watcher``.
    Returns None if watchdog is unavailable.
    """
    if not _WATCHDOG_AVAILABLE:
        return None

    # Global paths (never change)
    global_dirs: Set[str] = set()
    for p in (agents, global_dir, global_jsonc):
        r = _resolve_watch_dir(p)
        if r is not None:
            global_dirs.add(str(r))

    # Workspace paths (may change on workspace switch)
    ws_dirs: Set[str] = set()
    for p in (ws_agents, ws_dir, ws_jsonc):
        r = _resolve_watch_dir(p)
        if r is not None:
            ws_dirs.add(str(r))

    all_dirs = global_dirs | ws_dirs
    if not all_dirs:
        return None

    try:
        handler = _SkillsEventHandler(reload_fn, debounce_s=debounce_s)
        observer = Observer()
        watches: Dict[str, Any] = {}
        for d in sorted(all_dirs):
            w = observer.schedule(handler, d, recursive=True)
            watches[d] = w
        observer.start()
        return {
            "observer": observer,
            "handler": handler,
            "watches": watches,
            "global_dirs": global_dirs,
            "ws_dirs": ws_dirs,
        }
    except Exception:
        return None


def update_workspace_paths(
    handle: Optional[Dict[str, Any]],
    ws_agents: Optional[Path],
    ws_dir: Optional[Path],
    ws_jsonc: Optional[Path],
) -> None:
    """Update the workspace-specific watched paths without restarting the observer.

    Called when the workspace changes. Old workspace paths are unscheduled,
    new ones are scheduled. Global paths are left untouched.
    """
    if handle is None:
        return
    observer = handle.get("observer")
    handler = handle.get("handler")
    watches: Dict[str, Any] = handle.get("watches") or {}
    if observer is None or handler is None:
        return

    new_ws: Set[str] = set()
    for p in (ws_agents, ws_dir, ws_jsonc):
        r = _resolve_watch_dir(p)
        if r is not None:
            new_ws.add(str(r))

    old_ws = handle.get("ws_dirs", set()) or set()
    global_dirs = handle.get("global_dirs", set()) or set()

    # Unschedule old workspace paths
    for d in sorted(old_ws - new_ws):
        if d in global_dirs:
            continue
        watch = watches.pop(d, None)
        if watch is not None:
            try:
                observer.unschedule(watch)
            except Exception:
                pass

    # Schedule new workspace paths
    for d in sorted(new_ws - old_ws):
        if d in global_dirs:
            continue
        try:
            w = observer.schedule(handler, d, recursive=True)
            watches[d] = w
        except Exception:
            pass

    handle["ws_dirs"] = new_ws


def stop_skills_watcher(handle: Optional[Dict[str, Any]]) -> None:
    """Stop a previously-started skills watcher cleanly."""
    if handle is None:
        return
    handler = handle.get("handler")
    if handler is not None and hasattr(handler, "cancel"):
        try:
            handler.cancel()
        except Exception:
            pass
    observer = handle.get("observer")
    if observer is not None:
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:
            pass
    handle.clear()
