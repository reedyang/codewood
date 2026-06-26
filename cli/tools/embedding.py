from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

_EMBEDDING_DIM = 384
_SCHEMA_VERSION = 1
_INDEX_CREATED: Set[str] = set()
_INDEX_LOCK = threading.Lock()


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    dot = float(np.dot(a, b))
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingProvider:
    def __init__(self) -> None:
        self._provider: Optional[Callable[[List[str]], List[np.ndarray]]] = None
        self._provider_name: str = "none"
        self._local_model: Any = None
        self._api_base_url: str = ""
        self._api_key: str = ""
        self._api_model: str = ""
        self._initialized: bool = False

    def initialize(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        force_local: bool = False,
    ) -> None:
        if self._initialized:
            return
        self._initialized = True

        self._api_base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._api_model = str(model or "").strip()

        if not force_local:
            provider = self._try_init_api_provider()
            if provider is not None:
                self._provider = provider
                self._provider_name = f"api:{self._api_model}"
                return

        provider = self._try_init_local_provider()
        if provider is not None:
            self._provider = provider
            self._provider_name = "local:all-MiniLM-L6-v2"
            return

        self._provider_name = "none"

    @property
    def available(self) -> bool:
        return self._provider is not None

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def dim(self) -> int:
        return _EMBEDDING_DIM

    def embed(self, texts: List[str]) -> List[np.ndarray]:
        if not self._provider or not texts:
            return []
        try:
            return list(self._provider(texts))
        except Exception:
            return []

    def _try_init_api_provider(self) -> Optional[Callable[[List[str]], List[np.ndarray]]]:
        if not self._api_base_url or not self._api_key:
            return None

        import urllib.request
        import json

        base = self._api_base_url
        key = self._api_key
        model = self._api_model or "text-embedding-3-small"

        # Probe the embeddings endpoint
        probe_url = f"{base}/embeddings"
        probe_data = json.dumps({
            "model": model,
            "input": "probe",
        }).encode("utf-8")
        probe_req = urllib.request.Request(
            probe_url,
            data=probe_data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(probe_req, timeout=10)
            resp.read()
        except Exception:
            return None

        def _api_embed(texts: List[str]) -> List[np.ndarray]:
            results: List[np.ndarray] = []
            batch_size = 20
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i + batch_size]
                req_data = json.dumps({
                    "model": model,
                    "input": batch,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{base}/embeddings",
                    data=req_data,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    resp = urllib.request.urlopen(req, timeout=60)
                    body = json.loads(resp.read().decode("utf-8"))
                    for item in body.get("data", []):
                        emb = item.get("embedding", [])
                        if emb:
                            results.append(np.array(emb, dtype=np.float32))
                except Exception:
                    for _ in batch:
                        results.append(np.zeros(_EMBEDDING_DIM, dtype=np.float32))
            return results

        return _api_embed

    def _try_init_local_provider(self) -> Optional[Callable[[List[str]], List[np.ndarray]]]:
        try:
            import sentence_transformers  # type: ignore[import-untyped]
        except ImportError:
            return None

        model = sentence_transformers.SentenceTransformer(
            "all-MiniLM-L6-v2",
            device="cpu",
        )
        self._local_model = model

        def _local_embed(texts: List[str]) -> List[np.ndarray]:
            if not texts:
                return []
            try:
                result = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
                if isinstance(result, np.ndarray):
                    return [np.array(r, dtype=np.float32) for r in result]
                return [np.array(r, dtype=np.float32) for r in result]
            except Exception:
                return [np.zeros(_EMBEDDING_DIM, dtype=np.float32) for _ in texts]

        return _local_embed


class FileEmbeddingIndex:
    def __init__(self, index_dir: Path) -> None:
        self.index_dir = Path(index_dir).resolve()
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.index_dir / "file_embeddings.db"
        self._lock = threading.Lock()
        self._provider: Optional[EmbeddingProvider] = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS embeddings (
                    rel TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    indexed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_embeddings_rel ON embeddings(rel);
            """)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_provider(self) -> Optional[EmbeddingProvider]:
        return self._provider

    def initialize_provider(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
    ) -> EmbeddingProvider:
        with self._lock:
            if self._provider is not None:
                return self._provider
            provider = EmbeddingProvider()
            provider.initialize(
                base_url=base_url,
                api_key=api_key,
                model=model,
                force_local=False,
            )
            self._provider = provider
            return provider

    def has_embeddings(self) -> bool:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
            return int(row[0]) > 0 if row else False
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_embedding(self, rel: str) -> Optional[np.ndarray]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE rel = ?", (rel,)
            ).fetchone()
            if row:
                return np.frombuffer(row[0], dtype=np.float32)
            return None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_embeddings_batch(self, rels: List[str]) -> Dict[str, np.ndarray]:
        if not rels:
            return {}
        conn = self._connect()
        try:
            placeholders = ",".join("?" * len(rels))
            rows = conn.execute(
                f"SELECT rel, embedding FROM embeddings WHERE rel IN ({placeholders})",
                rels,
            ).fetchall()
            return {
                str(r[0]): np.frombuffer(r[1], dtype=np.float32)
                for r in rows
            }
        except Exception:
            return {}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def index_files(
        self,
        files: Dict[str, str],
        provider: Optional[EmbeddingProvider] = None,
    ) -> Dict[str, Any]:
        ep = provider or self._provider
        if ep is None or not ep.available:
            return {"success": False, "error": "No embedding provider available", "indexed": 0}

        t0 = time.time()
        rels = list(files.keys())
        contents = [files[rel][:4096] for rel in rels]
        embeddings = ep.embed(contents)

        if len(embeddings) != len(rels):
            return {"success": False, "error": "Embedding count mismatch", "indexed": 0}

        conn = self._connect()
        now = time.time()
        try:
            conn.execute("DELETE FROM embeddings")
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (rel, embedding, indexed_at) VALUES (?, ?, ?)",
                [
                    (rel, emb.tobytes(), now)
                    for rel, emb in zip(rels, embeddings)
                ],
            )
            conn.commit()
        except Exception as e:
            return {"success": False, "error": str(e), "indexed": 0}
        finally:
            try:
                conn.close()
            except Exception:
                pass

        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "success": True,
            "indexed": len(embeddings),
            "elapsed_ms": elapsed_ms,
            "provider": ep.provider_name,
        }

    def hybrid_search(
        self,
        query: str,
        candidate_files: Dict[str, Any],
        provider: Optional[EmbeddingProvider] = None,
        embedding_weight: float = 0.3,
        top_k: int = 12,
    ) -> List[str]:
        ep = provider or self._provider
        if ep is None or not ep.available:
            return list(candidate_files.keys())[:top_k]

        query_vecs = ep.embed([query[:4096]])
        if not query_vecs or len(query_vecs) == 0:
            return list(candidate_files.keys())[:top_k]
        query_vec = query_vecs[0]

        rels = list(candidate_files.keys())
        stored = self.get_embeddings_batch(rels)

        scored: List[Tuple[float, str]] = []
        for rel in rels:
            bm25 = float(candidate_files.get(rel, 0.0))
            emb = stored.get(rel)
            if emb is not None:
                cos_sim = _cosine_similarity(query_vec, emb)
            else:
                cos_sim = 0.0
            hybrid = (1.0 - embedding_weight) * (bm25 / max(1.0, bm25 + 20.0)) + embedding_weight * cos_sim
            scored.append((hybrid, rel))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [rel for _, rel in scored[:top_k]]
