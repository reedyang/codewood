import unittest

from cli.server.console_manager import ConsoleManager, ConsoleSession


class RingBufferTests(unittest.TestCase):
    def _session(self, buffer_lines: int = 5) -> ConsoleSession:
        # Build a session without starting a PTY; exercise the buffer directly.
        return ConsoleSession("s1", "cmd", "Command Prompt 1", "/tmp", buffer_lines)

    def test_ingest_splits_lines_and_keeps_pending(self):
        s = self._session()
        s._ingest(b"line one\nline two\npartial")
        result = s.read_lines(0, 10)
        self.assertEqual(result["lines"], ["line one", "line two"])
        self.assertEqual(result["totalLines"], 2)
        self.assertEqual(result["pending"], "partial")

    def test_strips_carriage_returns(self):
        s = self._session()
        s._ingest(b"a\r\nb\r\n")
        self.assertEqual(s.read_lines(0, 10)["lines"], ["a", "b"])

    def test_total_lines_counts_truncated(self):
        s = self._session(buffer_lines=3)
        for i in range(6):
            s._ingest(f"l{i}\n".encode())
        result = s.read_lines(0, 100)
        # Only the last 3 lines are retained but total reflects all 6.
        self.assertEqual(result["totalLines"], 6)
        self.assertEqual(result["lines"], ["l3", "l4", "l5"])
        self.assertEqual(result["retainedFrom"], 3)
        self.assertTrue(result["truncated"])

    def test_read_absolute_range(self):
        s = self._session(buffer_lines=100)
        for i in range(10):
            s._ingest(f"l{i}\n".encode())
        result = s.read_lines(2, 3)
        self.assertEqual(result["lines"], ["l2", "l3", "l4"])
        self.assertEqual(result["start"], 2)

    def test_read_beyond_end_returns_empty(self):
        s = self._session()
        s._ingest(b"only\n")
        self.assertEqual(s.read_lines(50, 10)["lines"], [])

    def test_subscriber_receives_chunks(self):
        s = self._session()
        seen = []
        s.add_subscriber(seen.append)
        s._ingest(b"hello\n")
        self.assertEqual(seen, [b"hello\n"])
        s.remove_subscriber(seen.append)
        s._ingest(b"world\n")
        self.assertEqual(seen, [b"hello\n"])


class ManagerTests(unittest.TestCase):
    def test_open_rejects_unknown_kind(self):
        mgr = ConsoleManager(cwd_getter=lambda: "/tmp")
        result = mgr.open("zsh-evil")
        self.assertFalse(result.get("success"))

    def test_list_and_active_tracking(self):
        # Inject sessions directly to avoid spawning real PTYs in CI.
        mgr = ConsoleManager(cwd_getter=lambda: "/tmp")
        s1 = ConsoleSession("a", "cmd", "Command Prompt 1", "/tmp", 100)
        s2 = ConsoleSession("b", "cmd", "Command Prompt 2", "/tmp", 100)
        mgr._sessions = {"a": s1, "b": s2}
        mgr._order = ["a", "b"]
        mgr._active_id = "b"
        listed = mgr.list()
        self.assertEqual([x["id"] for x in listed], ["a", "b"])
        self.assertTrue(listed[1]["active"])
        self.assertFalse(listed[0]["active"])
        self.assertTrue(mgr.set_active("a"))
        self.assertEqual(mgr.active().id, "a")
        self.assertFalse(mgr.set_active("missing"))

    def test_close_updates_active_fallback(self):
        mgr = ConsoleManager(cwd_getter=lambda: "/tmp")
        s1 = ConsoleSession("a", "cmd", "t1", "/tmp", 100)
        s2 = ConsoleSession("b", "cmd", "t2", "/tmp", 100)
        mgr._sessions = {"a": s1, "b": s2}
        mgr._order = ["a", "b"]
        mgr._active_id = "b"
        self.assertTrue(mgr.close("b"))
        self.assertEqual(mgr._active_id, "a")
        self.assertFalse(mgr.close("b"))


if __name__ == "__main__":
    unittest.main()
