"""
Experiential memory (internalized lessons and preferences).

Storage: Markdown files plus one manifest.json per scope (machine-readable) and INDEX.md (human-readable index).
No Chroma / embedding model is used to avoid first-load latency.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Callable

import yaml
from ...config.app_info import get_app_logger_root, get_app_name

_mem_log = logging.getLogger(f"{get_app_logger_root()}.memory")

# No heavy dependency; enabled by default. MemoryService only marks it unavailable if initialization raises.
MEMORY_AVAILABLE = True

MANIFEST_VERSION = 1
INDEX_HEADER = (
    "# Experiential Memory Index\n\n"
    f"This file is generated automatically by {get_app_name()} from `manifest.json`. It is readable, but do not edit the structure lines by hand.\n\n"
)


def _scope_hash(scope_key: str) -> str:
    raw = scope_key or "global"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _tier_expires_at(tier: str, now_ts: float) -> Optional[float]:
    if tier == "working":
        return now_ts + 72 * 3600
    if tier == "episodic":
        return now_ts + 30 * 24 * 3600
    if tier == "durable":
        return None
    return now_ts + 7 * 24 * 3600


def _query_tokens(query: str) -> List[str]:
    """Lightweight Chinese/English tokenization: split on whitespace/punctuation plus short-token filtering."""
    q = (query or "").strip()
    if not q:
        return []
    parts = re.split(r"[\s,，。;；、.!?？！\n\r\t]+", q)
    out: List[str] = []
    for p in parts:
        s = p.strip()
        if len(s) >= 2:
            out.append(s)
    if not out and len(q) >= 1:
        out = [q]
    # Deduplicate while preserving order.
    seen: Set[str] = set()
    uniq: List[str] = []
    for t in out:
        low = t.lower()
        if low not in seen:
            seen.add(low)
            uniq.append(t)
    return uniq


def _read_md_document(path: Path) -> Tuple[Dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return {}, raw
    fm_text = raw[3:end].strip()
    body = raw[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(fm_text) or {}
        if not isinstance(meta, dict):
            return {}, body
        return meta, body
    except Exception:
        return {}, raw


def _write_md_document(path: Path, meta: Dict[str, Any], body: str) -> None:
    fm = yaml.safe_dump(
        meta,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    text = f"---\n{fm}\n---\n\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class MemoryManager:
    """
    Experiential memory: directories are organized by scope_key; entries/*.md are the source of truth; manifest.json is the retrieval index; INDEX.md is for manual browsing.
    """

    def __init__(self, config_dir: str, embedding_model: str = ""):
        self.config_dir = Path(config_dir)
        self.memory_root = self.config_dir / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self._embedding_model_legacy = embedding_model  # Compatibility field for stats
        self._lock = threading.Lock()

    def _scopes_base(self) -> Path:
        d = self.memory_root / "scopes"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _scope_dir(self, scope_key: str) -> Path:
        sk = scope_key or "global"
        return self._scopes_base() / _scope_hash(sk)

    def _manifest_path(self, scope_key: str) -> Path:
        return self._scope_dir(scope_key) / "manifest.json"

    def _load_manifest(self, scope_key: str) -> Dict[str, Any]:
        p = self._manifest_path(scope_key)
        if not p.is_file():
            return {
                "version": MANIFEST_VERSION,
                "scope_key": scope_key or "global",
                "updated_at": time.time(),
                "entries": [],
            }
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid manifest root")
            entries = data.get("entries")
            if not isinstance(entries, list):
                data["entries"] = []
            data.setdefault("version", MANIFEST_VERSION)
            data.setdefault("scope_key", scope_key or "global")
            return data
        except Exception as e:
            _mem_log.warning("Failed to read manifest; using an empty manifest: %s", e)
            return {
                "version": MANIFEST_VERSION,
                "scope_key": scope_key or "global",
                "updated_at": time.time(),
                "entries": [],
            }

    def _save_manifest(self, scope_key: str, data: Dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        data["version"] = MANIFEST_VERSION
        data["scope_key"] = scope_key or "global"
        p = self._manifest_path(scope_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_index_md(scope_key, data)

    def _write_index_md(self, scope_key: str, data: Dict[str, Any]) -> None:
        scope_dir = self._scope_dir(scope_key)
        lines = [
            INDEX_HEADER,
            f"**Scope scope_key**: `{data.get('scope_key', '')}`\n\n",
            "| id | type | tier | title | summary | file |\n",
            "| --- | --- | --- | --- | --- | --- |\n",
        ]
        for e in data.get("entries") or []:
            if not isinstance(e, dict):
                continue
            eid = str(e.get("id", ""))[:8] + "…"
            mid = str(e.get("id", ""))
            mt = str(e.get("memory_type", ""))[:20]
            tier = str(e.get("tier", ""))[:12]
            title = str(e.get("title", ""))[:80].replace("|", "\\|")
            summ = str(e.get("summary", ""))[:120].replace("|", "\\|")
            rel = str(e.get("rel_path", "")).replace("|", "\\|")
            lines.append(f"| `{eid}` | {mt} | {tier} | {title} | {summ} | `{rel}` |\n")
        (scope_dir / "INDEX.md").write_text("".join(lines), encoding="utf-8")

    def _entry_path(self, scope_key: str, rel_path: str) -> Path:
        return self._scope_dir(scope_key) / rel_path

    def _purge_expired_unlocked(self) -> None:
        now = time.time()
        for manifest_path in self._scopes_base().glob("*/manifest.json"):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data.get("entries")
            if not isinstance(entries, list):
                continue
            scope_key = str(data.get("scope_key") or "global")
            kept: List[Dict[str, Any]] = []
            removed = False
            for e in entries:
                if not isinstance(e, dict):
                    continue
                exp = e.get("expires_at")
                if exp is not None:
                    try:
                        if float(exp) < now:
                            rel = str(e.get("rel_path", ""))
                            ep = self._entry_path(scope_key, rel)
                            if ep.is_file():
                                try:
                                    ep.unlink()
                                except Exception:
                                    pass
                            removed = True
                            continue
                    except (TypeError, ValueError):
                        pass
                kept.append(e)
            if removed or len(kept) != len(entries):
                data["entries"] = kept
                self._save_manifest(scope_key, data)

    def add_memory(
        self,
        *,
        title: str,
        content: str,
        tier: str = "episodic",
        memory_type: str = "lesson",
        scope_key: str = "",
        source: str = "auto",
        user_request: Optional[str] = None,
        system_note: Optional[str] = None,
        strength: float = 0.55,
        extra: Optional[Dict[str, Any]] = None,
        memory_id: Optional[str] = None,
        created_at: Optional[float] = None,
        last_access: Optional[float] = None,
        expires_at_override: Optional[float] = None,
    ) -> str:
        with self._lock:
            mid = (memory_id or str(uuid.uuid4())).strip()
            now = time.time()
            cr = float(created_at) if created_at is not None else now
            la = float(last_access) if last_access is not None else now
            sk = (scope_key or "global").strip() or "global"
            title = (title or "untitled").strip()[:500]
            content = (content or "").strip()
            if not content:
                raise ValueError("content must not be empty")
            exp = expires_at_override
            if exp is None:
                exp = _tier_expires_at(tier, cr)
            summary = content.replace("\n", " ").strip()[:240]
            rel_path = f"entries/{mid}.md"
            ep = self._entry_path(sk, rel_path)
            meta_fm: Dict[str, Any] = {
                "id": mid,
                "tier": tier,
                "memory_type": memory_type,
                "title": title,
                "scope_key": sk,
                "strength": float(strength),
                "created_at": cr,
                "last_access": la,
                "expires_at": exp,
                "source": source,
            }
            if user_request:
                meta_fm["user_request"] = user_request
            if system_note:
                meta_fm["system_note"] = system_note
            if extra:
                meta_fm["extra"] = extra
            _write_md_document(ep, meta_fm, content)

            data = self._load_manifest(sk)
            entries = [e for e in (data.get("entries") or []) if isinstance(e, dict) and str(e.get("id")) != mid]
            entries.append(
                {
                    "id": mid,
                    "rel_path": rel_path,
                    "title": title,
                    "summary": summary,
                    "created_at": cr,
                    "last_access": la,
                    "strength": float(strength),
                    "tier": tier,
                    "memory_type": memory_type,
                    "expires_at": exp,
                    "source": source,
                }
            )
            entries.sort(key=lambda x: float(x.get("last_access") or 0), reverse=True)
            data["entries"] = entries
            self._save_manifest(sk, data)
            return mid

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            self._purge_expired_unlocked()
            mid = (memory_id or "").strip()
            if not mid:
                return False
            for manifest_path in self._scopes_base().glob("*/manifest.json"):
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sk = str(raw.get("scope_key") or "global")
                entries = raw.get("entries")
                if not isinstance(entries, list):
                    continue
                new_entries: List[Dict[str, Any]] = []
                hit = False
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    if str(e.get("id")) == mid:
                        hit = True
                        rel = str(e.get("rel_path", ""))
                        ep = self._entry_path(sk, rel)
                        if ep.is_file():
                            try:
                                ep.unlink()
                            except Exception:
                                pass
                        continue
                    new_entries.append(e)
                if hit:
                    raw["entries"] = new_entries
                    self._save_manifest(sk, raw)
                    return True
            return False

    def _score_entry(
        self, tokens: List[str], title: str, summary: str, body: str
    ) -> float:
        blob = f"{title}\n{summary}\n{body}"
        if not tokens:
            return 0.0
        score = 0.0
        blob_lower = blob.lower()
        for t in tokens:
            tl = t.lower()
            c = blob_lower.count(tl)
            score += float(c) * 2.0
            if tl in title.lower():
                score += 3.0
            if tl in summary.lower():
                score += 1.5
        return score

    def _collect_entries_for_scope(self, scope_key: str) -> List[Dict[str, Any]]:
        data = self._load_manifest(scope_key)
        out: List[Dict[str, Any]] = []
        sk = str(data.get("scope_key") or scope_key or "global")
        now = time.time()
        for e in data.get("entries") or []:
            if not isinstance(e, dict):
                continue
            exp = e.get("expires_at")
            if exp is not None:
                try:
                    if float(exp) < now:
                        continue
                except (TypeError, ValueError):
                    pass
            e = dict(e)
            e["_scope_key"] = sk
            out.append(e)
        return out

    def _collect_all_entries(self) -> List[Dict[str, Any]]:
        all_e: List[Dict[str, Any]] = []
        for manifest_path in sorted(self._scopes_base().glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sk = str(data.get("scope_key") or "global")
            for e in data.get("entries") or []:
                if not isinstance(e, dict):
                    continue
                e = dict(e)
                e["_scope_key"] = sk
                all_e.append(e)
        return all_e

    def search_memories(
        self,
        query: str,
        top_k: int = 6,
        scope_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._purge_expired_unlocked()
            q = (query or "").strip()
            if not q:
                return []
            tokens = _query_tokens(q)
            top_k = max(1, min(top_k, 20))

            def run_pool(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                scored: List[Tuple[float, Dict[str, Any], str, str]] = []
                for e in candidates:
                    sk = str(e.get("_scope_key", "global"))
                    rel = str(e.get("rel_path", ""))
                    title = str(e.get("title", ""))
                    summary = str(e.get("summary", ""))
                    body = ""
                    ep = self._entry_path(sk, rel)
                    if ep.is_file():
                        meta, body = _read_md_document(ep)
                        title = str(meta.get("title") or title)
                        if meta.get("system_note"):
                            summary = f"{summary} {meta.get('system_note')}"
                    sc = self._score_entry(tokens, title, summary, body)
                    if sc > 0:
                        mid = str(e.get("id", ""))
                        scored.append((sc, e, title, body))
                if not scored:
                    return []
                max_s = max(s[0] for s in scored)
                if max_s <= 0:
                    max_s = 1.0
                out: List[Dict[str, Any]] = []
                for sc, e, title, body in sorted(
                    scored, key=lambda x: x[0], reverse=True
                )[:top_k]:
                    sim = min(1.0, sc / max_s)
                    mid = str(e.get("id", ""))
                    sys_note = None
                    ep = self._entry_path(str(e.get("_scope_key", "global")), str(e.get("rel_path", "")))
                    created_ts: Optional[float] = None
                    if ep.is_file():
                        meta, body = _read_md_document(ep)
                        sys_note = meta.get("system_note")
                        body = body.strip()
                        try:
                            if meta.get("created_at") is not None:
                                created_ts = float(meta.get("created_at"))
                        except (TypeError, ValueError):
                            created_ts = None
                    if created_ts is None:
                        try:
                            if e.get("created_at") is not None:
                                created_ts = float(e.get("created_at"))
                        except (TypeError, ValueError):
                            created_ts = None
                    if created_ts is None:
                        created_ts = 0.0
                    out.append(
                        {
                            "id": mid,
                            "title": title,
                            "content": body[:8000],
                            "tier": str(e.get("tier", "")),
                            "memory_type": str(e.get("memory_type", "")),
                            "source": str(e.get("source", "")),
                            "similarity": round(float(sim), 4),
                            "raw_score": round(float(sc), 4),
                            "created_at": created_ts,
                            "system_note": sys_note,
                        }
                    )
                return out

            sk_filter = (scope_key or "").strip() or None
            if sk_filter:
                candidates = self._collect_entries_for_scope(sk_filter)
                res = run_pool(candidates)
                if res:
                    return res
            # Global fallback (matches the old Chroma behavior of relaxing scope when nothing matches).
            return run_pool(self._collect_all_entries())

    def touch_memory(self, memory_id: str, delta_strength: float = 0.05) -> None:
        with self._lock:
            self._purge_expired_unlocked()
            mid = (memory_id or "").strip()
            if not mid:
                return
            for manifest_path in self._scopes_base().glob("*/manifest.json"):
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sk = str(data.get("scope_key") or "global")
                entries = data.get("entries")
                if not isinstance(entries, list):
                    continue
                for i, e in enumerate(entries):
                    if not isinstance(e, dict):
                        continue
                    if str(e.get("id")) != mid:
                        continue
                    now = time.time()
                    e["last_access"] = now
                    try:
                        st = float(e.get("strength") or 0.5) + delta_strength
                    except (TypeError, ValueError):
                        st = 0.5 + delta_strength
                    e["strength"] = min(1.0, st)
                    entries[i] = e
                    rel = str(e.get("rel_path", ""))
                    ep = self._entry_path(sk, rel)
                    if ep.is_file():
                        meta, body = _read_md_document(ep)
                        meta["last_access"] = now
                        meta["strength"] = e["strength"]
                        _write_md_document(ep, meta, body)
                    data["entries"] = entries
                    self._save_manifest(sk, data)
                    return

    def list_recent(self, limit: int = 20, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._purge_expired_unlocked()
            limit = max(1, min(limit, 100))
            sk = (scope_key or "").strip() or None
            if sk:
                data = self._load_manifest(sk)
                entries = [
                    e
                    for e in (data.get("entries") or [])
                    if isinstance(e, dict)
                ]
            else:
                entries = self._collect_all_entries()
            entries.sort(
                key=lambda x: float(x.get("last_access") or 0), reverse=True
            )
            out: List[Dict[str, Any]] = []
            for e in entries[:limit]:
                if not isinstance(e, dict):
                    continue
                preview = str(e.get("summary", ""))[:200]
                out.append(
                    {
                        "id": e.get("id"),
                        "title": e.get("title"),
                        "tier": e.get("tier"),
                        "memory_type": e.get("memory_type"),
                        "source": e.get("source"),
                        "strength": e.get("strength"),
                        "created_at": e.get("created_at"),
                        "preview": preview,
                    }
                )
            return out

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            n = 0
            for manifest_path in self._scopes_base().glob("*/manifest.json"):
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    entries = data.get("entries")
                    if isinstance(entries, list):
                        n += len(entries)
                except Exception:
                    pass
            return {
                "total_memories": n,
                "storage_backend": "markdown",
                "embedding_model": None,
                "storage_dir": str(self.memory_root),
            }


class MemoryService:
    """Run MemoryManager on a single worker thread."""

    def __init__(self, config_dir: str, embedding_model: str = ""):
        self._config_dir = str(Path(config_dir))
        self._embedding_model = embedding_model
        self._task_queue: "queue.Queue[Optional[Tuple[Callable[[], Any], concurrent.futures.Future]]]" = queue.Queue()
        self._closed = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"{get_app_logger_root()}-memory",
            daemon=True,
        )
        self._worker.start()
        self._mm: Optional[MemoryManager] = None
        self._ready = threading.Event()
        self._enqueue_background(self._bootstrap)

    def _worker_loop(self) -> None:
        while True:
            task = self._task_queue.get()
            if task is None:
                return
            fn, future = task
            if future.cancelled():
                continue
            try:
                result = fn()
            except Exception as e:
                try:
                    future.set_exception(e)
                except Exception:
                    pass
            else:
                try:
                    future.set_result(result)
                except Exception:
                    pass

    def _enqueue_background(self, fn: Callable[[], Any]) -> None:
        if self._closed.is_set():
            return
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._task_queue.put((fn, future))

    def _submit(self, fn: Callable[[], Any], timeout: float) -> Any:
        if self._closed.is_set():
            raise RuntimeError("MemoryService is closed")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._task_queue.put((fn, future))
        return future.result(timeout=timeout)

    def _bootstrap(self) -> None:
        try:
            _mem_log.info("Experiential memory thread initialization started, config_dir=%s", self._config_dir)
            self._mm = MemoryManager(self._config_dir, self._embedding_model)
            _mem_log.info("Experiential memory thread initialization completed (Markdown backend)")
        except Exception:
            _mem_log.exception("Experiential memory thread initialization failed")
            self._mm = None
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def is_available(self) -> bool:
        if not self._ready.wait(timeout=0.01):
            return False
        return self._mm is not None

    def add_memory(self, **kwargs: Any) -> str:
        def _do() -> str:
            if self._mm is None:
                raise RuntimeError("MemoryManager is unavailable")
            return self._mm.add_memory(**kwargs)

        if not self.wait_ready(120.0):
            raise RuntimeError("Memory service is not ready")
        if self._mm is None:
            raise RuntimeError("Memory service is unavailable")
        return self._submit(_do, timeout=60.0)

    def search_memories(self, query: str, top_k: int = 6, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        def _do() -> List[Dict[str, Any]]:
            if self._mm is None:
                return []
            return self._mm.search_memories(query, top_k=top_k, scope_key=scope_key)

        if not self.wait_ready(120.0) or self._mm is None:
            return []
        return self._submit(_do, timeout=60.0)

    def list_recent(self, limit: int = 20, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        def _do() -> List[Dict[str, Any]]:
            if self._mm is None:
                return []
            return self._mm.list_recent(limit=limit, scope_key=scope_key)

        if not self.wait_ready(120.0) or self._mm is None:
            return []
        return self._submit(_do, timeout=30.0)

    def delete_memory(self, memory_id: str) -> bool:
        def _do() -> bool:
            if self._mm is None:
                return False
            return self._mm.delete_memory(memory_id)

        if not self.wait_ready(120.0) or self._mm is None:
            return False
        return self._submit(_do, timeout=30.0)

    def touch_memory(self, memory_id: str) -> None:
        def _do() -> None:
            if self._mm is None:
                return
            self._mm.touch_memory(memory_id)

        if not self.wait_ready(120.0) or self._mm is None:
            return
        self._submit(_do, timeout=10.0)

    def stats(self) -> Dict[str, Any]:
        def _do() -> Dict[str, Any]:
            if self._mm is None:
                return {}
            return self._mm.stats()

        if not self.wait_ready(120.0) or self._mm is None:
            return {}
        return self._submit(_do, timeout=10.0)

    def shutdown(self, wait: bool = False) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        def _do() -> None:
            self._mm = None

        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._task_queue.put((_do, future))
            if wait:
                future.result(timeout=30.0)
        except Exception:
            _mem_log.debug("MemoryService shutdown cleanup failed", exc_info=True)
        finally:
            self._task_queue.put(None)
            if wait:
                try:
                    self._worker.join(timeout=2.0)
                except Exception:
                    pass
