from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname

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
    "dist",
    "build",
    "out",
    ".idea",
    ".vscode",
    get_app_config_dirname(),
    "__pycache__",
    ".pytest_cache",
}


def _now_ts() -> float:
    return time.time()


def _normalize_token(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", (s or "").strip().lower())


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
        self._load()

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
            self.workspace_root = root
            if str(target_storage) != str(self.storage_dir):
                self.storage_dir = target_storage
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                self.index_path = self.storage_dir / "project_context_index.db"
            self.files = {}
            self.last_index_at = 0.0
            self._load()

    def _connect(self) -> sqlite3.Connection:
        # Caller controls synchronization. A fresh connection per operation
        # keeps the index thread-safe under the class-level RLock without
        # juggling SQLite's per-connection thread affinity.
        conn = sqlite3.connect(str(self.index_path))
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
            for file_rel, token in conn.execute(
                "SELECT file_rel, token FROM tokens"
            ):
                e = entries.get(str(file_rel))
                if e is not None:
                    e.tokens.append(str(token))
            for file_rel, caller, callee in conn.execute(
                "SELECT file_rel, caller, callee FROM calls ORDER BY file_rel, ord"
            ):
                e = entries.get(str(file_rel))
                if e is not None:
                    e.calls.append(_CallEdge(caller=str(caller), callee=str(callee)))
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
        # Caller controls synchronization. Keep this helper lock-free.
        conn = self._connect()
        try:
            self._create_schema(conn)
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM imports")
            conn.execute("DELETE FROM tokens")
            conn.execute("DELETE FROM calls")
            conn.executemany(
                "INSERT OR REPLACE INTO files (rel, path, mtime_ns, size) VALUES (?, ?, ?, ?)",
                [(rel, e.path, e.mtime_ns, e.size) for rel, e in self.files.items()],
            )
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
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _iter_code_files(self, deadline_ts: Optional[float] = None) -> Tuple[List[Path], bool]:
        out: List[Path] = []
        root = self.workspace_root
        if not root.is_dir():
            return out, False
        timed_out = False
        root_s = str(root)
        for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
            if deadline_ts is not None and _now_ts() >= deadline_ts:
                timed_out = True
                break
            # Prune excluded directories before descending to keep traversal cheap.
            dirnames[:] = [d for d in dirnames if str(d or "").lower() not in _DEFAULT_EXCLUDE_DIRS]
            for fn in filenames:
                if deadline_ts is not None and _now_ts() >= deadline_ts:
                    timed_out = True
                    break
                p = Path(dirpath) / str(fn)
                if p.suffix.lower() not in _DEFAULT_CODE_EXTS:
                    continue
                out.append(p)
            if timed_out:
                break
        return out, timed_out

    def _parse_file(self, p: Path, rel: str, st_mtime_ns: int, st_size: int) -> _FileEntry:
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
        # Patterns that introduce a new callable symbol (the enclosing
        # "caller" for any call sites that follow it).
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
            # Track the enclosing callable so call edges can be attributed to a
            # caller. Only definition-like symbols become callers (a class
            # declaration is not a caller; methods/functions are).
            def_name = ""
            for pat in def_patterns:
                m = re.search(pat, line)
                if m:
                    def_name = (m.group(1) or "").strip()
                    break
            if def_name:
                current_caller = def_name
            # Extract call sites: ``callee(`` occurrences on the line, skipping
            # language keywords and the definition itself.
            for cm in _CALL_SITE_RE.finditer(line):
                callee = cm.group(1)
                if not callee or callee in _CALL_KEYWORDS:
                    continue
                if matched_symbol and callee == def_name:
                    continue
                call_edges.append(_CallEdge(caller=current_caller, callee=callee))
        # de-dup while keeping order
        symbols = list(dict.fromkeys(symbols))[:120]
        imports = list(dict.fromkeys(imports))[:120]
        # de-dup call edges (caller, callee) while keeping order, capped.
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
        deadline = (_now_ts() + budget_s) if budget_s is not None else None

        with self._lock:
            root = self.workspace_root
            if not root.is_dir():
                return {"success": False, "error": f"workspace does not exist: {root}"}

            index_existed_before_refresh = self.index_path.is_file()
            scanned, discovery_timed_out = self._iter_code_files(deadline_ts=deadline)
            base_files = self.files
            next_files: Dict[str, _FileEntry] = dict(base_files)
            seen_rel: Set[str] = set()
            added = 0
            updated = 0
            unchanged = 0
            timed_out = bool(discovery_timed_out)
            processed = 0

            for p in scanned:
                if deadline is not None and _now_ts() >= deadline:
                    timed_out = True
                    break
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
                entry = self._parse_file(p, rel, mtime_ns, size)
                next_files[rel] = entry
                if old is None:
                    added += 1
                else:
                    updated += 1

            deleted = 0
            if not timed_out:
                for rel in list(next_files.keys()):
                    if rel not in seen_rel:
                        deleted += 1
                        next_files.pop(rel, None)

                changed = (
                    added > 0
                    or updated > 0
                    or deleted > 0
                    or force
                    or len(next_files) != len(base_files)
                )
            else:
                changed = added > 0 or updated > 0 or force or len(next_files) != len(base_files)

            self.files = next_files
            self.last_index_at = _now_ts()
            should_save = changed or (not index_existed_before_refresh) or (timed_out and (added > 0 or updated > 0))
            if should_save:
                self._save()

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

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "workspace_root": str(self.workspace_root),
                "index_path": str(self.index_path),
                "files_total": len(self.files),
                "last_index_at": self.last_index_at,
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
            # incremental refresh (non-force) keeps cost acceptable for M1.
            refresh_result = self.refresh_index(force=False, timeout_ms=refresh_timeout_ms)

        with self._lock:
            files_items = list(self.files.items())
            status_snapshot = self.status()

        q_tokens = _split_words(q)
        q_l = q.lower()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for rel, e in files_items:
            path_l = rel.lower()
            score = 0.0
            reasons: List[str] = []
            if q_l in path_l:
                score += 8.0
                reasons.append("path_contains_query")
            token_hits = 0
            for t in q_tokens:
                if t in path_l:
                    score += 3.0
                    token_hits += 1
                if t in e.tokens:
                    score += 2.0
                    token_hits += 1
                if any(t in s.lower() for s in e.symbols[:80]):
                    score += 4.0
                    token_hits += 1
                if any(t in imp.lower() for imp in e.imports[:80]):
                    score += 1.5
                    token_hits += 1
            if token_hits > 0:
                reasons.append(f"token_hits={token_hits}")
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "path": rel,
                        "score": round(score, 2),
                        "reasons": reasons,
                        "symbols": e.symbols[:12],
                        "imports": e.imports[:8],
                    },
                )
            )
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [x[1] for x in scored[: max(1, int(max_files or 12))]]
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
        """Return call-graph relationships for ``symbol``.

        ``direction``:
          - ``"callees"``: functions that ``symbol`` calls.
          - ``"callers"``: functions/files that call ``symbol``.
          - ``"both"`` (default): both of the above.

        Each edge entry carries the file it was observed in and the caller /
        callee names, enabling change-impact ("what calls X") and
        dependency ("what does X call") analysis.
        """
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
            files_items = list(self.files.items())
            status_snapshot = self.status()

        callees: List[Dict[str, Any]] = []
        callers: List[Dict[str, Any]] = []
        for rel, e in files_items:
            for c in e.calls:
                if dir_l in ("callees", "both") and c.caller == name and c.callee:
                    callees.append({"file": rel, "caller": c.caller, "callee": c.callee})
                if dir_l in ("callers", "both") and c.callee == name:
                    callers.append({"file": rel, "caller": c.caller, "callee": c.callee})

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

_FILE_LISTING_TTL_SECONDS: float = 5.0
_file_listing_cache: Dict[str, Tuple[float, List[str]]] = {}
_file_listing_lock = threading.RLock()
# Hard cap on how many files we keep cached so a huge monorepo can't blow up
# memory; the cap is generous and only matters for pathological trees.
_FILE_LISTING_MAX_FILES: int = 50000


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
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
            dirnames[:] = [
                d for d in dirnames if str(d or "").lower() not in _DEFAULT_EXCLUDE_DIRS
            ]
            for fn in filenames:
                if str(fn or "").startswith("."):
                    # Skip dotfiles; they are rarely the target of an @ pick
                    # and add noise to candidate lists.
                    continue
                full = Path(dirpath) / str(fn)
                try:
                    rel = full.resolve().relative_to(root).as_posix()
                except Exception:
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

