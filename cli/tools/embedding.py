from __future__ import annotations

import logging
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from ..config.app_info import get_app_logger_root

logger = logging.getLogger(f"{get_app_logger_root()}.embedding")

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

_EMBEDDING_DIM = 384
_SCHEMA_VERSION = 1
_INDEX_CREATED: Set[str] = set()
_INDEX_LOCK = threading.Lock()
_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# Hugging Face repo hosting the model. The ONNX backend downloads raw files
# from here (honoring the HF_ENDPOINT mirror env var) because
# sentence-transformers may be unavailable (e.g. ARM64-native Windows has no
# torch wheels at all).
_HF_REPO = f"sentence-transformers/{_EMBEDDING_MODEL_NAME}"
_MODEL_TOKENIZER_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
)
_ONNX_MAX_LENGTH = 256


def _default_model_dir() -> str:
    env_dir = os.environ.get("CODEWOOD_MODELS_DIR", "").strip()
    if env_dir:
        return os.path.join(env_dir, _EMBEDDING_MODEL_NAME)
    try:
        from ..config.app_info import get_app_global_config_dir
        return os.path.join(get_app_global_config_dir(), "models", _EMBEDDING_MODEL_NAME)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "models", _EMBEDDING_MODEL_NAME)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(os.path.dirname(script_dir))
    return os.path.join(root_dir, "models", _EMBEDDING_MODEL_NAME)


def _hf_download(rel_src: str, dest_path: str) -> bool:
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    url = f"{endpoint}/{_HF_REPO}/resolve/main/{rel_src}"
    logger.info("Downloading %s", url)
    try:
        import requests

        with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=True) as resp:
            resp.raise_for_status()
            tmp_path = dest_path + ".part"
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        logger.error("Failed to download %s: %s", url, e)
        try:
            if os.path.exists(dest_path + ".part"):
                os.remove(dest_path + ".part")
        except Exception:
            pass
        return False


def ensure_onnx_model(model_dir: str) -> Optional[str]:
    """Make sure ``model_dir`` contains the files needed by the ONNX embedding
    backend (tokenizer/config files plus ``onnx/model.onnx``), downloading any
    that are missing from Hugging Face. Returns the ONNX model path, or None
    when a required download fails."""
    os.makedirs(model_dir, exist_ok=True)
    for rel in _MODEL_TOKENIZER_FILES:
        dest = os.path.join(model_dir, rel)
        if not os.path.isfile(dest) and not _hf_download(rel, dest):
            return None
    onnx_dir = os.path.join(model_dir, "onnx")
    os.makedirs(onnx_dir, exist_ok=True)
    onnx_path = os.path.join(onnx_dir, "model.onnx")
    if not os.path.isfile(onnx_path) and not _hf_download("onnx/model.onnx", onnx_path):
        return None
    return onnx_path


