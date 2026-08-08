"""Global cross-workspace chat full-text search index.

Tokenizer: Chinese text is segmented with ``jieba`` (dictionary word
segmentation), with a bigram fallback for out-of-vocabulary runs and a
stopword filter for single-character function words. ``jieba`` is imported
lazily on first use (indexing happens in the background refresher thread, so
app startup speed is unaffected); the first index build pays the dictionary
load cost.

Maintains an inverted index of user prompts and model replies (``content``
only — thinking blocks, tool calls and tool results are excluded) across every
workspace's non-archived chats. The database lives in the global config
directory under ``search/chat_index.db`` so a single index spans all
workspaces.

Index updates are incremental per chat: a fingerprint (file mtime/size +
message count + last ``created_at``) is stored next to each chat, so only
changed record files are re-tokenized. Query time touches only SQLite
index lookups, so results return near-instantly.

Tokenization splits text into ASCII words (lowercased) plus CJK single
characters and sliding bigrams, so a query is decomposed into multiple
keywords and a message matches when ANY keyword appears; results rank by the
number of distinct keywords matched first, then by tf·idf score, then
recency.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import sqlite3
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..agent import (
    DIRECT_SHELL_USER_HISTORY_PREFIX,
    INTERNAL_SLASH_USER_HISTORY_PREFIX,
)
from ..config.app_info import get_app_global_config_dir
from ..controllers.chat_command_controller import _genuine_user_positions_in_list

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2
_DB_DIR = "search"
_DB_FILE = "chat_index.db"
_CHATS_SUBDIR = "chats"
_CHAT_STATE_FILE = "chats.json"

# Bounds so a single pathological message/query can never balloon the index.
_MAX_TOKENS_PER_MESSAGE = 2000
_MAX_KEYWORDS = 32
_MAX_CANDIDATES_PER_KEYWORD = 5000
_SNIPPET_RADIUS = 45

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")

# High-frequency single-character function words filtered on BOTH the index
# and query side so they can never pollute results (``的``, ``了``, ``是`` …).
_CJK_STOPWORDS = frozenset(
    "的了是在有和与就都也我不他一你这那而并或及对为从把被将等很到要会可去说能上"
    "下中内个只于之其该各又再但若如因所被让向往自先同随后且即则却虽仍"
)

# Lazily imported ``jieba`` module (shared across ChatSearchIndex instances so
# the ~1s dictionary load happens exactly once, off the startup path).
_jieba: Any = None
_seg_lock = threading.Lock()

# Token weights: full words (ASCII + jieba dictionary words) are the primary
# signal (1.0); bigram fallbacks inside out-of-vocabulary runs (0.5) and
# standalone single characters (0.3) only contribute secondarily, so a
# message matching the actual words always outranks one matching stray chars.
_WEIGHT_WORD = 1.0
_WEIGHT_BIGRAM = 0.5
_WEIGHT_CHAR = 0.3


def _get_jieba() -> Any:
    global _jieba
    if _jieba is None:
        # jieba 0.42.1 imports the long-deprecated pkg_resources in
        # ``jieba/_compat.py``; setuptools>=81 then emits a UserWarning on
        # every import. Filter exactly that message so search logs stay clean.
        warnings.filterwarnings(
            "ignore",
            message=r"pkg_resources is deprecated as an API.*",
            category=UserWarning,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba

        try:
            jieba.setLogLevel(60)  # silence the "Prefix dict built" banner
        except Exception:
            pass
        _jieba = jieba
    return _jieba


def _segment_cjk(
    text: str,
    start: int,
    end: int,
    out: List[Tuple[str, float, int]],
) -> None:
    """Segment a pure-CJK slice with jieba and append ``(token, weight, abs_pos)``.

    Dictionary words are kept whole (weight 1.0); runs jieba cannot cover get
    a sliding-bigram fallback (0.5); standalone non-stopword characters (0.3)
    are kept so single-character queries still match.
    """
    jb = _get_jieba()
    try:
        with _seg_lock:
            for word, ws, _we in jb.tokenize(text[start:end]):
                if not word or not _HAN_RE.fullmatch(word):
                    continue
                pos = start + ws
                wlen = len(word)
                if wlen == 1:
                    if word not in _CJK_STOPWORDS:
                        out.append((word, _WEIGHT_CHAR, pos))
                    continue
                out.append((word, _WEIGHT_WORD, pos))
                if wlen > 2:
                    for i in range(wlen - 1):
                        out.append((word[i : i + 2], _WEIGHT_BIGRAM, pos + i))
    except Exception:
        # jieba unavailable/corrupt: fall back to plain bigrams so search still
        # works without the dependency.
        run = text[start:end]
        n = len(run)
        if n == 1:
            if run not in _CJK_STOPWORDS:
                out.append((run, _WEIGHT_CHAR, start))
        else:
            for i in range(n - 1):
                out.append((run[i : i + 2], _WEIGHT_BIGRAM, start + i))


def tokenize_with_weights(text: str) -> List[Tuple[str, float, int]]:
    """Tokenize *text* into ``(token, weight, char_offset)`` in document order.

    ASCII words are kept whole (lowercased) with the existing regex; only CJK
    runs go through jieba, so English/code text behaves exactly as before.
    """
    text = str(text or "")
    out: List[Tuple[str, float, int]] = []
    pos = 0
    for m in _WORD_RE.finditer(text):
        if m.start() > pos:
            _segment_cjk(text, pos, m.start(), out)
        out.append((m.group(0).lower(), _WEIGHT_WORD, m.start()))
        pos = m.end()
    if pos < len(text):
        _segment_cjk(text, pos, len(text), out)
    if len(out) > _MAX_TOKENS_PER_MESSAGE:
        out = out[:_MAX_TOKENS_PER_MESSAGE]
    return out


def tokenize_text(text: str) -> List[str]:
    """Distinct tokens for *text* (document order) — query keywords."""
    seen: Dict[str, None] = {}
    out: List[str] = []
    for tok, _w, _pos in tokenize_with_weights(text):
        if tok not in seen:
            seen[tok] = None
            out.append(tok)
        if len(out) >= _MAX_TOKENS_PER_MESSAGE:
            break
    return out


def tokenize_query(text: str) -> List[Tuple[str, float]]:
    """Query-side keywords with weights: ``[(token, weight)]`` (deduplicated)."""
    merged: Dict[str, float] = {}
    for tok, weight, _pos in tokenize_with_weights(text):
        if tok not in merged or weight > merged[tok]:
            merged[tok] = weight
    return list(merged.items())


def _tokenize_with_positions(text: str) -> Dict[str, List[int]]:
    """Tokenize *text* into ``{token: [char offsets]}`` (index side)."""
    text = str(text or "")
    out: Dict[str, List[int]] = {}
    for tok, _w, pos in tokenize_with_weights(text):
        positions = out.get(tok)
        if positions is None:
            out[tok] = [pos]
        elif len(positions) < _MAX_TOKENS_PER_MESSAGE:
            positions.append(pos)
    return out


def _build_indexable_messages(
    messages: Any,
    ws_id: str,
    ws_name: str,
    chat_id: str,
    chat_name: str,
) -> List[Tuple[Any, ...]]:
    """Extract the rows worth indexing from a chat record's message list.

    Only genuine user prompts and model replies (their ``content`` field) are
    indexed. Internal bookkeeping messages (slash commands, direct shell),
    thinking blocks, tool-call JSON and ``role=="tool"`` results are excluded.

    ``turn_idx`` mirrors the GUI's turn grouping (``_build_structured_turns``
    starts a new turn at every genuine user message), so a search hit maps
    exactly onto the rendered history turn the frontend should scroll to.
    """
    genuine = _genuine_user_positions_in_list(messages)
    out: List[Tuple[Any, ...]] = []
    for i, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        content = str(msg.get("content") or "")
        if not content:
            continue
        if role == "user":
            if msg.get("_internal"):
                continue
            if content.startswith(
                DIRECT_SHELL_USER_HISTORY_PREFIX
            ) or content.startswith(INTERNAL_SLASH_USER_HISTORY_PREFIX):
                continue
        elif role == "assistant":
            # Index only the model's reply text. Thinking blocks, tool-call
            # payloads and tool results live in other fields/messages.
            pass
        else:
            continue
        turn_idx = bisect.bisect_right(genuine, i) - 1
        if turn_idx < 0:
            turn_idx = 0
        created = str(msg.get("created_at") or "")
        updated = str(msg.get("updated_at") or created)
        out.append(
            (
                ws_id,
                ws_name,
                chat_id,
                chat_name,
                i,
                role,
                content,
                created,
                updated,
                turn_idx,
            )
        )
    return out


def _build_snippet(
    content: str,
    kws: Dict[str, Tuple[int, List[int]]],
) -> Tuple[str, List[Tuple[int, int]]]:
    """Build a display snippet around the earliest hit plus merged ranges."""
    text = str(content or "")
    if not text:
        return "", []
    hits: List[Tuple[int, int]] = []
    for kw, (_tf, positions) in kws.items():
        for p in positions:
            hits.append((p, p + len(kw)))
    if not hits:
        return text[: _SNIPPET_RADIUS * 2], []
    hits.sort()
    first = hits[0][0]
    start = max(0, first - _SNIPPET_RADIUS)
    end = min(len(text), first + _SNIPPET_RADIUS + 40)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    snippet = prefix + text[start:end] + suffix
    offset = len(prefix)
    ranges: List[Tuple[int, int]] = []
    for s, e in hits:
        if s >= end:
            break
        if e <= start:
            continue
        ranges.append(
            (
                max(0, s - start) + offset,
                min(len(snippet) - len(suffix), e - start) + offset,
            )
        )
    merged: List[Tuple[int, int]] = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return snippet, merged


class ChatSearchIndex:
    """SQLite-backed inverted index over all workspaces' chat messages."""

    def __init__(self, global_config_dir: Optional[Path] = None) -> None:
        base = (
            Path(global_config_dir)
            if global_config_dir is not None
            else get_app_global_config_dir()
        )
        self.db_path = base / _DB_DIR / _DB_FILE
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._lock = threading.RLock()
        self._init_db()

    # -- schema -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ws_id TEXT NOT NULL,
                        ws_name TEXT NOT NULL DEFAULT '',
                        chat_id TEXT NOT NULL,
                        chat_name TEXT NOT NULL DEFAULT '',
                        msg_idx INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT '',
                        turn_idx INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_message
                        ON messages(ws_id, chat_id, msg_idx);
                    CREATE INDEX IF NOT EXISTS idx_message_chat
                        ON messages(ws_id, chat_id);
                    CREATE TABLE IF NOT EXISTS terms(
                        term TEXT NOT NULL,
                        msg_id INTEGER NOT NULL,
                        tf INTEGER NOT NULL,
                        positions TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
                    CREATE INDEX IF NOT EXISTS idx_terms_msg ON terms(msg_id);
                    """
                )
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if row is not None and str(row[0]) != str(_SCHEMA_VERSION):
                    # Tokenizer changed: drop everything and let the refresher
                    # rebuild from scratch (background, so no startup cost).
                    conn.execute("DELETE FROM terms")
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM meta WHERE key LIKE 'fp:%'")
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def warmup(self) -> None:
        """Force-load the jieba dictionary (called from the background
        refresher thread so the first interactive query never pays for it)."""
        try:
            tokenize_with_weights("预热分词")
        except Exception:
            pass

    def _chat_fingerprint(self, ws_id: str, chat_id: str) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (f"fp:{ws_id}:{chat_id}",)
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    # -- indexing ---------------------------------------------------------

    def refresh_workspace(
        self, ws_id: str, ws_name: str, storage_dir: Any
    ) -> Dict[str, Any]:
        """(Re)index every non-archived chat in one workspace.

        Returns ``{indexed, skipped, removed, chats}`` stats. Only record
        files whose fingerprint changed are re-tokenized.
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "chats": 0}
        chats_dir = Path(storage_dir) / _CHATS_SUBDIR
        index_path = chats_dir / _CHAT_STATE_FILE
        entries: List[Dict[str, Any]] = []
        try:
            if index_path.is_file():
                with open(index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("chats") if isinstance(data, dict) else None
                if isinstance(raw, list):
                    entries = [e for e in raw if isinstance(e, dict)]
        except Exception:
            return stats
        known: set = set()
        for entry in entries:
            cid = str(entry.get("id") or "").strip()
            record_file = str(entry.get("record_file") or "").strip()
            if not cid or not record_file:
                continue
            known.add(cid)
            if bool(entry.get("archived", False)):
                self.invalidate_chat(ws_id, cid)
                continue
            rel = Path(record_file)
            if rel.is_absolute() or rel.name != record_file:
                continue
            record_path = chats_dir / record_file
            if not record_path.is_file():
                self.invalidate_chat(ws_id, cid)
                continue
            stats["chats"] += 1
            if self._index_chat_record(
                ws_id, ws_name, cid, str(entry.get("name") or ""), record_path
            ):
                stats["indexed"] += 1
            else:
                stats["skipped"] += 1
        stats["removed"] = self._remove_missing_chats(ws_id, known)
        return stats

    def _index_chat_record(
        self,
        ws_id: str,
        ws_name: str,
        chat_id: str,
        chat_name: str,
        record_path: Path,
    ) -> bool:
        try:
            with open(record_path, "r", encoding="utf-8") as f:
                chat = json.load(f)
        except Exception:
            return False
        if not isinstance(chat, dict):
            return False
        messages = chat.get("messages")
        if not isinstance(messages, list):
            messages = []
        try:
            st = record_path.stat()
            mtime = str(st.st_mtime_ns)
            size = str(st.st_size)
        except Exception:
            mtime, size = "", ""
        last_ts = ""
        for m in reversed(messages):
            ts = str(m.get("created_at") or "") if isinstance(m, dict) else ""
            if ts:
                last_ts = ts
                break
        fp = f"{mtime}:{size}:{len(messages)}:{last_ts}"
        if self._chat_fingerprint(ws_id, chat_id) == fp:
            return False
        rows = _build_indexable_messages(
            messages, ws_id, ws_name, chat_id, chat_name
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                old_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM messages WHERE ws_id=? AND chat_id=?",
                        (ws_id, chat_id),
                    ).fetchall()
                ]
                if old_ids:
                    conn.executemany(
                        "DELETE FROM terms WHERE msg_id=?", [(i,) for i in old_ids]
                    )
                conn.execute(
                    "DELETE FROM messages WHERE ws_id=? AND chat_id=?",
                    (ws_id, chat_id),
                )
                for row in rows:
                    cur = conn.execute(
                        "INSERT INTO messages(ws_id, ws_name, chat_id, chat_name,"
                        " msg_idx, role, content, created_at, updated_at, turn_idx)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        row,
                    )
                    mid = int(cur.lastrowid)
                    for term, positions in _tokenize_with_positions(row[6]).items():
                        conn.execute(
                            "INSERT INTO terms(term, msg_id, tf, positions)"
                            " VALUES (?,?,?,?)",
                            (term, mid, len(positions), json.dumps(positions)),
                        )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (f"fp:{ws_id}:{chat_id}", fp),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return True

    def invalidate_chat(self, ws_id: str, chat_id: str) -> None:
        """Drop a chat (and its fingerprint) from the index immediately."""
        ws_id = str(ws_id or "").strip()
        chat_id = str(chat_id or "").strip()
        if not ws_id or not chat_id:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM messages WHERE ws_id=? AND chat_id=?",
                        (ws_id, chat_id),
                    ).fetchall()
                ]
                if ids:
                    conn.executemany(
                        "DELETE FROM terms WHERE msg_id=?", [(i,) for i in ids]
                    )
                conn.execute(
                    "DELETE FROM messages WHERE ws_id=? AND chat_id=?",
                    (ws_id, chat_id),
                )
                conn.execute(
                    "DELETE FROM meta WHERE key=?", (f"fp:{ws_id}:{chat_id}",)
                )
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()

    def _remove_missing_chats(self, ws_id: str, known: set) -> int:
        """Drop indexed chats of *ws_id* that no longer exist on disk."""
        with self._lock:
            conn = self._connect()
            removed = 0
            try:
                conn.execute("BEGIN")
                rows = conn.execute(
                    "SELECT DISTINCT chat_id FROM messages WHERE ws_id=?",
                    (ws_id,),
                ).fetchall()
                for (cid,) in rows:
                    if cid in known:
                        continue
                    ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT id FROM messages WHERE ws_id=? AND chat_id=?",
                            (ws_id, cid),
                        ).fetchall()
                    ]
                    if ids:
                        conn.executemany(
                            "DELETE FROM terms WHERE msg_id=?", [(i,) for i in ids]
                        )
                    conn.execute(
                        "DELETE FROM messages WHERE ws_id=? AND chat_id=?",
                        (ws_id, cid),
                    )
                    conn.execute(
                        "DELETE FROM meta WHERE key=?", (f"fp:{ws_id}:{cid}",)
                    )
                    removed += 1
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()
        return removed

    # -- querying ---------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> Dict[str, Any]:
        """Full-text search over all indexed messages.

        The query is split into multiple keywords (OR semantics). Results are
        ordered by (weighted keyword coverage — whole words count more than
        bigram/char fallbacks, tf·idf score, recency) and each carries a
        snippet with highlight ranges plus the absolute ``turnIdx`` of the
        containing history turn.
        """
        query_tokens = tokenize_query(query)[:_MAX_KEYWORDS]
        if not query_tokens:
            return {"keywords": [], "total": 0, "results": []}
        keywords = [kw for kw, _w in query_tokens]
        kw_weight = dict(query_tokens)
        with self._lock:
            conn = self._connect()
            try:
                n_docs = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
                df: Dict[str, int] = {}
                postings: Dict[str, Dict[int, Tuple[int, List[int]]]] = {}
                for kw in keywords:
                    rows = conn.execute(
                        "SELECT msg_id, tf, positions FROM terms WHERE term=?", (kw,)
                    ).fetchall()
                    df[kw] = len(rows)
                    bucket: Dict[int, Tuple[int, List[int]]] = {}
                    for mid, tf, pos_json in rows:
                        try:
                            positions = json.loads(pos_json)
                        except Exception:
                            positions = []
                        bucket[int(mid)] = (int(tf), positions)
                        if len(bucket) >= _MAX_CANDIDATES_PER_KEYWORD:
                            break
                    postings[kw] = bucket
                matched: Dict[int, Dict[str, Tuple[int, List[int]]]] = {}
                for kw, bucket in postings.items():
                    for mid, tfpos in bucket.items():
                        matched.setdefault(mid, {})[kw] = tfpos
                if not matched:
                    return {"keywords": keywords, "total": 0, "results": []}

                def _idf(kw: str) -> float:
                    d = df.get(kw, 0)
                    return math.log(1.0 + (n_docs + 1) / (d + 1)) if d else 0.0

                scored: List[Tuple[int, float, float]] = []
                for mid, kws in matched.items():
                    coverage = sum(kw_weight[kw] for kw in kws)
                    score = sum(float(tf) * _idf(kw) for kw, (tf, _p) in kws.items())
                    scored.append((mid, coverage, score))
                scored.sort(key=lambda t: (-t[1], -t[2], t[0]))
                top = scored[: max(1, min(50, int(limit) or 20))]

                rows_by_id: Dict[int, Tuple[Any, ...]] = {}
                if top:
                    placeholders = ",".join("?" for _ in top)
                    for row in conn.execute(
                        "SELECT id, ws_id, ws_name, chat_id, chat_name, msg_idx,"
                        " role, content, created_at, updated_at, turn_idx"
                        f" FROM messages WHERE id IN ({placeholders})",
                        [t[0] for t in top],
                    ):
                        rows_by_id[int(row[0])] = row

                results: List[Dict[str, Any]] = []
                for mid, _coverage, score in top:
                    row = rows_by_id.get(mid)
                    if row is None:
                        continue
                    (_id, ws_id, ws_name, chat_id, chat_name, msg_idx, role,
                     content, created_at, updated_at, turn_idx) = row
                    kws_here = matched[mid]
                    snippet, ranges = _build_snippet(content, kws_here)
                    results.append(
                        {
                            "wsId": ws_id,
                            "wsName": ws_name,
                            "chatId": chat_id,
                            "chatName": chat_name,
                            "msgIdx": msg_idx,
                            "role": role,
                            "turnIdx": turn_idx,
                            "snippet": snippet,
                            "ranges": ranges,
                            "keywords": [kw for kw in keywords if kw in kws_here],
                            "score": round(score, 4),
                            "updatedAt": updated_at or created_at or "",
                        }
                    )
                return {
                    "keywords": keywords,
                    "total": len(scored),
                    "results": results,
                }
            finally:
                conn.close()
