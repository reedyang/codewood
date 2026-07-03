"""
Experiential memory (user-requested facts and conventions).

Storage: SQLite + sentence-transformer embeddings (all-MiniLM-L6-v2).
Search: cosine similarity on 384-dim embedding vectors.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ...config.app_info import get_app_logger_root

_mem_log = logging.getLogger(f"{get_app_logger_root()}.memory")

MEMORY_AVAILABLE = True

_EMBEDDING_DIM = 384


def _tier_expires_at(tier: str, now_ts: float) -> Optional[float]:
    if tier == "working":
        return now_ts + 72 * 3600
    if tier == "episodic":
        return now_ts + 30 * 24 * 3600
    if tier == "durable":
        return None
    return now_ts + 7 * 24 * 3600


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    dot = float(np.dot(a, b))
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _init_embedding_provider() -> Any:
    """Lazy-init and return an EmbeddingProvider instance."""
    try:
        from ...tools.embedding import EmbeddingProvider
        p = EmbeddingProvider()
        p.initialize()
        return p
    except Exception:
        _mem_log.warning("Embedding provider initialization failed", exc_info=True)
        return None


class MemoryManager:
    """SQLite + embedding-based experiential memory store."""

    def __init__(self, config_dir: str, embedding_model: str = ""):
        self.config_dir = Path(config_dir)
        self.memory_root = self.config_dir / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self._db_path = self.memory_root / "memory_embeddings.db"
        self._lock = threading.Lock()
        self._provider: Any = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL DEFAULT 'episodic',
                    memory_type TEXT NOT NULL DEFAULT 'lesson',
                    scope_key TEXT NOT NULL DEFAULT 'global',
                    source TEXT NOT NULL DEFAULT 'user_request',
                    strength REAL NOT NULL DEFAULT 0.55,
                    created_at REAL NOT NULL,
                    last_access REAL NOT NULL,
                    expires_at REAL,
                    system_note TEXT,
                    user_request TEXT,
                    embedding BLOB,
                    indexed_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_last_access ON memories(last_access)")
            conn.execute("COMMIT")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _lazy_provider(self) -> Any:
        if self._provider is None:
            self._provider = _init_embedding_provider()
        return self._provider

    def _embed(self, text: str) -> Optional[np.ndarray]:
        provider = self._lazy_provider()
        if provider is None or not provider.available:
            return None
        vecs = provider.embed([text[:4096]])
        if vecs:
            return vecs[0]
        return None

    def _purge_expired(self) -> None:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def add_memory(
        self,
        *,
        title: str,
        content: str,
        tier: str = "episodic",
        memory_type: str = "lesson",
        scope_key: str = "",
        source: str = "user_request",
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
            self._purge_expired()
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

            # Generate embedding
            embed_text = f"{title}\n{summary}\n{content}"[:4096]
            vec = self._embed(embed_text)
            embedding_blob = vec.tobytes() if vec is not None else None
            indexed_at = time.time() if vec is not None else None

            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, title, content, summary, tier, memory_type, scope_key,
                        source, strength, created_at, last_access, expires_at,
                        system_note, user_request, embedding, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mid, title, content, summary, tier, memory_type, sk,
                        source, float(strength), cr, la, exp,
                        system_note, user_request, embedding_blob, indexed_at,
                    ),
                )
                conn.commit()
            except Exception:
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            return mid

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            self._purge_expired()
            mid = (memory_id or "").strip()
            if not mid:
                return False
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def search_memories(
        self,
        query: str,
        top_k: int = 6,
        scope_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._purge_expired()
            q = (query or "").strip()
            if not q:
                return []
            top_k = max(1, min(top_k, 20))

            # Embed query
            query_vec = self._embed(q)
            if query_vec is None:
                return []

            conn = self._connect()
            try:
                scope_filter = (scope_key or "").strip() or None
                if scope_filter:
                    rows = conn.execute(
                        "SELECT id, title, content, tier, memory_type, source, "
                        "strength, created_at, last_access, expires_at, "
                        "system_note, embedding "
                        "FROM memories WHERE scope_key = ? AND embedding IS NOT NULL",
                        (scope_filter,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, title, content, tier, memory_type, source, "
                        "strength, created_at, last_access, expires_at, "
                        "system_note, embedding "
                        "FROM memories WHERE embedding IS NOT NULL"
                    ).fetchall()
            except Exception:
                return []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            scored: List[Tuple[float, Dict[str, Any]]] = []
            for row in rows:
                mem_id = str(row[0])
                title = str(row[1] or "")
                content = str(row[2] or "")
                tier = str(row[3] or "")
                mem_type = str(row[4] or "")
                source = str(row[5] or "")
                strength = float(row[6] or 0.0)
                created_at = float(row[7] or 0.0)
                system_note = str(row[10]) if row[10] else None
                emb_blob = row[11]

                if emb_blob is None:
                    continue

                try:
                    stored_vec = np.frombuffer(emb_blob, dtype=np.float32)
                except Exception:
                    continue

                if stored_vec.shape[0] != _EMBEDDING_DIM:
                    continue

                sim = _cosine_similarity(query_vec, stored_vec)
                scored.append((
                    sim,
                    {
                        "id": mem_id,
                        "title": title,
                        "content": content[:8000],
                        "tier": tier,
                        "memory_type": mem_type,
                        "source": source,
                        "similarity": round(float(sim), 4),
                        "raw_score": round(float(sim), 4),
                        "created_at": created_at,
                        "system_note": system_note,
                    },
                ))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [item for _, item in scored[:top_k]]

    def touch_memory(self, memory_id: str, delta_strength: float = 0.05) -> None:
        with self._lock:
            self._purge_expired()
            mid = (memory_id or "").strip()
            if not mid:
                return
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT strength FROM memories WHERE id = ?", (mid,)
                ).fetchone()
                if row is None:
                    return
                now = time.time()
                st = min(1.0, float(row[0] or 0.5) + delta_strength)
                conn.execute(
                    "UPDATE memories SET last_access = ?, strength = ? WHERE id = ?",
                    (now, st, mid),
                )
                conn.commit()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def list_recent(self, limit: int = 20, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._purge_expired()
            limit = max(1, min(limit, 100))
            conn = self._connect()
            try:
                scope_filter = (scope_key or "").strip() or None
                if scope_filter:
                    rows = conn.execute(
                        "SELECT id, title, tier, memory_type, source, strength, created_at, content "
                        "FROM memories WHERE scope_key = ? "
                        "ORDER BY last_access DESC LIMIT ?",
                        (scope_filter, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, title, tier, memory_type, source, strength, created_at, content "
                        "FROM memories "
                        "ORDER BY last_access DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            except Exception:
                return []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            out: List[Dict[str, Any]] = []
            for row in rows:
                preview = str(row[7] or "").replace("\n", " ").strip()[:200]
                out.append({
                    "id": row[0],
                    "title": row[1],
                    "tier": row[2],
                    "memory_type": row[3],
                    "source": row[4],
                    "strength": row[5],
                    "created_at": row[6],
                    "preview": preview,
                })
            return out

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
                n = int(row[0]) if row else 0
            except Exception:
                n = 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return {
                "total_memories": n,
                "storage_backend": "sqlite+embedding",
                "embedding_model": "all-MiniLM-L6-v2",
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
            _mem_log.info("Experiential memory thread initialization completed (SQLite+embedding backend)")
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