def _resolve_model_path(model_name: str) -> Optional[str]:
    candidates: List[str] = []
    env_dir = os.environ.get("CODEWOOD_MODELS_DIR", "").strip()
    if env_dir:
        candidates.append(os.path.join(env_dir, model_name))
    try:
        from ..config.app_info import get_app_global_config_dir
        candidates.append(os.path.join(get_app_global_config_dir(), "models", model_name))
    except Exception:
        pass
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None
    if exe_dir:
        candidates.append(os.path.join(exe_dir, "models", model_name))
    if not exe_dir:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(os.path.dirname(script_dir))
        candidates.append(os.path.join(root_dir, "models", model_name))
    for c in candidates:
        if os.path.isdir(c) and os.path.isfile(os.path.join(c, "config.json")):
            logger.info("Found local embedding model at: %s", c)
            return c
    return None


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
        self._initialized: bool = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        provider = self._try_init_local_provider()
        if provider is not None:
            self._provider = provider
            self._provider_name = "local:all-MiniLM-L6-v2"
            logger.info("Embedding provider initialized: local model all-MiniLM-L6-v2")
            return

        provider = self._try_init_onnx_provider()
        if provider is not None:
            self._provider = provider
            self._provider_name = "onnx:all-MiniLM-L6-v2"
            logger.info("Embedding provider initialized: onnxruntime model all-MiniLM-L6-v2")
            return

        self._provider_name = "none"
        logger.warning("No embedding provider available")

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

    def _try_init_local_provider(self) -> Optional[Callable[[List[str]], List[np.ndarray]]]:
        try:
            import sentence_transformers  # type: ignore[import-untyped]
        except ImportError:
            return None

        for name in ("sentence_transformers", "transformers", "huggingface_hub", "filelock", "urllib3"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.ERROR)
            lg.propagate = False

        model_name = _EMBEDDING_MODEL_NAME
        model_path = _resolve_model_path(model_name)
        logger.info("Loading embedding model: model_name=%s model_path=%s", model_name, model_path)

        try:
            model = sentence_transformers.SentenceTransformer(
                model_path if model_path else model_name,
                device="cpu",
            )
        except Exception as e:
            logger.error("Failed to load embedding model: %s", e)
            return None

        self._local_model = model
        logger.info("Local embedding model loaded: all-MiniLM-L6-v2 (dim=%d)", _EMBEDDING_DIM)

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

    def _try_init_onnx_provider(self) -> Optional[Callable[[List[str]], List[np.ndarray]]]:
        """ONNX Runtime fallback used where sentence-transformers/torch cannot
        be installed (ARM64-native Windows publishes no torch wheels)."""
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError:
            return None

        # onnxruntime logs through its own native logger (stderr, not Python
        # logging). Its CPUID probe prints a harmless "Unknown CPU vendor"
        # warning on ARM64 Windows, so keep the native logger at ERROR to
        # avoid polluting the TUI output.
        try:
            ort.set_default_logger_severity(3)  # 3 = ERROR (hide warnings)
        except Exception:
            pass

        for name in ("onnxruntime", "tokenizers"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.ERROR)
            lg.propagate = False

        model_dir = _resolve_model_path(_EMBEDDING_MODEL_NAME)
        if model_dir is None:
            model_dir = _default_model_dir()
        onnx_path = ensure_onnx_model(model_dir)
        if not onnx_path:
            logger.warning("ONNX embedding model not available under %s", model_dir)
            return None

        try:
            tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
            tokenizer.enable_truncation(max_length=_ONNX_MAX_LENGTH)
            tokenizer.enable_padding()
            session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        except Exception as e:
            logger.error("Failed to load ONNX embedding model: %s", e)
            return None

        output_names = [o.name for o in session.get_outputs()]
        # Prefer the sentence_embedding output (already mean-pooled by the
        # export); fall back to pooling token_embeddings ourselves.
        output_index = output_names.index("sentence_embedding") if "sentence_embedding" in output_names else 0
        if output_index == 0:
            out_shape = session.get_outputs()[0].shape
            if not out_shape or out_shape[-1] != _EMBEDDING_DIM:
                logger.error("Unexpected ONNX model output shape: %s", out_shape)
                return None
        input_names = {i.name for i in session.get_inputs()}

        self._local_model = (tokenizer, session)
        logger.info("ONNX embedding model loaded: all-MiniLM-L6-v2 (dim=%d)", _EMBEDDING_DIM)

        def _onnx_embed(texts: List[str]) -> List[np.ndarray]:
            if not texts:
                return []
            encoded = tokenizer.encode_batch([str(t)[:4096] for t in texts])
            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feed: Dict[str, np.ndarray] = {"input_ids": input_ids, "attention_mask": attention_mask}
            if "token_type_ids" in input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            output = session.run(None, feed)[output_index]
            if output.ndim == 3:
                mask = attention_mask[:, :, None].astype(np.float32)
                summed = (output * mask).sum(axis=1)
                counts = np.clip(mask.sum(axis=1), 1e-9, None)
                output = summed / counts
            norms = np.clip(np.linalg.norm(output, axis=1, keepdims=True), 1e-12, None)
            normalized = output / norms
            return [np.array(r, dtype=np.float32) for r in normalized]

        return _onnx_embed


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
            conn.execute("BEGIN")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    rel TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    indexed_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_rel ON embeddings(rel)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    file_rel TEXT NOT NULL,
                    chunk_name TEXT NOT NULL,
                    chunk_kind TEXT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    indexed_at REAL NOT NULL,
                    PRIMARY KEY (file_rel, chunk_name)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunk_file ON chunk_embeddings(file_rel)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            conn.execute("COMMIT")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_provider(self) -> Optional[EmbeddingProvider]:
        return self._provider

    def initialize_provider(self) -> EmbeddingProvider:
        with self._lock:
            if self._provider is not None:
                return self._provider
            provider = EmbeddingProvider()
            provider.initialize()
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

    def has_chunk_embeddings(self) -> bool:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()
            return int(row[0]) > 0 if row else False
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def index_chunks(
        self,
        chunks: List[Dict[str, str]],
        provider: Optional[EmbeddingProvider] = None,
    ) -> Dict[str, Any]:
        ep = provider or self._provider
        if ep is None or not ep.available:
            return {"success": False, "error": "No embedding provider available", "indexed": 0}
        if not chunks:
            return {"success": True, "indexed": 0, "elapsed_ms": 0}

        t0 = time.time()
        texts = [chunk["chunk_text"][:2048] for chunk in chunks]
        embeddings = ep.embed(texts)
        if len(embeddings) != len(chunks):
            return {"success": False, "error": "Embedding count mismatch", "indexed": 0}

        conn = self._connect()
        now = time.time()
        try:
            conn.execute("DELETE FROM chunk_embeddings")
            conn.executemany(
                "INSERT OR REPLACE INTO chunk_embeddings (file_rel, chunk_name, chunk_kind, chunk_text, embedding, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        chunk["file_rel"],
                        chunk["chunk_name"],
                        chunk["chunk_kind"],
                        chunk["chunk_text"],
                        emb.tobytes(),
                        now,
                    )
                    for chunk, emb in zip(chunks, embeddings)
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
            "indexed": len(chunks),
            "elapsed_ms": elapsed_ms,
            "provider": ep.provider_name,
        }

    def chunk_search(
        self,
        query: str,
        candidate_files: Optional[List[str]] = None,
        provider: Optional[EmbeddingProvider] = None,
        top_k: int = 12,
    ) -> List[Dict[str, Any]]:
        ep = provider or self._provider
        if ep is None or not ep.available:
            return []

        query_vecs = ep.embed([query[:4096]])
        if not query_vecs or len(query_vecs) == 0:
            return []
        query_vec = query_vecs[0]

        conn = self._connect()
        try:
            if candidate_files:
                placeholders = ",".join("?" * len(candidate_files))
                rows = conn.execute(
                    f"SELECT file_rel, chunk_name, chunk_kind, chunk_text, embedding "
                    f"FROM chunk_embeddings WHERE file_rel IN ({placeholders})",
                    candidate_files,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT file_rel, chunk_name, chunk_kind, chunk_text, embedding "
                    "FROM chunk_embeddings"
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
            file_rel = str(row[0])
            chunk_name = str(row[1])
            chunk_kind = str(row[2])
            chunk_text = str(row[3])
            emb = np.frombuffer(row[4], dtype=np.float32)
            cos_sim = _cosine_similarity(query_vec, emb)
            if cos_sim <= 0:
                continue
            scored.append((cos_sim, {
                "file_rel": file_rel,
                "chunk_name": chunk_name,
                "chunk_kind": chunk_kind,
                "chunk_text": chunk_text,
                "score": round(cos_sim, 3),
            }))

        scored.sort(key=lambda x: x[0], reverse=True)

        file_scores: Dict[str, float] = {}
        top_chunks: Dict[str, List[Dict[str, Any]]] = {}
        for _, chunk_info in scored[:max(1, top_k * 3)]:
            fr = chunk_info["file_rel"]
            s = chunk_info["score"]
            file_scores[fr] = max(file_scores.get(fr, 0.0), s)
            if fr not in top_chunks:
                top_chunks[fr] = []
            top_chunks[fr].append(chunk_info)

        result_files = sorted(file_scores.keys(), key=lambda f: file_scores[f], reverse=True)[:top_k]
        return [
            {
                "path": fr,
                "score": round(file_scores[fr], 3),
                "chunks": top_chunks[fr][:5],
            }
            for fr in result_files
        ]
