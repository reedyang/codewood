"""
经验记忆（与知识库完全分离：知识库 = 图书馆文档；记忆 = 内化经验）。

存储：SQLite 元数据 + Chroma 独立持久化目录（与 knowledge_db 不同路径）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import concurrent.futures
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# 与知识库默认嵌入一致，便于本机缓存复用；集合名与路径均不同，数据不混用。
DEFAULT_MEMORY_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_mem_log = logging.getLogger("smartshell.memory")


@contextlib.contextmanager
def _quiet_embedding_init() -> Iterator[None]:
    """加载 SentenceTransformer 时压低日志（不依赖 knowledge_manager）。"""
    import warnings

    tr_logging = None
    prev_verbosity: Optional[int] = None
    try:
        from transformers.utils import logging as tr_logging

        prev_verbosity = tr_logging.get_verbosity()
        tr_logging.set_verbosity_error()
        tr_logging.disable_progress_bar()
    except Exception:
        tr_logging = None
    saved: List[Tuple[logging.Logger, int]] = []
    for name in ("huggingface_hub", "transformers", "sentence_transformers"):
        lg = logging.getLogger(name)
        saved.append((lg, lg.level))
        lg.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            yield
    finally:
        for lg, lvl in saved:
            lg.setLevel(lvl)
        if tr_logging is not None and prev_verbosity is not None:
            tr_logging.set_verbosity(prev_verbosity)
            try:
                tr_logging.enable_progress_bar()
            except Exception:
                pass


try:
    # 不在此处 redirect stdout：本模块会在后台线程加载，与主线程 print 竞态会吞掉启动横幅。
    import chromadb
    from chromadb.config import Settings
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
except Exception:
    chromadb = None  # type: ignore
    Settings = None  # type: ignore
    SentenceTransformerEmbeddingFunction = None  # type: ignore

MEMORY_AVAILABLE = bool(chromadb is not None)


def _scope_key_from_workdir(work_directory: Path) -> str:
    try:
        return str(work_directory.resolve())
    except Exception:
        return str(work_directory)


def _tier_expires_at(tier: str, now_ts: float) -> Optional[float]:
    """返回绝对时间戳；None 表示由 decay 规则处理而非固定过期。"""
    if tier == "working":
        return now_ts + 72 * 3600  # 72h
    if tier == "episodic":
        return now_ts + 30 * 24 * 3600  # 30d 默认，可被强化延长
    if tier == "durable":
        return None
    return now_ts + 7 * 24 * 3600


class MemoryManager:
    """
    经验记忆：与 KnowledgeManager 独立；使用独立 Chroma 目录与集合名。
    """

    def __init__(self, config_dir: str, embedding_model: str = DEFAULT_MEMORY_EMBEDDING_MODEL):
        if not MEMORY_AVAILABLE:
            raise ImportError("记忆模块依赖 chromadb / sentence-transformers，请安装 requirements")
        self.config_dir = Path(config_dir)
        self.memory_root = self.config_dir / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.memory_root / "memories.sqlite"
        self.chroma_path = self.memory_root / "memory_chroma"
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.embedding_model = embedding_model
        self._embedding_fn = None
        self.client = None
        self.collection = None
        self._init_sqlite()
        self._init_chroma()

    def _init_sqlite(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    strength REAL NOT NULL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    last_access REAL NOT NULL,
                    expires_at REAL,
                    source TEXT NOT NULL,
                    user_request TEXT,
                    system_note TEXT,
                    extra_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_exp ON memories(expires_at)"
            )
            conn.commit()

    def _init_chroma(self) -> None:
        try:
            with _quiet_embedding_init():
                self._embedding_fn = SentenceTransformerEmbeddingFunction(
                    model_name=self.embedding_model
                )
            self.client = chromadb.PersistentClient(
                path=str(self.chroma_path),
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )
            self.collection = self.client.get_or_create_collection(
                name="smart_shell_experience_memory",
                embedding_function=self._embedding_fn,
                metadata={"hnsw:space": "cosine"},
            )
            _mem_log.info("经验记忆 Chroma 就绪，path=%s", self.chroma_path)
        except Exception as e:
            _mem_log.exception("经验记忆 Chroma 初始化失败: %s", e)
            raise

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
    ) -> str:
        mid = str(uuid.uuid4())
        now = time.time()
        exp = _tier_expires_at(tier, now)
        title = (title or "untitled").strip()[:500]
        content = (content or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        scope_key = scope_key or "global"
        doc_text = f"{title}\n{content}"
        meta = {
            "tier": tier,
            "memory_type": memory_type,
            "source": source,
            "scope_key": scope_key,
            "title": title[:200],
        }
        extra_json = json.dumps(extra or {}, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, tier, memory_type, title, content, scope_key, strength,
                    created_at, last_access, expires_at, source, user_request, system_note, extra_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mid,
                    tier,
                    memory_type,
                    title,
                    content,
                    scope_key,
                    float(strength),
                    now,
                    now,
                    exp,
                    source,
                    user_request,
                    system_note,
                    extra_json,
                ),
            )
            conn.commit()
        self.collection.add(
            ids=[mid],
            documents=[doc_text[:8000]],
            metadatas=[meta],
        )
        return mid

    def delete_memory(self, memory_id: str) -> bool:
        try:
            self.collection.delete(ids=[memory_id])
        except Exception:
            pass
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    def _purge_expired(self) -> None:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).fetchall()
            for (mid,) in rows:
                try:
                    self.collection.delete(ids=[mid])
                except Exception:
                    pass
                conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
            conn.commit()

    def search_memories(
        self,
        query: str,
        top_k: int = 6,
        scope_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._purge_expired()
        q = (query or "").strip()
        if not q:
            return []
        where = None
        if scope_key:
            where = {"scope_key": scope_key}
        try:
            res = self.collection.query(
                query_texts=[q],
                n_results=max(1, min(top_k, 20)),
                where=where,
                include=["documents", "distances", "metadatas"],
            )
            ids0 = (res.get("ids") or [[]])[0]
            if scope_key and not ids0:
                res = self.collection.query(
                    query_texts=[q],
                    n_results=max(1, min(top_k, 20)),
                    include=["documents", "distances", "metadatas"],
                )
        except Exception as e:
            _mem_log.warning("记忆检索失败: %s", e)
            return []
        out: List[Dict[str, Any]] = []
        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, mid in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            dist = dists[i] if i < len(dists) else 1.0
            sim = max(0.0, 1.0 - float(dist))
            row = self._get_row(mid)
            if not row:
                continue
            out.append(
                {
                    "id": mid,
                    "title": row.get("title", ""),
                    "content": row.get("content", ""),
                    "tier": row.get("tier", ""),
                    "memory_type": row.get("memory_type", ""),
                    "source": row.get("source", ""),
                    "similarity": round(sim, 4),
                    "system_note": row.get("system_note"),
                }
            )
        return out

    def _get_row(self, mid: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,))
            r = cur.fetchone()
            if not r:
                return None
            return dict(r)

    def touch_memory(self, memory_id: str, delta_strength: float = 0.05) -> None:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE memories SET last_access = ?, strength = min(1.0, strength + ?)
                WHERE id = ?
                """,
                (now, delta_strength, memory_id),
            )
            conn.commit()

    def list_recent(self, limit: int = 20, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        self._purge_expired()
        limit = max(1, min(limit, 100))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if scope_key:
                cur = conn.execute(
                    """
                    SELECT id, title, tier, memory_type, source, strength, created_at, substr(content,1,200) as preview
                    FROM memories WHERE scope_key = ? ORDER BY last_access DESC LIMIT ?
                    """,
                    (scope_key, limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, title, tier, memory_type, source, strength, created_at, substr(content,1,200) as preview
                    FROM memories ORDER BY last_access DESC LIMIT ?
                    """,
                    (limit,),
                )
            return [dict(x) for x in cur.fetchall()]

    def stats(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {
            "total_memories": n,
            "embedding_model": self.embedding_model,
            "storage_dir": str(self.memory_root),
        }


class MemoryService:
    """单线程执行 MemoryManager（与 KnowledgeService 相同线程亲和策略）。"""

    def __init__(self, config_dir: str, embedding_model: str = DEFAULT_MEMORY_EMBEDDING_MODEL):
        self._config_dir = str(Path(config_dir))
        self._embedding_model = embedding_model
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smartshell-memory"
        )
        self._mm: Optional[MemoryManager] = None
        self._ready = threading.Event()
        self._executor.submit(self._bootstrap)

    def _bootstrap(self) -> None:
        try:
            if not MEMORY_AVAILABLE:
                _mem_log.info("经验记忆跳过：chromadb 等依赖不可用")
                self._mm = None
                return
            _mem_log.info("经验记忆线程开始初始化, config_dir=%s", self._config_dir)
            self._mm = MemoryManager(self._config_dir, self._embedding_model)
            _mem_log.info("经验记忆线程初始化完成")
        except Exception:
            _mem_log.exception("经验记忆线程初始化失败")
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
                raise RuntimeError("MemoryManager 不可用")
            return self._mm.add_memory(**kwargs)

        if not self.wait_ready(120.0):
            raise RuntimeError("记忆服务未就绪")
        if self._mm is None:
            raise RuntimeError("记忆服务不可用")
        return self._executor.submit(_do).result(timeout=60.0)

    def search_memories(self, query: str, top_k: int = 6, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        def _do() -> List[Dict[str, Any]]:
            if self._mm is None:
                return []
            return self._mm.search_memories(query, top_k=top_k, scope_key=scope_key)

        if not self.wait_ready(120.0) or self._mm is None:
            return []
        return self._executor.submit(_do).result(timeout=60.0)

    def list_recent(self, limit: int = 20, scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        def _do() -> List[Dict[str, Any]]:
            if self._mm is None:
                return []
            return self._mm.list_recent(limit=limit, scope_key=scope_key)

        if not self.wait_ready(120.0) or self._mm is None:
            return []
        return self._executor.submit(_do).result(timeout=30.0)

    def delete_memory(self, memory_id: str) -> bool:
        def _do() -> bool:
            if self._mm is None:
                return False
            return self._mm.delete_memory(memory_id)

        if not self.wait_ready(120.0) or self._mm is None:
            return False
        return self._executor.submit(_do).result(timeout=30.0)

    def touch_memory(self, memory_id: str) -> None:
        def _do() -> None:
            if self._mm is None:
                return
            self._mm.touch_memory(memory_id)

        if not self.wait_ready(120.0) or self._mm is None:
            return
        self._executor.submit(_do).result(timeout=10.0)

    def stats(self) -> Dict[str, Any]:
        def _do() -> Dict[str, Any]:
            if self._mm is None:
                return {}
            return self._mm.stats()

        if not self.wait_ready(120.0) or self._mm is None:
            return {}
        return self._executor.submit(_do).result(timeout=10.0)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)
