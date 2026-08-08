import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from cli.services.chat_search_index import (
    ChatSearchIndex,
    _CJK_STOPWORDS,
    _build_snippet,
    tokenize_query,
    tokenize_text,
)

try:
    import jieba  # noqa: F401

    _HAS_JIEBA = True
except Exception:
    _HAS_JIEBA = False

needs_jieba = unittest.skipUnless(_HAS_JIEBA, "jieba is not installed")


def _write_chat(
    chats_dir: Path,
    chat_id: str,
    record_file: str,
    name: str,
    messages: list,
    archived: bool = False,
) -> None:
    index_path = chats_dir / "chats.json"
    chats = []
    if index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        chats = data.get("chats") or []
    chats.append(
        {
            "id": chat_id,
            "name": name,
            "record_file": record_file,
            "archived": archived,
            "updated_at": "2026-01-01 00:00:00",
        }
    )
    index_path.write_text(
        json.dumps({"version": 1, "active": chat_id, "chats": chats}, ensure_ascii=False),
        encoding="utf-8",
    )
    (chats_dir / record_file).write_text(
        json.dumps({"id": chat_id, "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )


class TokenizeTextTests(unittest.TestCase):
    def test_ascii_words_lowercased(self):
        self.assertEqual(tokenize_text("Python SSearch!"), ["python", "ssearch"])

    @needs_jieba
    def test_cjk_dictionary_words(self):
        # jieba covers common words; stopwords/standalone chars are dropped.
        self.assertEqual(tokenize_text("搜索框"), ["搜索", "框"])
        tokens = tokenize_text("搜索引擎优化")
        self.assertIn("搜索引擎", tokens)
        self.assertIn("优化", tokens)

    @needs_jieba
    def test_oov_bigram_fallback_weights(self):
        fallback = dict(tokenize_query("搜索引擎"))
        self.assertEqual(fallback.get("搜索引擎"), 1.0)
        self.assertEqual(fallback.get("搜索"), 0.5)
        self.assertEqual(fallback.get("引擎"), 0.5)

    @needs_jieba
    def test_stopwords_filtered(self):
        self.assertEqual(tokenize_text("的"), [])
        self.assertNotIn("的", tokenize_text("这是一个搜索的框"))
        for ch in "的了是在":
            self.assertIn(ch, _CJK_STOPWORDS)

    @needs_jieba
    def test_single_han_char_kept(self):
        self.assertEqual(tokenize_text("搜"), ["搜"])

    def test_mixed_text(self):
        tokens = tokenize_text("修复 bug 搜索框")
        self.assertIn("bug", tokens)
        self.assertIn("修复", tokens)
        self.assertIn("搜索", tokens)


class ChatSearchIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.global_dir = self.root / "global"
        self.storage = self.root / "ws1" / ".codewood"
        self.chats_dir = self.storage / "chats"
        self.chats_dir.mkdir(parents=True)
        self.index = ChatSearchIndex(self.global_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index_default_workspace(self) -> dict:
        return self.index.refresh_workspace("ws1", "Workspace 1", self.storage)

    def test_tokenizer_shared_between_index_and_query(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "搜索",
            [
                {"role": "user", "content": "如何修复搜索框？", "created_at": "2026-01-01 10:00:00"},
                {"role": "assistant", "content": "用索引极速返回。", "created_at": "2026-01-01 10:00:05"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("搜索")
        self.assertEqual(r["total"], 1)  # only the user message contains 搜索
        self.assertIn("搜索", r["keywords"])

    def test_archived_chat_excluded(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "活跃",
            [{"role": "user", "content": "搜索框布局", "created_at": "2026-01-01 10:00:00"}],
        )
        _write_chat(
            self.chats_dir,
            "c2",
            "b.json",
            "归档",
            [{"role": "user", "content": "搜索框归档内容", "created_at": "2026-01-01 10:00:00"}],
            archived=True,
        )
        self._index_default_workspace()
        r = self.index.search("搜索框")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["chatId"], "c1")

    def test_thinking_tool_and_internal_messages_not_indexed(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "过滤",
            [
                {"role": "user", "content": "请帮我看看这个报错", "created_at": "2026-01-01 10:00:00"},
                {"role": "assistant", "content": "好的，正在分析。", "created_at": "2026-01-01 10:00:01"},
                {
                    "role": "assistant",
                    "content": "",
                    "_thinking": "思考内容不应被索引",
                    "created_at": "2026-01-01 10:00:02",
                },
                {"role": "tool", "content": "工具结果不应被索引", "created_at": "2026-01-01 10:00:03"},
                {"role": "user", "content": "[DIRECT_SHELL_USER_COMMAND] ls -la", "created_at": "2026-01-01 10:00:04"},
                {"role": "user", "_internal": True, "content": "内部消息不应被索引", "created_at": "2026-01-01 10:00:05"},
            ],
        )
        self._index_default_workspace()
        self.assertEqual(self.index.search("思考内容")["total"], 0)
        self.assertEqual(self.index.search("工具结果")["total"], 0)
        self.assertEqual(self.index.search("ls -la")["total"], 0)
        self.assertEqual(self.index.search("内部消息")["total"], 0)
        self.assertEqual(self.index.search("分析")["total"], 1)

    def test_turn_idx_matches_genuine_user_boundaries(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "轮次",
            [
                {"role": "user", "content": "alpha-question", "created_at": "2026-01-01 10:00:00"},
                {"role": "assistant", "content": "alpha-answer", "created_at": "2026-01-01 10:00:01"},
                {"role": "user", "content": "beta-question", "created_at": "2026-01-01 10:00:02"},
                {"role": "assistant", "content": "beta-answer", "created_at": "2026-01-01 10:00:03"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("alpha")
        self.assertEqual(r["total"], 2)
        turns = {h["msgIdx"]: h["turnIdx"] for h in r["results"]}
        self.assertEqual(turns.get(0), 0)  # "alpha-question" -> turn 0
        self.assertEqual(turns.get(1), 0)  # "alpha-answer" -> turn 0
        r2 = self.index.search("beta")
        turns2 = {h["msgIdx"]: h["turnIdx"] for h in r2["results"]}
        self.assertEqual(turns2.get(2), 1)  # "beta-question" -> turn 1
        self.assertEqual(turns2.get(3), 1)  # "beta-answer" -> turn 1

    def test_multi_keyword_ranking_more_matches_first(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "排序",
            [
                {"role": "user", "content": "优化搜索体验", "created_at": "2026-01-01 10:00:00"},
                {"role": "assistant", "content": "极速返回结果", "created_at": "2026-01-01 10:00:01"},
                {"role": "user", "content": "极速搜索优化", "created_at": "2026-01-01 10:00:02"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("搜索 极速")
        self.assertGreaterEqual(r["total"], 2)
        first, second = r["results"][0], r["results"][1]
        self.assertGreaterEqual(
            len(first["keywords"]),
            len(second["keywords"]),
            "message matching more keywords must rank first",
        )

    def test_or_semantics_any_keyword_matches(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "OR",
            [
                {"role": "user", "content": "alpha 讨论", "created_at": "2026-01-01 10:00:00"},
                {"role": "user", "content": "beta 讨论", "created_at": "2026-01-01 10:00:01"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("alpha beta")
        self.assertEqual(r["total"], 2)

    def test_snippet_ranges_are_valid(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "片段",
            [
                {
                    "role": "user",
                    "content": "这是一个很长的句子，用来测试摘要片段的生成逻辑是否正确。",
                    "created_at": "2026-01-01 10:00:00",
                }
            ],
        )
        self._index_default_workspace()
        r = self.index.search("摘要")
        self.assertEqual(r["total"], 1)
        hit = r["results"][0]
        self.assertTrue(hit["snippet"])
        for start, end in hit["ranges"]:
            self.assertGreaterEqual(start, 0)
            self.assertLess(start, end)
            self.assertLessEqual(end, len(hit["snippet"]))

    def test_incremental_refresh_skips_unchanged(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "增量",
            [{"role": "user", "content": "第一版内容", "created_at": "2026-01-01 10:00:00"}],
        )
        stats1 = self._index_default_workspace()
        self.assertEqual(stats1["indexed"], 1)
        stats2 = self._index_default_workspace()
        self.assertEqual(stats2["indexed"], 0)
        self.assertEqual(stats2["skipped"], 1)
        # Touch + change the record: must re-index.
        record = self.chats_dir / "a.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data["messages"].append(
            {"role": "assistant", "content": "recheck-v2 reply", "created_at": "2026-01-01 10:00:01"}
        )
        record.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # Force a distinct mtime (some filesystems have coarse timestamps).
        old = record.stat().st_mtime_ns
        while record.stat().st_mtime_ns == old:
            os.utime(record, None)
            time.sleep(0.01)
        stats3 = self._index_default_workspace()
        self.assertEqual(stats3["indexed"], 1)
        self.assertEqual(self.index.search("recheck")["total"], 1)

    def test_invalidate_and_missing_chat_removal(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "删除",
            [{"role": "user", "content": "待删除内容", "created_at": "2026-01-01 10:00:00"}],
        )
        _write_chat(
            self.chats_dir,
            "c2",
            "b.json",
            "保留",
            [{"role": "user", "content": "保留内容", "created_at": "2026-01-01 10:00:00"}],
        )
        self._index_default_workspace()
        self.assertEqual(self.index.search("待删除")["total"], 1)
        self.index.invalidate_chat("ws1", "c1")
        self.assertEqual(self.index.search("待删除")["total"], 0)
        self.assertEqual(self.index.search("保留")["total"], 1)
        # Simulate a chat deleted by another process: refresh must drop it.
        (self.chats_dir / "b.json").unlink()
        index = json.loads((self.chats_dir / "chats.json").read_text(encoding="utf-8"))
        index["chats"] = [c for c in index["chats"] if c["id"] != "c2"]
        (self.chats_dir / "chats.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )
        stats = self._index_default_workspace()
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(self.index.search("保留")["total"], 0)

    def test_cross_workspace_results_carry_workspace_info(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "工作区一",
            [{"role": "user", "content": "跨区搜索", "created_at": "2026-01-01 10:00:00"}],
        )
        storage2 = self.root / "ws2" / ".codewood"
        chats2 = storage2 / "chats"
        chats2.mkdir(parents=True)
        _write_chat(
            chats2,
            "c1",
            "a.json",
            "工作区二",
            [{"role": "user", "content": "跨区搜索", "created_at": "2026-01-01 10:00:00"}],
        )
        self._index_default_workspace()
        self.index.refresh_workspace("ws2", "Workspace 2", storage2)
        r = self.index.search("跨区")
        self.assertEqual(r["total"], 2)
        ws_ids = {(h["wsId"], h["wsName"]) for h in r["results"]}
        self.assertEqual(ws_ids, {("ws1", "Workspace 1"), ("ws2", "Workspace 2")})

    @needs_jieba
    def test_single_char_query_produces_no_noise(self):
        # A message containing only the char 索 must NOT match a 搜索框 query.
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "噪音",
            [
                {"role": "user", "content": "搜索框优化", "created_at": "2026-01-01 10:00:00"},
                {"role": "user", "content": "索引力测试", "created_at": "2026-01-01 10:00:01"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("搜索框")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["msgIdx"], 0)

    @needs_jieba
    def test_stopword_query_returns_nothing(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "停用词",
            [
                {"role": "user", "content": "这是一个测试消息", "created_at": "2026-01-01 10:00:00"}
            ],
        )
        self._index_default_workspace()
        r = self.index.search("的")
        self.assertEqual(r["total"], 0)

    @needs_jieba
    def test_weighted_coverage_ranks_more_words_first(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "加权",
            [
                {"role": "user", "content": "搜索与优化", "created_at": "2026-01-01 10:00:00"},
                {"role": "user", "content": "搜索框样式", "created_at": "2026-01-01 10:00:01"},
            ],
        )
        self._index_default_workspace()
        r = self.index.search("搜索 优化")
        self.assertEqual(r["total"], 2)
        first, second = r["results"][0], r["results"][1]
        self.assertEqual(first["msgIdx"], 0)  # matches 搜索 + 优化 (weight 2.0)
        self.assertEqual(second["msgIdx"], 1)  # only 搜索 (weight 1.0)

    def test_schema_version_mismatch_rebuilds_index(self):
        _write_chat(
            self.chats_dir,
            "c1",
            "a.json",
            "重建",
            [{"role": "user", "content": "rebuild probe", "created_at": "2026-01-01 10:00:00"}],
        )
        self._index_default_workspace()
        self.assertEqual(self.index.search("rebuild")["total"], 1)
        # Simulate an old schema: rewrite meta and re-instantiate.
        import sqlite3

        conn = sqlite3.connect(str(self.index.db_path))
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
        conn.close()
        reopened = ChatSearchIndex(self.global_dir)
        self.assertEqual(reopened.search("rebuild")["total"], 0)  # wiped


class SnippetBuilderTests(unittest.TestCase):
    def test_merges_overlapping_ranges(self):
        snippet, ranges = _build_snippet(
            "搜索框测试", {"搜索": (1, [0]), "索": (1, [1])}
        )
        self.assertIn("搜索框", snippet)
        merged = ranges
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], (0, 2))

    def test_empty_content(self):
        self.assertEqual(_build_snippet("", {"a": (1, [])}), ("", []))


if __name__ == "__main__":
    unittest.main()
