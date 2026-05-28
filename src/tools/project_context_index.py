from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname


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


def _split_words(s: str) -> List[str]:
    raw = re.split(r"[^A-Za-z0-9_]+", str(s or ""))
    out: List[str] = []
    for w in raw:
        t = _normalize_token(w)
        if len(t) >= 2:
            out.append(t)
    return out


@dataclass
class _FileEntry:
    path: str
    mtime_ns: int
    size: int
    symbols: List[str]
    imports: List[str]
    tokens: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "symbols": self.symbols,
            "imports": self.imports,
            "tokens": self.tokens,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "_FileEntry":
        return _FileEntry(
            path=str(d.get("path") or ""),
            mtime_ns=int(d.get("mtime_ns") or 0),
            size=int(d.get("size") or 0),
            symbols=[str(x) for x in (d.get("symbols") or []) if str(x).strip()],
            imports=[str(x) for x in (d.get("imports") or []) if str(x).strip()],
            tokens=[str(x) for x in (d.get("tokens") or []) if str(x).strip()],
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
        self.index_path = self.storage_dir / "project_context_index.json"
        self.files: Dict[str, _FileEntry] = {}
        self.last_index_at: float = 0.0
        self.version: int = 1
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
                self.index_path = self.storage_dir / "project_context_index.json"
            self.files = {}
            self.last_index_at = 0.0
            self._load()

    def _load(self) -> None:
        # Caller controls synchronization. Keep this helper lock-free.
        if not self.index_path.is_file():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            self.version = int(raw.get("version") or 1)
            self.last_index_at = float(raw.get("last_index_at") or 0.0)
            files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
            self.files = {}
            for rel, obj in files.items():
                if not isinstance(obj, dict):
                    continue
                e = _FileEntry.from_dict(obj)
                if e.path:
                    self.files[str(rel)] = e
        except Exception:
            self.files = {}
            self.last_index_at = 0.0

    def _save(self) -> None:
        # Caller controls synchronization. Keep this helper lock-free.
        payload = {
            "version": self.version,
            "workspace_root": str(self.workspace_root),
            "last_index_at": self.last_index_at,
            "files": {k: v.to_dict() for k, v in self.files.items()},
        }
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
        for line in text.splitlines():
            for pat in symbol_patterns:
                m = re.search(pat, line)
                if m:
                    name = (m.group(1) or "").strip()
                    if name:
                        symbols.append(name)
                    break
            for pat in import_patterns:
                m = re.search(pat, line)
                if m:
                    g = " ".join((x or "").strip() for x in m.groups()).strip()
                    if g:
                        imports.append(g)
                    break
        # de-dup while keeping order
        symbols = list(dict.fromkeys(symbols))[:120]
        imports = list(dict.fromkeys(imports))[:120]

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
                return {"success": False, "error": f"workspace 不存在: {root}"}

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
                self.files = next_files
                self.last_index_at = _now_ts()
                if changed:
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
            return {"success": False, "error": "query 不能为空"}
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

