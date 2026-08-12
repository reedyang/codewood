from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname
from .embedding import _cosine_similarity
from .tree_sitter_parser import parse_file_tree_sitter
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

# Storage schema version for the SQLite index. Bump when the table layout
# changes; an on-disk database with a different version is discarded and
# rebuilt (we intentionally do NOT migrate or read legacy JSON indexes).
_SCHEMA_VERSION = 2


_DEFAULT_CODE_EXTS: Set[str] = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".kts",
    ".rb",
    ".php",
    ".m",
    ".mm",
}

_DEFAULT_EXCLUDE_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "dist",
    "build",
    "out",
    "target",
    ".idea",
    ".vscode",
    get_app_config_dirname(),
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "venv",
    ".venv",
    "env",
    ".env",
    "vendor",
    "vendors",
    ".cache",
    ".turbo",
    ".next",
    ".nuxt",
    ".output",
    ".angular",
    ".terraform",
    ".serverless",
    "coverage",
    ".coverage",
    "eggs",
    ".eggs",
    "wheelhouse",
    "__pypackages__",
    "site-packages",
}


def _now_ts() -> float:
    return time.time()


def _normalize_token(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", (s or "").strip().lower())


def _guess_symbol_kind(rel: str, sym: str) -> str:
    lower = (sym or "").lower()
    if lower.startswith("_") and lower.endswith("_"):
        return "dunder"
    if lower.startswith("_"):
        return "private"
    ext = Path(rel).suffix.lower() if rel else ""
    if ext in (".py",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "function"
    if ext in (".go",):
        if re.match(r"^[A-Z][a-z0-9]", sym or ""):
            return "exported_func"
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "type"
        return "function"
    if ext in (".rs",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "type"
        if (sym or "").endswith("!"):
            return "macro"
        return "function"
    if ext in (".java", ".kt", ".kts", ".scala"):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "method"
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class_or_component"
        return "function"
    if ext in (".cs",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        if re.match(r"^I[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "interface"
        return "method"
    if ext in (".c", ".h"):
        return "function"
    if ext in (".cpp", ".cxx", ".hpp", ".cc"):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "function"
    if ext in (".swift",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "func"
    if ext in (".rb",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "method"
    if ext in (".php",):
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", sym or ""):
            return "class"
        return "function"
    return "symbol"


# A call site is an identifier immediately followed by ``(``. The leading
# ``(?<![\w.])`` keeps it from matching the tail of ``obj.method`` as a bare
# ``method`` while still capturing the unqualified call name in simple cases.
_CALL_SITE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Control-flow / declaration keywords that look like calls (``if (...)``) but
# are not. Kept language-agnostic and intentionally small.
_CALL_KEYWORDS: Set[str] = {
    "if", "for", "while", "switch", "catch", "return", "with", "elif",
    "def", "function", "class", "and", "or", "not", "in", "is", "await",
    "yield", "del", "assert", "raise", "lambda", "print", "super",
    "typeof", "sizeof", "new", "delete", "throw", "case", "do", "else",
}


def _derive_tokens(rel: str, e: _FileEntry) -> List[str]:
    if e.tokens:
        return e.tokens
    token_set: Set[str] = set()
    for t in _split_words(rel):
        token_set.add(t)
    for s in e.symbols:
        for t in _split_words(s):
            token_set.add(t)
    for imp in e.imports:
        for t in _split_words(imp):
            token_set.add(t)
    return sorted(token_set)[:300]


def _split_words(s: str) -> List[str]:
    raw = re.split(r"[^A-Za-z0-9_]+", str(s or ""))
    out: List[str] = []
    for w in raw:
        t = _normalize_token(w)
        if len(t) >= 2:
            out.append(t)
    return out


@dataclass
class _CallEdge:
    """A directed call edge ``caller`` -> ``callee`` extracted from a file.

    ``caller`` is the enclosing function/method symbol (or an empty string for
    module/top-level calls); ``callee`` is the called name as written at the
    call site (best-effort, unqualified).
    """

    caller: str
    callee: str


@dataclass
class _FileEntry:
    path: str
    mtime_ns: int
    size: int
    symbols: List[str]
    imports: List[str]
    tokens: List[str]
    calls: List[_CallEdge] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "symbols": self.symbols,
            "imports": self.imports,
            "tokens": self.tokens,
            "calls": [{"caller": c.caller, "callee": c.callee} for c in self.calls],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "_FileEntry":
        calls_raw = d.get("calls") or []
        calls: List[_CallEdge] = []
        for c in calls_raw:
            if isinstance(c, dict):
                callee = str(c.get("callee") or "").strip()
                if callee:
                    calls.append(_CallEdge(caller=str(c.get("caller") or "").strip(), callee=callee))
        return _FileEntry(
            path=str(d.get("path") or ""),
            mtime_ns=int(d.get("mtime_ns") or 0),
            size=int(d.get("size") or 0),
            symbols=[str(x) for x in (d.get("symbols") or []) if str(x).strip()],
            imports=[str(x) for x in (d.get("imports") or []) if str(x).strip()],
            tokens=[str(x) for x in (d.get("tokens") or []) if str(x).strip()],
            calls=calls,
        )


def _write_status_file(path: str, data: dict) -> None:
    """Atomically write *data* to *path* as JSON (tmp + replace)."""
    data["_ts"] = time.time()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _make_status_payload(
    phase: str,
    progress_total: int = 0,
    progress_done: int = 0,
    expected_total: int = 0,
    checkpointed_done: int = 0,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "phase": str(phase or ""),
        "progress_total": int(progress_total or 0),
        "progress_done": int(progress_done or 0),
        "expected_total": int(expected_total or 0),
        "checkpointed_done": int(checkpointed_done or 0),
    }
    payload.update(extra)
    return payload


def _index_refresh_worker(workspace_root: str, storage_dir: str, status_file: str) -> None:
    """Run in a child process (``multiprocessing.spawn``) to build the
    project context index.  Writes progress to *status_file* so the
    main process can surface it via ``status()``.
    """
    _ts_ok = False
    try:
        from cli.tools.tree_sitter_parser import _TS_AVAILABLE
        _ts_ok = bool(_TS_AVAILABLE)
    except Exception:
        pass
    _write_status_file(
        status_file,
        _make_status_payload("scanning", ts=_ts_ok),
    )
    try:
        from cli.tools.project_context_index import ProjectContextIndex

        idx = ProjectContextIndex(
            workspace_root=Path(workspace_root),
            storage_dir=Path(storage_dir),
        )

        # Background thread: poll index progress and write to status file
        # every 2 seconds so the GUI can display live progress.
        _stop_poll = threading.Event()
        _phase1_peak = [0]
        _stable_phase = [""]
        _t0 = time.time()

        def _poll_progress() -> None:
            while not _stop_poll.is_set():
                phase = idx._refresh_progress_phase or ""
                total = idx._refresh_progress_total
                done = idx._refresh_progress_done
                checkpointed_done = idx._refresh_checkpointed_done
                expected_total = max(idx._index_expected_total, checkpointed_done, len(idx.files))
                if phase and phase != "saving":
                    _stable_phase[0] = phase
                if phase == "scanning" and done > _phase1_peak[0]:
                    _phase1_peak[0] = done
                display_phase = phase or _stable_phase[0] or "scanning"
                if (
                    not phase
                    and _stable_phase[0] == "indexing"
                    and total == 0
                    and done == 0
                    and expected_total > 0
                    and max(checkpointed_done, len(idx.files)) >= expected_total
                ):
                    # Parsing is finished and the in-memory counters have been
                    # reset, but the worker is still saving/finalizing. Keep
                    # the GUI on the terminal phase instead of flashing back
                    # to "Indexing 0%".
                    display_phase = "saving"
                _write_status_file(
                    status_file,
                    _make_status_payload(
                        display_phase,
                        progress_total=(total or _phase1_peak[0]),
                        progress_done=done,
                        expected_total=max(expected_total, _phase1_peak[0]),
                        checkpointed_done=checkpointed_done,
                        elapsed_sec=int(time.time() - _t0),
                        ts=_ts_ok,
                    ),
                )
                _stop_poll.wait(timeout=0.5)

        pt = threading.Thread(target=_poll_progress, daemon=True)
        pt.start()

        result = idx.refresh_index(force=False, timeout_ms=None)
        _stop_poll.set()
        pt.join(timeout=3)

        try:
            idx.initialize_embedding_provider()
            if idx._embedding_provider and idx._embedding_provider.available:
                if len(idx.files) > 0:
                    emb_result = idx.build_embeddings()
        except Exception:
            pass

        _write_status_file(
            status_file,
            _make_status_payload(
                "done",
                progress_total=result.get("files_total", 0),
                progress_done=result.get("files_total", 0),
                expected_total=result.get("files_total", 0),
                checkpointed_done=result.get("files_total", 0),
            ),
        )
    except Exception as exc:
        _write_status_file(
            status_file,
            _make_status_payload("error", error=str(exc)),
        )
        raise


class ProjectContextIndex:
    """
    Lightweight project index for M1:
    - incremental file refresh by mtime/size
    - symbol/import/path token extraction
    - query -> ranked candidate files with reasons
    """

    def __init__(self, workspace_root: Path, storage_dir: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.storage_dir / "project_context_index.db"
        self.files: Dict[str, _FileEntry] = {}
        self.last_index_at: float = 0.0
        self.version: int = _SCHEMA_VERSION
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._embedding_index = None
        self._embedding_provider: Optional[EmbeddingProvider] = None
        self._agent_params: Dict[str, Any] = {}
        self._refresh_progress_total: int = 0
        self._refresh_progress_done: int = 0
        self._refresh_progress_phase: str = ""
        self._file_watcher: Optional[Any] = None
        self._yield_event = threading.Event()
        self._subprocess: Optional[multiprocessing.Process] = None
        self._status_file: Optional[str] = str(self.storage_dir / ".index_status.json")
        self._index_expected_total: int = 0
        self._index_checkpointed_done: int = 0
        self._refresh_checkpointed_done: int = 0
        self._load()
        self._load_stale_status_counters()
        self._start_watcher()

    def _start_watcher(self) -> None:
        with self._lock:
            if self._file_watcher is not None:
                return
            obs = _start_file_watcher(self)
            if obs is not None:
                self._file_watcher = obs

    def _stop_watcher(self) -> None:
        with self._lock:
            obs = self._file_watcher
            self._file_watcher = None
        _stop_file_watcher(obs)

    def shutdown(self) -> None:
        self._stop_watcher()

    def request_yield(self) -> None:
        self._yield_event.set()

    def _yield_if_requested(self) -> None:
        if self._yield_event.is_set():
            self._yield_event.clear()
            time.sleep(0.5)

    def start_subprocess_refresh(self, on_done: Optional[Any] = None) -> bool:
        """Launch the index refresh in a child process (separate GIL).

        Returns ``True`` if the process was started, ``False`` if one is
        already running or the subprocess could not be created.
        """
        if self._subprocess is not None and self._subprocess.is_alive():
            return False

        try:
            ctx = multiprocessing.get_context("spawn")
            sf = str(self.storage_dir / ".index_status.json")
            self._status_file = sf
            _write_status_file(
                sf,
                _make_status_payload(
                    "scanning",
                    expected_total=max(self._index_expected_total, len(self.files)),
                    checkpointed_done=max(self._index_checkpointed_done, len(self.files)),
                ),
            )
            proc = ctx.Process(
                target=_index_refresh_worker,
                args=(str(self.workspace_root), str(self.storage_dir), sf),
                daemon=True,
            )
            proc.start()
            self._subprocess = proc

            def _monitor() -> None:
                try:
                    proc.join()
                except Exception:
                    pass
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass
                self._subprocess = None

            threading.Thread(target=_monitor, daemon=True).start()
            return True
        except Exception:
            return False

    @staticmethod
    def _read_status_file_by_path(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(str(path), "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _read_status_file(self) -> Optional[Dict[str, Any]]:
        sf = self._status_file
        if sf is None:
            return None
        return self._read_status_file_by_path(Path(sf))

    def _load_stale_status_counters(self) -> None:
        expected_total = 0
        checkpointed_done = 0
        try:
            st = self._read_status_file()
            if st is not None:
                expected_total = int(st.get("expected_total", 0) or 0)
                if expected_total == 0:
                    expected_total = int(st.get("progress_total", 0) or 0)
                checkpointed_done = int(st.get("checkpointed_done", 0) or 0)
        except Exception:
            pass
        self._index_expected_total = max(expected_total, len(self.files))
        self._index_checkpointed_done = max(checkpointed_done, len(self.files))

    def _compute_refresh_progress_percent(
        self,
        phase: str,
        total: int,
        done: int,
        expected_total: int = 0,
    ) -> int:
        phase_l = str(phase or "").strip().lower()
        total_i = max(0, int(total or 0))
        done_i = max(0, int(done or 0))
        expected_i = max(0, int(expected_total or 0), int(self._index_expected_total or 0))
        if phase_l == "scanning":
            denom = max(expected_i, done_i)
            if denom <= 0:
                return 0
            if done_i >= denom:
                return 100
            return min(99, math.floor((done_i * 100) / denom))
        if phase_l == "indexing":
            if total_i <= 0:
                return 0
            return max(0, min(100, math.floor((done_i * 100) / total_i)))
        if phase_l == "saving":
            if total_i <= 0:
                return 100
            return max(0, min(100, math.floor((done_i * 100) / total_i)))
        if phase_l == "done":
            return 100
        return 0

    def _save_checkpoint_batch(
        self,
        entries: Dict[str, _FileEntry],
        checkpoint_ts: Optional[float] = None,
    ) -> None:
        if not entries:
            return
        when = float(checkpoint_ts) if checkpoint_ts is not None else _now_ts()
        conn = self._connect()
        try:
            self._create_schema(conn)
            rels = list(entries.keys())
            conn.executemany(
                "INSERT OR REPLACE INTO files (rel, path, mtime_ns, size) VALUES (?, ?, ?, ?)",
                [(rel, e.path, e.mtime_ns, e.size) for rel, e in entries.items()],
            )
            conn.executemany("DELETE FROM symbols WHERE file_rel = ?", [(rel,) for rel in rels])
            conn.executemany("DELETE FROM imports WHERE file_rel = ?", [(rel,) for rel in rels])
            conn.executemany("DELETE FROM tokens WHERE file_rel = ?", [(rel,) for rel in rels])
            conn.executemany("DELETE FROM calls WHERE file_rel = ?", [(rel,) for rel in rels])
            sym_rows: List[Tuple[str, str, int]] = []
            imp_rows: List[Tuple[str, str, int]] = []
            tok_rows: List[Tuple[str, str]] = []
            call_rows: List[Tuple[str, str, str, int]] = []
            for rel, e in entries.items():
                for i, s in enumerate(e.symbols):
                    sym_rows.append((rel, s, i))
                for i, imp in enumerate(e.imports):
                    imp_rows.append((rel, imp, i))
                for t in e.tokens:
                    tok_rows.append((rel, t))
                for i, c in enumerate(e.calls):
                    call_rows.append((rel, c.caller, c.callee, i))
            if sym_rows:
                conn.executemany(
                    "INSERT INTO symbols (file_rel, name, ord) VALUES (?, ?, ?)",
                    sym_rows,
                )
            if imp_rows:
                conn.executemany(
                    "INSERT INTO imports (file_rel, value, ord) VALUES (?, ?, ?)",
                    imp_rows,
                )
            if tok_rows:
                conn.executemany(
                    "INSERT INTO tokens (file_rel, token) VALUES (?, ?)",
                    tok_rows,
                )
            if call_rows:
                conn.executemany(
                    "INSERT INTO calls (file_rel, caller, callee, ord) VALUES (?, ?, ?, ?)",
                    call_rows,
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('workspace_root', ?)",
                (str(self.workspace_root),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_index_at', ?)",
                (repr(float(when)),),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _emit_status_snapshot(self, phase_override: Optional[str] = None) -> None:
        sf = self._status_file
        if not sf:
            return
        phase = str(phase_override or self._refresh_progress_phase or "").strip()
        if not phase:
            return
        expected_total = max(
            int(self._index_expected_total or 0),
            int(self._refresh_checkpointed_done or 0),
            len(self.files),
        )
        progress_total = int(self._refresh_progress_total or 0)
        progress_done = int(self._refresh_progress_done or 0)
        if phase == "scanning" and progress_total <= 0:
            progress_total = progress_done
        _write_status_file(
            sf,
            _make_status_payload(
                phase,
                progress_total=progress_total,
                progress_done=progress_done,
                expected_total=expected_total,
                checkpointed_done=int(self._refresh_checkpointed_done or 0),
            ),
        )

    def bind_workspace(self, workspace_root: Path, storage_dir: Optional[Path] = None) -> None:
        root = Path(workspace_root).resolve()
        target_storage = (
            Path(storage_dir).resolve() if storage_dir is not None else self.storage_dir
        )
        with self._lock:
            if (
                str(root) == str(self.workspace_root)
                and str(target_storage) == str(self.storage_dir)
            ):
                return
            self._stop_watcher()
            self.workspace_root = root
            if str(target_storage) != str(self.storage_dir):
                self.storage_dir = target_storage
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                self.index_path = self.storage_dir / "project_context_index.db"
            self._status_file = str(self.storage_dir / ".index_status.json")
            self.files = {}
            self.last_index_at = 0.0
            self._load()
            self._load_stale_status_counters()
            self._start_watcher()

    def _connect(self) -> sqlite3.Connection:
        # Caller controls synchronization. A fresh connection per operation
        # keeps the index thread-safe under the class-level RLock without
        # juggling SQLite's per-connection thread affinity.
        conn = sqlite3.connect(str(self.index_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                rel TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                file_rel TEXT NOT NULL,
                name TEXT NOT NULL,
                ord INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imports (
                file_rel TEXT NOT NULL,
                value TEXT NOT NULL,
                ord INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                file_rel TEXT NOT NULL,
                token TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                file_rel TEXT NOT NULL,
                caller TEXT NOT NULL,
                callee TEXT NOT NULL,
                ord INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_rel);
            CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_rel);
            CREATE INDEX IF NOT EXISTS idx_tokens_file ON tokens(file_rel);
            CREATE INDEX IF NOT EXISTS idx_tokens_token ON tokens(token);
            CREATE INDEX IF NOT EXISTS idx_calls_file ON calls(file_rel);
            CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee);
            CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller);
            """
        )

    def _load(self) -> None:
        # Caller controls synchronization. Keep this helper lock-free.
        if not self.index_path.is_file():
            return
        try:
            conn = self._connect()
        except Exception:
            self.files = {}
            self.last_index_at = 0.0
            return
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.Error:
                row = None
            on_disk_version = int(row[0]) if row and str(row[0]).isdigit() else 0
            if on_disk_version != _SCHEMA_VERSION:
                # Incompatible / legacy database: discard and rebuild from
                # scratch on the next refresh. We never migrate old data.
                self.files = {}
                self.last_index_at = 0.0
                return

            at_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_index_at'"
            ).fetchone()
            try:
                self.last_index_at = float(at_row[0]) if at_row else 0.0
            except Exception:
                self.last_index_at = 0.0
            self.version = _SCHEMA_VERSION

            entries: Dict[str, _FileEntry] = {}
            for rel, path, mtime_ns, size in conn.execute(
                "SELECT rel, path, mtime_ns, size FROM files"
            ):
                entries[str(rel)] = _FileEntry(
                    path=str(path),
                    mtime_ns=int(mtime_ns),
                    size=int(size),
                    symbols=[],
                    imports=[],
                    tokens=[],
                    calls=[],
                )
            for file_rel, name in conn.execute(
                "SELECT file_rel, name FROM symbols ORDER BY file_rel, ord"
            ):
                e = entries.get(str(file_rel))
                if e is not None:
                    e.symbols.append(str(name))
            for file_rel, value in conn.execute(
                "SELECT file_rel, value FROM imports ORDER BY file_rel, ord"
            ):
                e = entries.get(str(file_rel))
                if e is not None:
                    e.imports.append(str(value))
            # Tokens are derived from symbols + imports in search().
            # Not loaded at startup to keep _load() fast for large projects.
            # Calls are stored in SQLite only, queried on demand by call_graph().
            # Loading millions of rows into memory blocks startup for large projects.
            self.files = entries
        except Exception:
            self.files = {}
            self.last_index_at = 0.0
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _save(self) -> None:
        with self._save_lock:
            self._save_locked()

    def _save_locked(self) -> None:
        conn = self._connect()
        try:
            save_total = 10
            save_done = 0

            def _advance_save_progress(steps: int = 1) -> None:
                nonlocal save_done
                save_done = min(save_total, save_done + max(0, int(steps or 0)))
                if self._refresh_progress_phase == "saving":
                    self._refresh_progress_total = save_total
                    self._refresh_progress_done = save_done

            if self._refresh_progress_phase == "saving":
                self._refresh_progress_total = save_total
                self._refresh_progress_done = 0

            self._create_schema(conn)
            _advance_save_progress()
            conn.execute("DELETE FROM files")
            _advance_save_progress()
            conn.execute("DELETE FROM symbols")
            _advance_save_progress()
            conn.execute("DELETE FROM imports")
            _advance_save_progress()
            conn.execute("DELETE FROM tokens")
            _advance_save_progress()
            conn.execute("DELETE FROM calls")
            _advance_save_progress()
            conn.executemany(
                "INSERT OR REPLACE INTO files (rel, path, mtime_ns, size) VALUES (?, ?, ?, ?)",
                [(rel, e.path, e.mtime_ns, e.size) for rel, e in self.files.items()],
            )
            _advance_save_progress()
            sym_rows: List[Tuple[str, str, int]] = []
            imp_rows: List[Tuple[str, str, int]] = []
            tok_rows: List[Tuple[str, str]] = []
            call_rows: List[Tuple[str, str, str, int]] = []
            for rel, e in self.files.items():
                for i, s in enumerate(e.symbols):
                    sym_rows.append((rel, s, i))
                for i, imp in enumerate(e.imports):
                    imp_rows.append((rel, imp, i))
                for t in e.tokens:
                    tok_rows.append((rel, t))
                for i, c in enumerate(e.calls):
                    call_rows.append((rel, c.caller, c.callee, i))
            conn.executemany(
                "INSERT INTO symbols (file_rel, name, ord) VALUES (?, ?, ?)", sym_rows
            )
            conn.executemany(
                "INSERT INTO imports (file_rel, value, ord) VALUES (?, ?, ?)", imp_rows
            )
            conn.executemany(
                "INSERT INTO tokens (file_rel, token) VALUES (?, ?)", tok_rows
            )
            conn.executemany(
                "INSERT INTO calls (file_rel, caller, callee, ord) VALUES (?, ?, ?, ?)",
                call_rows,
            )
            _advance_save_progress()
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('workspace_root', ?)",
                (str(self.workspace_root),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_index_at', ?)",
                (repr(float(self.last_index_at)),),
            )
            _advance_save_progress()
            conn.commit()
            _advance_save_progress()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _iter_code_files(self, deadline_ts: Optional[float] = None, progress_cb: Any = None) -> Tuple[List[Path], bool]:
        out: List[Path] = []
        root = self.workspace_root
        if not root.is_dir():
            return out, False
        timed_out = False
        root_s = str(root)
        git_spec = _load_gitignore(root_s)
        for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
            if deadline_ts is not None and _now_ts() >= deadline_ts:
                timed_out = True
                break
            dirnames[:] = [
                d for d in dirnames
                if str(d or "").lower() not in _DEFAULT_EXCLUDE_DIRS
                and not _is_venv_dir(os.path.join(dirpath, str(d)))
            ]
            dirnames[:] = [d for d in dirnames if not _is_gitignored(
                os.path.relpath(os.path.join(dirpath, d), root_s), git_spec
            )]
            for fn in filenames:
                if deadline_ts is not None and _now_ts() >= deadline_ts:
                    timed_out = True
                    break
                p = Path(dirpath) / str(fn)
                if p.suffix.lower() not in _DEFAULT_CODE_EXTS:
                    continue
                rel = os.path.relpath(str(p), root_s)
                if _is_gitignored(rel, git_spec):
                    continue
                out.append(p)
                if callable(progress_cb) and len(out) % 10 == 0:
                    progress_cb(len(out))
            if timed_out:
                break
        if callable(progress_cb):
            progress_cb(len(out))
        return out, timed_out

    def _parse_file(self, p: Path, rel: str, st_mtime_ns: int, st_size: int) -> _FileEntry:
        self._yield_if_requested()
        ts_symbols, ts_imports, ts_calls, ts_tokens = parse_file_tree_sitter(p)
        # Yield again after tree-sitter parse returns — the C parse() call
        # releases the GIL, giving HTTP handler threads a chance to set the
        # yield event. Without a re-check here the BG thread would continue
        # straight into CPU-bound extraction code, holding the GIL for
        # another ~50-500 ms before the next file boundary.
        self._yield_if_requested()
        if ts_symbols or ts_imports or ts_tokens:
            calls = [_CallEdge(caller=c, callee=cal) for c, cal in ts_calls]
            return _FileEntry(
                path=rel,
                mtime_ns=st_mtime_ns,
                size=st_size,
                symbols=ts_symbols,
                imports=ts_imports,
                tokens=ts_tokens,
                calls=calls,
            )

        # Fallback regex path: skip files larger than 512 KB to avoid holding
        # the GIL for an extended time (which freezes the HTTP server).
        if st_size > 512 * 1024:
            return _FileEntry(path=rel, mtime_ns=st_mtime_ns, size=st_size, symbols=[], imports=[], tokens=[], calls=[])

        text = ""
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        symbols: List[str] = []
        imports: List[str] = []

        symbol_patterns = [
            r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
            r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[A-Za-z_][A-Za-z0-9_<>\[\]]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        ]
        import_patterns = [
            r"^\s*import\s+([A-Za-z0-9_.*{},\s]+)\s+from\s+['\"]([^'\"]+)['\"]",
            r"^\s*from\s+([A-Za-z0-9_\.]+)\s+import\s+(.+)$",
            r"^\s*#include\s+[<\"]([^>\"]+)[>\"]",
            r"^\s*using\s+([A-Za-z0-9_:.]+)\s*;",
            r"^\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
        ]
        def_patterns = [
            r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)",
            r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[A-Za-z_][A-Za-z0-9_<>\[\]]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{?\s*$",
        ]
        call_edges: List[_CallEdge] = []
        current_caller = ""
        for line in text.splitlines():
            matched_symbol = False
            for pat in symbol_patterns:
                m = re.search(pat, line)
                if m:
                    name = (m.group(1) or "").strip()
                    if name:
                        symbols.append(name)
                    matched_symbol = True
                    break
            for pat in import_patterns:
                m = re.search(pat, line)
                if m:
                    g = " ".join((x or "").strip() for x in m.groups()).strip()
                    if g:
                        imports.append(g)
                    break
            def_name = ""
            for pat in def_patterns:
                m = re.search(pat, line)
                if m:
                    def_name = (m.group(1) or "").strip()
                    break
            if def_name:
                current_caller = def_name
            for cm in _CALL_SITE_RE.finditer(line):
                callee = cm.group(1)
                if not callee or callee in _CALL_KEYWORDS:
                    continue
                if matched_symbol and callee == def_name:
                    continue
                call_edges.append(_CallEdge(caller=current_caller, callee=callee))
        symbols = list(dict.fromkeys(symbols))[:120]
        imports = list(dict.fromkeys(imports))[:120]
        seen_edges: Set[Tuple[str, str]] = set()
        calls: List[_CallEdge] = []
        for c in call_edges:
            key = (c.caller, c.callee)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            calls.append(c)
            if len(calls) >= 400:
                break

        token_set: Set[str] = set()
        for t in _split_words(rel):
            token_set.add(t)
        for s in symbols:
            for t in _split_words(s):
                token_set.add(t)
        for imp in imports:
            for t in _split_words(imp):
                token_set.add(t)
        tokens = sorted(token_set)[:300]
        return _FileEntry(
            path=rel,
            mtime_ns=st_mtime_ns,
            size=st_size,
            symbols=symbols,
            imports=imports,
            tokens=tokens,
            calls=calls,
        )

    def refresh_index(self, force: bool = False, timeout_ms: Optional[int] = None) -> Dict[str, Any]:
        t0 = _now_ts()
        budget_s = None
        try:
            if timeout_ms is not None:
                v = int(timeout_ms)
                if v > 0:
                    budget_s = v / 1000.0
        except Exception:
            budget_s = None

        root = self.workspace_root
        if not root.is_dir():
            return {"success": False, "error": f"workspace does not exist: {root}"}

        # Phase 1 (no lock): walk filesystem
        self._refresh_progress_phase = "scanning"
        self._refresh_progress_total = 0
        self._refresh_progress_done = 0
        self._refresh_checkpointed_done = 0
        def _scan_progress(count: int) -> None:
            self._refresh_progress_done = count
            self._emit_status_snapshot("scanning")
        scanned, _discovery_timed_out = self._iter_code_files(deadline_ts=None, progress_cb=_scan_progress)
        self._refresh_progress_done = len(scanned)
        self._emit_status_snapshot("scanning")
        self._refresh_progress_phase = ""

        with self._lock:
            base_files = dict(self.files)

        # Phase 2 (no lock): stat + compare
        to_parse: List[Tuple[Path, str, int, int, bool]] = []
        seen_rel: Set[str] = set()
        processed = 0
        unchanged = 0
        for p in scanned:
            try:
                rel = str(p.relative_to(root)).replace("\\", "/")
                st = p.stat()
            except Exception:
                continue
            processed += 1
            seen_rel.add(rel)
            old = base_files.get(rel)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            size = int(st.st_size)
            if (
                (not force)
                and old is not None
                and old.mtime_ns == mtime_ns
                and old.size == size
            ):
                unchanged += 1
                continue
            is_new = old is None
            to_parse.append((p, rel, mtime_ns, size, is_new))

        deleted_candidates = 0
        if not force and base_files:
            for rel in base_files.keys():
                if rel not in seen_rel:
                    deleted_candidates += 1

        if base_files and (not to_parse) and deleted_candidates == 0 and not force:
            self.last_index_at = _now_ts()
            self._refresh_progress_phase = "done"
            self._refresh_progress_total = len(scanned)
            self._refresh_progress_done = len(scanned)
            self._refresh_checkpointed_done = len(self.files)
            self._emit_status_snapshot("done")
            self._ensure_embedding_provider()
            return {
                "success": True,
                "force": False,
                "workspace_root": str(root),
                "files_total": len(self.files),
                "scanned": len(scanned),
                "processed": processed,
                "added": 0,
                "updated": 0,
                "unchanged": unchanged,
                "deleted": 0,
                "timed_out": False,
                "stale": False,
                "elapsed_ms": int((_now_ts() - t0) * 1000),
                "index_path": str(self.index_path),
            }

        # Phase 3 (no lock): parse changed files with a thread pool.
        # When running in a child process (subprocess mode) this gives
        # true parallelism without GIL contention; when running in the
        # main process the cooperative _yield_if_requested() after each
        # file keeps HTTP handlers responsive.
        parsed_entries: Dict[str, _FileEntry] = {}
        timed_out = False
        parse_deadline = (_now_ts() + budget_s) if budget_s is not None else None
        if to_parse:
            self._refresh_progress_total = len(to_parse)
            self._refresh_progress_phase = "indexing"
            self._refresh_progress_done = 0
            self._refresh_checkpointed_done = len(base_files)
            checkpoint_batch: Dict[str, _FileEntry] = {}
            checkpoint_batch_count = 0
            checkpoint_last_saved_at = _now_ts()
            workers = max(2, (os.cpu_count() or 4) // 2)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_info: Dict[Any, Tuple[str, bool]] = {}
                for p, rel, mtime_ns, size, is_new in to_parse:
                    fut = executor.submit(self._parse_file, p, rel, mtime_ns, size)
                    future_to_info[fut] = (rel, is_new)
                    if parse_deadline is not None and _now_ts() >= parse_deadline:
                        timed_out = True
                        break
                for fut in as_completed(future_to_info):
                    rel, is_new = future_to_info[fut]
                    try:
                        entry = fut.result()
                    except Exception:
                        self._refresh_progress_done += 1
                        continue
                    parsed_entries[rel] = entry
                    checkpoint_batch[rel] = entry
                    checkpoint_batch_count += 1
                    self._refresh_progress_done += 1
                    should_checkpoint = checkpoint_batch_count >= 100
                    if not should_checkpoint and (_now_ts() - checkpoint_last_saved_at) >= 2.0:
                        should_checkpoint = True
                    if should_checkpoint:
                        checkpoint_ts = _now_ts()
                        self._save_checkpoint_batch(checkpoint_batch, checkpoint_ts=checkpoint_ts)
                        with self._lock:
                            next_checkpointed = dict(self.files)
                            next_checkpointed.update(checkpoint_batch)
                            self.files = next_checkpointed
                            self.last_index_at = checkpoint_ts
                        self._refresh_checkpointed_done = len(self.files)
                        self._index_checkpointed_done = max(
                            self._index_checkpointed_done,
                            self._refresh_checkpointed_done,
                        )
                        checkpoint_batch = {}
                        checkpoint_batch_count = 0
                        checkpoint_last_saved_at = checkpoint_ts
                    self._yield_if_requested()
            if checkpoint_batch:
                checkpoint_ts = _now_ts()
                self._save_checkpoint_batch(checkpoint_batch, checkpoint_ts=checkpoint_ts)
                with self._lock:
                    next_checkpointed = dict(self.files)
                    next_checkpointed.update(checkpoint_batch)
                    self.files = next_checkpointed
                    self.last_index_at = checkpoint_ts
                self._refresh_checkpointed_done = len(self.files)
                self._index_checkpointed_done = max(
                    self._index_checkpointed_done,
                    self._refresh_checkpointed_done,
                )

        # Phase 4 (lock): commit results
        self._refresh_progress_phase = "saving"
        index_existed_before_refresh = self.index_path.is_file()
        next_files: Dict[str, _FileEntry] = {}
        added = 0
        updated = 0
        deleted = 0
        changed = False
        should_save = False
        with self._lock:
            next_files = dict(self.files)
            for rel, entry in parsed_entries.items():
                if rel not in base_files:
                    added += 1
                else:
                    updated += 1
                next_files[rel] = entry

            if not timed_out:
                for rel in list(next_files.keys()):
                    if rel not in seen_rel:
                        deleted += 1
                        next_files.pop(rel, None)
                changed = (
                    added > 0 or updated > 0 or deleted > 0
                    or force or len(next_files) != len(self.files)
                )
            else:
                changed = added > 0 or updated > 0 or force or len(next_files) != len(self.files)

            self.files = next_files
            self.last_index_at = _now_ts()
            self._index_expected_total = (
                len(next_files) if not timed_out else max(self._index_expected_total, len(next_files))
            )
            self._index_checkpointed_done = len(next_files)
            should_save = changed or (not index_existed_before_refresh) or (timed_out and (added > 0 or updated > 0))

        # Save outside the lock so HTTP handler threads (status(), search())
        # are not blocked during SQLite writes. _save() opens its own
        # connection (WAL mode allows concurrent readers).
        if should_save:
            self._save()
        self._refresh_progress_phase = ""
        self._refresh_progress_total = 0
        self._refresh_progress_done = 0
        self._refresh_checkpointed_done = 0

        self._ensure_embedding_provider()

        return {
            "success": True,
            "force": bool(force),
            "workspace_root": str(root),
            "files_total": len(self.files),
            "scanned": len(scanned),
            "processed": processed,
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "deleted": deleted,
            "timed_out": timed_out,
            "stale": timed_out,
            "elapsed_ms": int((_now_ts() - t0) * 1000),
            "index_path": str(self.index_path),
        }

    def _get_embedding_index(self) -> Any:
        if self._embedding_index is None:
            from .embedding import FileEmbeddingIndex
            self._embedding_index = FileEmbeddingIndex(self.storage_dir)
        return self._embedding_index

    def _ensure_embedding_provider(self) -> None:
        if getattr(self, "_embedding_provider_loading", False):
            return

        ep = self._embedding_provider
        if ep is not None and ep.available:
            if len(self.files) > 0:
                self._get_embedding_index()
                self.build_embeddings()
            return

        self._get_embedding_index()

        def _init() -> None:
            try:
                provider = self._get_embedding_index().initialize_provider()
                with self._lock:
                    self._embedding_provider = provider
                if provider.available and len(self.files) > 0:
                    self.build_embeddings()
            except Exception as e:
                try:
                    import logging
                    from ..config.app_info import get_app_logger_root
                    logging.getLogger(f"{get_app_logger_root()}.embedding").warning(
                        "Embedding provider initialization failed: %s", e
                    )
                except Exception:
                    pass
            finally:
                self._embedding_provider_loading = False

        self._embedding_provider_loading = True
        threading.Thread(target=_init, daemon=True, name="embedding-init").start()

    def initialize_embedding_provider(self) -> Optional[EmbeddingProvider]:
        with self._lock:
            if self._embedding_provider is not None:
                return self._embedding_provider
            self._embedding_provider = self._get_embedding_index().initialize_provider()
            return self._embedding_provider

    def build_embeddings(self) -> Dict[str, Any]:
        with self._lock:
            ep = self._embedding_provider
            if ep is None or not ep.available:
                return {"success": False, "error": "No embedding provider initialized"}

            texts: Dict[str, str] = {}
            for rel, e in self.files.items():
                parts: List[str] = [rel]
                parts.extend(e.symbols[:20])
                texts[rel] = " ".join(parts)

            result = self._get_embedding_index().index_files(texts, provider=ep)

            chunks: List[Dict[str, str]] = []
            for rel, e in self.files.items():
                for sym in e.symbols[:60]:
                    kind = _guess_symbol_kind(rel, sym)
                    chunk_text = f"{rel}:{kind} {sym}"
                    chunks.append({
                        "file_rel": rel,
                        "chunk_name": sym,
                        "chunk_kind": kind,
                        "chunk_text": chunk_text,
                    })
            if chunks:
                chunk_result = self._get_embedding_index().index_chunks(chunks, provider=ep)
                result["chunks_indexed"] = chunk_result.get("indexed", 0)

            return result

    def embedding_status(self) -> Dict[str, Any]:
        ep = self._embedding_provider
        return {
            "success": True,
            "provider_name": ep.provider_name if ep else "none",
            "available": ep.available if ep else False,
            "has_embeddings": self._get_embedding_index().has_embeddings(),
            "files_indexed": len(self.files),
        }

    def status(self) -> Dict[str, Any]:
        # Read progress from the subprocess's status file whenever one
        # exists (alive or just-exited), so the count never drops to 0
        # during the brief window between proc.join() and _load().
        if self._subprocess is not None:
            st = self._read_status_file()
            if st is not None:
                raw_phase = str(st.get("phase", "scanning") or "scanning")
                phase = "scanning" if raw_phase == "starting" else raw_phase
                total = int(st.get("progress_total", 0) or 0)
                done = int(st.get("progress_done", 0) or 0)
                expected_total = int(st.get("expected_total", 0) or 0)
                checkpointed_done = int(st.get("checkpointed_done", 0) or 0)
                if phase == "":
                    # Idle: report the real deduplicated indexed count. Fall back to
                    # the worker's last checkpointed count only if we have no loaded
                    # files yet (brief window between proc.join() and _load()).
                    display = len(self.files) or checkpointed_done
                elif phase in ("scanning", "indexing"):
                    display = max(
                        self._index_expected_total, expected_total, checkpointed_done, len(self.files)
                    )
                else:
                    display = total if phase == "done" else done
                return {
                    "success": True,
                    "workspace_root": str(self.workspace_root) if self.workspace_root else "",
                    "index_path": str(self.index_path) if self.index_path else "",
                    "files_total": display,
                    "last_index_at": self.last_index_at,
                    "refresh_phase": phase,
                    "refresh_progress_total": total,
                    "refresh_progress_done": done,
                    "refresh_progress_percent": self._compute_refresh_progress_percent(
                        phase,
                        total,
                        done,
                        expected_total=expected_total,
                    ),
                }

        phase = self._refresh_progress_phase
        if phase == "scanning":
            files_display = self._refresh_progress_done
        elif phase == "indexing":
            files_display = self._refresh_progress_done
        else:
            # Idle: report the real deduplicated indexed count, never the
            # (monotonic, scan-time) expected total.
            files_display = len(self.files)
        return {
            "success": True,
            "workspace_root": str(self.workspace_root) if self.workspace_root else "",
            "index_path": str(self.index_path) if self.index_path else "",
            "files_total": files_display,
            "last_index_at": self.last_index_at,
            "refresh_phase": phase,
            "refresh_progress_total": self._refresh_progress_total,
            "refresh_progress_done": self._refresh_progress_done,
            "refresh_progress_percent": self._compute_refresh_progress_percent(
                phase,
                self._refresh_progress_total,
                self._refresh_progress_done,
                expected_total=max(self._index_expected_total, self._index_checkpointed_done),
            ),
        }

    def search(
        self,
        query: str,
        max_files: int = 12,
        auto_refresh: bool = True,
        refresh_timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        q = str(query or "").strip()
        if not q:
            return {"success": False, "error": "query must not be empty"}
        refresh_result: Optional[Dict[str, Any]] = None
        if auto_refresh:
            refresh_result = self.refresh_index(force=False, timeout_ms=refresh_timeout_ms)

        with self._lock:
            files_items = list(self.files.items())
            status_snapshot = self.status()

        q_tokens = _split_words(q)
        q_l = q.lower()
        if not q_tokens:
            q_tokens = [q_l]

        doc_count = len(files_items)
        if doc_count == 0:
            return {
                "success": True,
                "query": q,
                "query_tokens": q_tokens,
                "total_matches": 0,
                "candidates": [],
                "index_status": status_snapshot,
                "stale": bool(refresh_result.get("timed_out")) if isinstance(refresh_result, dict) else False,
            }

        doc_token_lists: List[Tuple[str, List[str]]] = []
        doc_lengths: List[int] = []
        for rel, e in files_items:
            doc_token_lists.append((rel, _derive_tokens(rel, e)))
            doc_lengths.append(len(_derive_tokens(rel, e)))
        avgdl = sum(doc_lengths) / max(1, len(doc_lengths))

        doc_freq: Dict[str, int] = {}
        for _, tokens in doc_token_lists:
            seen: Set[str] = set()
            for t in tokens:
                if t not in seen:
                    doc_freq[t] = doc_freq.get(t, 0) + 1
                    seen.add(t)

        k1 = 1.5
        b = 0.75
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for (rel, e), (_, doc_tokens), doc_len in zip(files_items, doc_token_lists, doc_lengths):
            bm25 = 0.0
            for t in q_tokens:
                df = doc_freq.get(t, 0)
                if df == 0:
                    continue
                idf = math.log((doc_count - df + 0.5) / (df + 0.5) + 1.0)
                tf = sum(1 for dt in doc_tokens if dt == t)
                if tf == 0:
                    continue
                numerator = tf * (k1 + 1.0)
                denominator = tf + k1 * (1.0 - b + b * doc_len / max(1, avgdl))
                bm25 += idf * numerator / denominator

            reasons: List[str] = []
            path_l = rel.lower()
            # Path bonus: exact query in path is a strong signal
            if q_l in path_l:
                bm25 += 8.0
                reasons.append("path_contains_query")
            # Symbol/import boost on top of BM25
            sym_boost = 0.0
            for t in q_tokens:
                if any(t in s.lower() for s in e.symbols[:80]):
                    sym_boost += 4.0
                if any(t in imp.lower() for imp in e.imports[:80]):
                    sym_boost += 1.5
            bm25 += sym_boost
            total_score = bm25

            if total_score <= 0:
                continue
            if sym_boost > 0:
                reasons.append(f"symbol_boost={round(sym_boost, 1)}")
            scored.append(
                (
                    total_score,
                    {
                        "path": rel,
                        "score": round(total_score, 2),
                        "reasons": reasons,
                        "symbols": e.symbols[:12],
                        "imports": e.imports[:8],
                    },
                )
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [x[1] for x in scored[: max(1, int(max_files or 12))]]

        ep = self._embedding_provider
        if ep is None:
            self._ensure_embedding_provider()
        ep = self._embedding_provider
        has_emb = ep is not None and ep.available and self._get_embedding_index().has_embeddings()
        if has_emb and len(scored) > 1:
            candidate_rels = [x[1]["path"] for x in scored[:50]]
            stored_embs = self._get_embedding_index().get_embeddings_batch(candidate_rels)
            if stored_embs:
                query_vecs = ep.embed([q[:4096]])
                if query_vecs and len(query_vecs) > 0:
                    qv = query_vecs[0]
                    emb_weight = 0.3
                    rescored: List[Tuple[float, Dict[str, Any]]] = []
                    for _, info in scored[:50]:
                        rel = info["path"]
                        emb = stored_embs.get(rel)
                        bm25_s = float(info.get("score", 0.0))
                        bm25_norm = bm25_s / max(1.0, bm25_s + 20.0)
                        if emb is not None:
                            cos_sim = _cosine_similarity(qv, emb)
                            info["embedding_score"] = round(cos_sim, 3)
                        else:
                            cos_sim = 0.0
                        hybrid = (1.0 - emb_weight) * bm25_norm + emb_weight * cos_sim
                        info["score"] = round(hybrid, 2)
                        if hybrid > 0:
                            rescored.append((hybrid, info))
                    if rescored:
                        rescored.sort(key=lambda x: x[0], reverse=True)
                        top = [x[1] for x in rescored[: max(1, int(max_files or 12))]]

        has_chunks = ep is not None and ep.available and self._get_embedding_index().has_chunk_embeddings()
        if has_chunks and q_tokens:
            chunk_results = self._get_embedding_index().chunk_search(
                query=q,
                candidate_files=[info["path"] for info in top],
                provider=ep,
                top_k=max(1, int(max_files or 12)),
            )
            if chunk_results:
                chunk_boost: Dict[str, Dict[str, Any]] = {}
                for cr in chunk_results:
                    path = cr["path"]
                    if path not in chunk_boost:
                        chunk_boost[path] = cr
                    else:
                        chunk_boost[path]["score"] = max(
                            chunk_boost[path]["score"], cr["score"]
                        )
                        existing_chunks = chunk_boost[path].get("chunks", [])
                        existing_chunks.extend(cr.get("chunks", []))
                        chunk_boost[path]["chunks"] = sorted(
                            existing_chunks, key=lambda x: -x["score"]
                        )[:5]
                for info in top:
                    if info["path"] in chunk_boost:
                        info["chunk_score"] = chunk_boost[info["path"]]["score"]
                        info["matched_chunks"] = chunk_boost[info["path"]]["chunks"]
                        info["score"] = round(float(info.get("score", 0.0)) + chunk_boost[info["path"]]["score"] * 0.5, 2)
                top.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)

        out = {
            "success": True,
            "query": q,
            "query_tokens": q_tokens,
            "total_matches": len(scored),
            "candidates": top,
            "index_status": status_snapshot,
            "stale": bool(refresh_result.get("timed_out")) if isinstance(refresh_result, dict) else False,
        }
        if isinstance(refresh_result, dict):
            out["index_refresh"] = refresh_result
        return out

    def call_graph(
        self,
        symbol: str,
        direction: str = "both",
        max_results: int = 50,
        auto_refresh: bool = True,
        refresh_timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        name = str(symbol or "").strip()
        if not name:
            return {"success": False, "error": "symbol must not be empty"}
        dir_l = str(direction or "both").strip().lower()
        if dir_l not in ("callees", "callers", "both"):
            dir_l = "both"
        cap = max(1, int(max_results or 50))

        refresh_result: Optional[Dict[str, Any]] = None
        if auto_refresh:
            refresh_result = self.refresh_index(force=False, timeout_ms=refresh_timeout_ms)

        with self._lock:
            status_snapshot = self.status()

        callees: List[Dict[str, Any]] = []
        callers: List[Dict[str, Any]] = []

        conn = self._connect()
        try:
            if dir_l in ("callees", "both"):
                rows = conn.execute(
                    "SELECT file_rel, caller, callee FROM calls WHERE caller = ? ORDER BY ord LIMIT ?",
                    (name, cap),
                ).fetchall()
                callees = [
                    {"file": str(r[0]), "caller": str(r[1]), "callee": str(r[2])}
                    for r in rows
                ]
            if dir_l in ("callers", "both"):
                rows = conn.execute(
                    "SELECT file_rel, caller, callee FROM calls WHERE callee = ? ORDER BY ord LIMIT ?",
                    (name, cap),
                ).fetchall()
                callers = [
                    {"file": str(r[0]), "caller": str(r[1]), "callee": str(r[2])}
                    for r in rows
                ]
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out: Dict[str, Any] = {
            "success": True,
            "symbol": name,
            "direction": dir_l,
            "index_status": status_snapshot,
        }
        if dir_l in ("callees", "both"):
            out["callees"] = callees[:cap]
            out["callees_total"] = len(callees)
        if dir_l in ("callers", "both"):
            out["callers"] = callers[:cap]
            out["callers_total"] = len(callers)
        if isinstance(refresh_result, dict):
            out["stale"] = bool(refresh_result.get("timed_out"))
        return out

# ---------------------------------------------------------------------------
# Filename search for the ``@``-file-reference feature (TUI + GUI)
# ---------------------------------------------------------------------------
#
# The ``@<name>`` quick file reference needs a *filename* lookup over ALL
# workspace files (not just code files), independent of the symbol index
# above. To keep typing responsive we cache the relative-path listing per
# workspace root for a short TTL and re-walk only when it expires.

if _WATCHDOG_AVAILABLE:

    class _ProjectFileWatcher(FileSystemEventHandler):
        def __init__(self, index: "ProjectContextIndex") -> None:
            super().__init__()
            self._index = index
            self._batch_lock = threading.Lock()
            self._batch: Dict[str, Optional[str]] = {}
            self._timer: Optional[threading.Timer] = None
            self._flush_delay = 0.3

        def _schedule_flush(self) -> None:
            with self._batch_lock:
                if self._timer is not None:
                    return
                self._timer = threading.Timer(self._flush_delay, self._flush_batch)
                self._timer.daemon = True
                self._timer.start()

        def _flush_batch(self) -> None:
            batch: Dict[str, Optional[str]] = {}
            with self._batch_lock:
                batch = self._batch
                self._batch = {}
                self._timer = None
            if not batch:
                return
            index = self._index
            root = index.workspace_root
            if not root.is_dir():
                return
            with index._lock:
                files = dict(index.files)
            to_parse: List[Tuple[Path, str, int, int]] = []
            to_delete: List[str] = []
            for rel, kind in batch.items():
                try:
                    p = root / rel
                except Exception:
                    continue
                if kind is None:
                    to_delete.append(rel)
                elif p.is_file() and p.suffix.lower() in _DEFAULT_CODE_EXTS:
                    try:
                        st = p.stat()
                        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                        size = int(st.st_size)
                    except Exception:
                        continue
                    old = files.get(rel)
                    if old is None or old.mtime_ns != mtime_ns or old.size != size:
                        to_parse.append((p, rel, mtime_ns, size))
            if to_parse or to_delete:
                with index._lock:
                    nf = dict(index.files)
                    for rel in to_delete:
                        nf.pop(rel, None)
                    batch_len = len(to_parse)
                    for i, (p, rel, mtime_ns, size_val) in enumerate(to_parse):
                        if batch_len > 10 and i % 5 == 0:
                            time.sleep(0.01)
                        try:
                            entry = index._parse_file(p, rel, mtime_ns, size_val)
                        except Exception:
                            continue
                        nf[rel] = entry
                    index.files = nf
                    index.last_index_at = _now_ts()
                    index._refresh_progress_phase = ""
                    index._refresh_progress_total = 0
                    index._refresh_progress_done = 0
                # Save outside the lock so HTTP handler threads are not blocked.
                index._save()

        def _on_event(self, rel: str, kind: Optional[str]) -> None:
            try:
                root_s = str(self._index.workspace_root).replace("\\", "/")
            except Exception:
                return
            rel = _normalize_watch_rel(rel, root_s)
            if not rel or rel.startswith(".") or "/." in rel:
                return
            parts = rel.split("/")
            for part in parts[:-1]:
                if part.lower() in _DEFAULT_EXCLUDE_DIRS or _is_venv_dir(os.path.join(root_s, *parts[:parts.index(part) + 1]).replace("/", os.sep)):
                    return
            if not any(rel.endswith(ext) for ext in _DEFAULT_CODE_EXTS):
                return
            with self._batch_lock:
                existing = self._batch.get(rel)
                if kind is None:
                    self._batch[rel] = None
                elif existing is not None:
                    pass
                else:
                    self._batch[rel] = kind
            self._schedule_flush()

        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                self._on_event(event.src_path, "created")

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                self._on_event(event.src_path, "modified")

        def on_deleted(self, event: Any) -> None:
            if not event.is_directory:
                self._on_event(event.src_path, None)

        def on_moved(self, event: Any) -> None:
            if not event.is_directory:
                if hasattr(event, "dest_path"):
                    self._on_event(event.dest_path, "created")
                if hasattr(event, "src_path"):
                    self._on_event(event.src_path, None)

else:
    _ProjectFileWatcher = None


def _normalize_watch_rel(rel: str, root_s: str) -> str:
    """Rebase a watchdog event path onto the workspace root as a POSIX rel path.

    watchdog emits absolute ``src_path``/``dest_path`` values, while index
    keys are workspace-relative. Storing an absolute path as a key would
    index the same file twice (absolute + relative), inflating the indexed
    file count and the status-bar ``Index: N files`` figure.
    """
    try:
        rel = str(rel or "").replace("\\", "/")
        if os.path.isabs(rel):
            rel = os.path.relpath(rel, root_s).replace("\\", "/")
        return rel
    except Exception:
        return ""


def _start_file_watcher(index: "ProjectContextIndex") -> Optional[Any]:
    if not _WATCHDOG_AVAILABLE:
        return None
    root = index.workspace_root
    if not root.is_dir():
        return None
    try:
        handler = _ProjectFileWatcher(index)
        observer = Observer()
        observer.schedule(handler, str(root), recursive=True)
        observer.start()
        return observer
    except Exception:
        return None


def _stop_file_watcher(observer: Optional[Any]) -> None:
    if observer is None:
        return
    try:
        observer.stop()
        observer.join(timeout=1)
    except Exception:
        pass


_FILE_LISTING_TTL_SECONDS: float = 5.0
_file_listing_cache: Dict[str, Tuple[float, List[str]]] = {}
_file_listing_lock = threading.RLock()
# Hard cap on how many files we keep cached so a huge monorepo can't blow up
# memory; the cap is generous and only matters for pathological trees.
_FILE_LISTING_MAX_FILES: int = 50000


def _is_venv_dir(dir_path: str) -> bool:
    try:
        return os.path.isfile(os.path.join(dir_path, "pyvenv.cfg"))
    except Exception:
        return False


_GITIGNORE_CACHE: Dict[str, Tuple[float, Any]] = {}
_GITIGNORE_CACHE_TTL: float = 30.0


def _load_gitignore(root: str) -> Any:
    root_key = str(Path(root).resolve())
    now = _now_ts()
    cached = _GITIGNORE_CACHE.get(root_key)
    if cached and (now - cached[0]) < _GITIGNORE_CACHE_TTL:
        return cached[1]

    try:
        import pathspec  # type: ignore[import-untyped]
    except ImportError:
        return None
    patterns: List[str] = []
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if ".gitignore" not in filenames:
            continue
        try:
            dir_prefix = Path(dirpath).resolve().relative_to(root_path).as_posix()
        except Exception:
            dir_prefix = ""
        if dir_prefix == ".":
            dir_prefix = ""
        try:
            with open(os.path.join(dirpath, ".gitignore"), "r", encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    negated = stripped.startswith("!")
                    raw = stripped[1:] if negated else stripped
                    anchored = raw.startswith("/")
                    body = raw[1:] if anchored else raw
                    if dir_prefix:
                        scoped = f"{'!' if negated else ''}{dir_prefix}/{body}"
                    else:
                        scoped = stripped
                    patterns.append(scoped)
        except Exception:
            pass
    if not patterns:
        _GITIGNORE_CACHE[root_key] = (now, None)
        return None
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    _GITIGNORE_CACHE[root_key] = (now, spec)
    return spec


def _is_gitignored(rel_path: str, spec: Any) -> bool:
    if spec is None:
        return False
    try:
        return bool(spec.match_file(rel_path.replace(os.sep, "/")))
    except Exception:
        return False


def _list_workspace_files(workspace_root: Path) -> List[str]:
    """Return workspace-relative POSIX paths for all non-excluded files.

    Cached per root for ``_FILE_LISTING_TTL_SECONDS`` so repeated keystrokes
    while typing ``@name`` don't re-walk the tree each time.
    """
    root = Path(workspace_root).resolve()
    key = str(root)
    now = _now_ts()
    with _file_listing_lock:
        cached = _file_listing_cache.get(key)
        if cached and (now - cached[0]) < _FILE_LISTING_TTL_SECONDS:
            return cached[1]

    rels: List[str] = []
    if root.is_dir():
        root_s = str(root)
        git_spec = _load_gitignore(root_s)
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if str(d or "").lower() not in _DEFAULT_EXCLUDE_DIRS
                and not _is_venv_dir(os.path.join(dirpath, str(d)))
            ]
            dirnames[:] = [d for d in dirnames if not _is_gitignored(
                os.path.relpath(os.path.join(dirpath, d), root_s), git_spec
            )]
            for fn in filenames:
                if str(fn or "").startswith("."):
                    continue
                full = Path(dirpath) / str(fn)
                try:
                    rel = full.resolve().relative_to(root).as_posix()
                except Exception:
                    continue
                if _is_gitignored(rel, git_spec):
                    continue
                rels.append(rel)
                if len(rels) >= _FILE_LISTING_MAX_FILES:
                    truncated = True
                    break
            if truncated:
                break

    with _file_listing_lock:
        _file_listing_cache[key] = (now, rels)
    return rels


def _score_filename_match(query_l: str, rel: str) -> float:
    """Score a relative path against a lowercase query for @ ranking.

    Favours matches on the basename, then prefix matches, then substring,
    then subsequence (fuzzy) matches. Shorter paths win ties so the closest
    file surfaces first.
    """
    if not query_l:
        # Empty query: rank purely by shallowness/length (used right after
        # the bare ``@`` before the user types anything).
        return 1.0 / (1.0 + len(rel))
    rel_l = rel.lower()
    base_l = rel_l.rsplit("/", 1)[-1]
    score = 0.0
    if base_l == query_l:
        score += 100.0
    elif base_l.startswith(query_l):
        score += 60.0
    elif query_l in base_l:
        score += 40.0
    if rel_l.startswith(query_l):
        score += 20.0
    elif query_l in rel_l:
        score += 12.0
    if score == 0.0:
        # Subsequence (fuzzy) fallback: all query chars appear in order.
        it = iter(rel_l)
        if all(ch in it for ch in query_l):
            score += 5.0
        else:
            return 0.0
    # Prefer shorter / shallower paths on ties.
    score += 1.0 / (1.0 + len(rel_l))
    return score


def search_workspace_files(
    workspace_root: Path,
    query: str,
    max_results: int = 10,
) -> List[str]:
    """Return up to ``max_results`` workspace-relative paths matching ``query``.

    Used by the ``@<name>`` quick file-reference autocompletion in both the
    TUI and the GUI. ``query`` is the partial filename the user has typed
    after ``@`` (may be empty to list the shallowest files). Matching is
    case-insensitive and filename-focused.
    """
    cap = max(1, int(max_results or 10))
    q_l = str(query or "").strip().lower()
    files = _list_workspace_files(workspace_root)
    scored: List[Tuple[float, str]] = []
    for rel in files:
        s = _score_filename_match(q_l, rel)
        if s > 0.0:
            scored.append((s, rel))
    # Sort by score desc, then path asc for stable, predictable ordering.
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [rel for _, rel in scored[:cap]]

