import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli.tools.apply_patch import _locate_hunk_start, action_apply_unified_patch


class _DummyPolicy:
    def can_write_path(self, _path: Path, _action: str) -> Dict[str, Any]:
        return {"allowed": True}


class _DummyAgent:
    def __init__(self, work_directory: Path) -> None:
        self.work_directory = work_directory
        self.workspace_root = work_directory
        self.workspace_config_dir = work_directory
        self.execution_policy = "confirmation"
        self._ai_created_path_keys = set()
        self._freedom_script_review_entries = {}
        self.prompt_calls = 0

    def _get_path_policy(self) -> _DummyPolicy:
        return _DummyPolicy()

    def _resolve_user_path(self, user_path: str) -> Path:
        p = Path(user_path)
        if not p.is_absolute():
            p = self.work_directory / p
        return p.resolve()

    def _is_path_under(self, _path: Path, _root: Path) -> bool:
        try:
            Path(_path).resolve().relative_to(Path(_root).resolve())
            return True
        except Exception:
            return False

    def _format_side_by_side_change_preview_segments(
        self,
        segments: List[Dict[str, Any]],
        file_path: Any = None,
    ) -> List[str]:
        return []

    def _prompt_confirm_yes_no_maybe_always(
        self, _message: str, offer_always: bool = False, kind: str = "", **_kwargs: object
    ) -> bool:
        self.prompt_calls += 1
        return True

    def _ephemeral_path_key(self, resolved: Path) -> str:
        return str(resolved.resolve())

    def _reload_skills_if_workspace_skill_changed(self, _paths: List[Path]) -> None:
        return None


_ORIGINAL = """import sys


def helper():
    return 1


def run_subagent(
    agent: Any,
    topic: str = "",
    cancel_check=None,
    suppress_session_marker=False,
) -> Dict:
    \"\"\"Execute a sub-agent.
    ``cancel_check`` lets a caller abort the sub-agent at its next round
    boundary. ``suppress_session_marker`` hides the GUI marker — used when
    the sub-agent runs on a background thread whose stdout must not leak.
    \"\"\"
    session = create_session(
        agent=agent,
        topic=topic,
    )
    session_id = session["id"]

    # Emit session start event
    emit(session_id)
"""

# Every @@ line number below is deliberately wrong (too low) but internally
# consistent, mimicking a patch generated against a stale file view: real
# content sits ~7 lines below the declared positions, so after hunk 1 is
# fuzz-located at its actual spot, hunk 2's declared position (6) falls
# behind the scanner position.  Regression for the misleading
# "Hunk start line 6 is outside file (file has 26 lines)" error.
_STALE_PATCH = """--- a/target.py
+++ b/target.py
@@ -3,4 +3,5 @@
     topic: str = "",
     cancel_check=None,
     suppress_session_marker=False,
+    on_session_created=None,
 ) -> Dict:
@@ -6,3 +7,4 @@
     ``cancel_check`` lets a caller abort the sub-agent at its next round
     boundary. ``suppress_session_marker`` hides the GUI marker — used when
-    the sub-agent runs on a background thread whose stdout must not leak.
+    the sub-agent runs on a background thread whose stdout must not leak.
+    ``on_session_created`` is invoked with the session id.
@@ -10,2 +11,4 @@
     )
     session_id = session["id"]
+    if on_session_created is not None:
+        on_session_created(session_id)
"""

_EXPECTED = """import sys


def helper():
    return 1


def run_subagent(
    agent: Any,
    topic: str = "",
    cancel_check=None,
    suppress_session_marker=False,
    on_session_created=None,
) -> Dict:
    \"\"\"Execute a sub-agent.
    ``cancel_check`` lets a caller abort the sub-agent at its next round
    boundary. ``suppress_session_marker`` hides the GUI marker — used when
    the sub-agent runs on a background thread whose stdout must not leak.
    ``on_session_created`` is invoked with the session id.
    \"\"\"
    session = create_session(
        agent=agent,
        topic=topic,
    )
    session_id = session["id"]
    if on_session_created is not None:
        on_session_created(session_id)

    # Emit session start event
    emit(session_id)
"""


class ApplyPatchStaleLineNumbersTests(unittest.TestCase):
    def test_stale_hunk_line_numbers_apply_successfully(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.py"
            target.write_text(_ORIGINAL, encoding="utf-8")
            agent = _DummyAgent(root)

            result = action_apply_unified_patch(
                agent, str(target), _STALE_PATCH, confirmed=False
            )

            self.assertTrue(result.get("success"), result.get("error"))
            self.assertNotIn("outside file", str(result.get("error") or ""))
            self.assertEqual(target.read_text(encoding="utf-8"), _EXPECTED)

    def test_hunk_start_past_eof_still_reports_outside_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "tiny.py"
            target.write_text("a\nb\nc\n", encoding="utf-8")
            agent = _DummyAgent(root)

            patch = "--- a/tiny.py\n+++ b/tiny.py\n@@ -10,2 +10,2 @@\n a\n b\n"
            result = action_apply_unified_patch(
                agent, str(target), patch, confirmed=False
            )

            self.assertFalse(result.get("success"))
            error = str(result.get("error") or "")
            self.assertIn("Hunk start line 10 is outside file", error)
            self.assertIn("file has 3 lines", error)

    def test_locate_hunk_start_skips_exact_match_behind_src_idx(self):
        old_lines = [
            "def run_subagent(",
            '    topic: str = "",',
            ") -> Dict:",
            "    ...",
            "def run_subagent2(",
            '    topic: str = "",',
            ") -> Dict:",
        ]
        hunk_lines = [
            '     topic: str = "",',
            "+    on_session_created=None,",
            " ) -> Dict:",
        ]

        # target_idx=1 lies behind src_idx=4; the exact-match fast path must
        # NOT return the already-consumed position even though it matches.
        result = _locate_hunk_start(old_lines, 4, 1, hunk_lines, fuzz=0)
        self.assertEqual(result, 5)


if __name__ == "__main__":
    unittest.main()
