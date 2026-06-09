"""Tests for the no-match-friendly exit-code classifier.

Search tools (``rg``, ``grep``, ``findstr`` and friends) document
exit code 1 as "ran successfully but found no matches". Treating
that as a hard failure (red bullet, ``success: False`` to the model)
makes the model conclude the tool itself is broken and switch to a
worse alternative — which is exactly what the user observed in chat
``4726f9e8d90d5fefb888697d6ff58c8a.json``: the model fell back from
``rg`` to ``grep`` after a benign "no matches" outcome.

These tests pin the relaxed classification to safe, single-tool
invocations only. Pipelines, boolean chains, and other compound
forms are explicitly excluded because their final exit code does
not reflect the head tool's behavior.
"""

from __future__ import annotations

import unittest

from src.actions.command_actions import (
    _classify_no_match_exit,
    _shell_command_has_compound_operator,
)


class NoMatchExitClassificationTests(unittest.TestCase):
    def _classifies(self, command: str, return_code: int = 1, stdout: str = "") -> bool:
        return _classify_no_match_exit(command, return_code, stdout) is not None

    # --- positive cases -----------------------------------------------

    def test_rg_with_no_matches_is_classified_as_success(self):
        self.assertTrue(self._classifies('rg -n "pseudo tool call" -C 2'))

    def test_grep_with_no_matches_is_classified_as_success(self):
        self.assertTrue(self._classifies("grep -R foo src"))

    def test_egrep_and_fgrep_are_recognized(self):
        self.assertTrue(self._classifies("egrep foo src"))
        self.assertTrue(self._classifies("fgrep foo src"))

    def test_findstr_is_recognized_on_windows_style_command(self):
        self.assertTrue(self._classifies('findstr /S "foo" *.txt'))

    def test_silver_searcher_and_ack_are_recognized(self):
        self.assertTrue(self._classifies("ag foo src"))
        self.assertTrue(self._classifies("ack foo src"))

    def test_inner_command_is_unwrapped_through_cmd_c(self):
        self.assertTrue(self._classifies("cmd /c rg foo"))

    def test_inner_command_is_unwrapped_through_powershell_command(self):
        self.assertTrue(
            self._classifies(
                'powershell -ExecutionPolicy Bypass -Command "rg foo"'
            )
        )

    def test_inner_command_is_unwrapped_through_bash_dash_c(self):
        self.assertTrue(self._classifies('bash -c "rg foo"'))

    def test_classifier_returns_explanatory_message(self):
        msg = _classify_no_match_exit("rg foo", 1, "")
        self.assertIsNotNone(msg)
        assert msg is not None  # for type-checker
        self.assertIn("rg", msg)
        self.assertIn("no matches", msg.lower())

    def test_absolute_path_to_rg_executable_is_recognized(self):
        # The runtime rewrites bare ``rg`` to the bundled absolute
        # path before execution; the classifier must still recognize
        # that path's basename.
        self.assertTrue(
            self._classifies(r'"D:\SourceCode\opensource\smart-shell\bin\rg.exe" -n foo .')
        )

    # --- negative cases -----------------------------------------------

    def test_zero_exit_code_is_not_reclassified(self):
        self.assertFalse(self._classifies("rg foo", return_code=0))

    def test_exit_code_two_is_not_reclassified(self):
        # Real rg/grep error (e.g. invalid regex, IO error) — must
        # remain a failure so the model can react.
        self.assertFalse(self._classifies("rg foo", return_code=2))

    def test_stdout_with_content_is_not_reclassified(self):
        # If there's already output, "no matches" is implausible.
        self.assertFalse(self._classifies("rg foo", 1, "match line\n"))

    def test_pipe_compound_command_is_excluded(self):
        # The pipe's exit code reflects the LAST tool, not rg.
        self.assertFalse(self._classifies("rg foo | head"))

    def test_logical_and_compound_command_is_excluded(self):
        self.assertFalse(self._classifies("rg foo && echo done"))

    def test_logical_or_compound_command_is_excluded(self):
        self.assertFalse(self._classifies("rg foo || true"))

    def test_sequencer_compound_command_is_excluded(self):
        self.assertFalse(self._classifies("rg foo; ls"))

    def test_background_operator_is_excluded(self):
        self.assertFalse(self._classifies("rg foo & ls"))

    def test_non_search_tool_is_not_reclassified(self):
        # ``python script.py`` returning 1 is a real script error,
        # never "no matches".
        self.assertFalse(self._classifies("python script.py"))
        self.assertFalse(self._classifies("ls notexist"))
        self.assertFalse(self._classifies("git status"))

    def test_empty_command_is_not_reclassified(self):
        self.assertFalse(self._classifies(""))


class CompoundOperatorDetectorTests(unittest.TestCase):
    def test_plain_command_has_no_compound_operator(self):
        self.assertFalse(_shell_command_has_compound_operator("rg foo"))
        self.assertFalse(_shell_command_has_compound_operator('rg "foo bar" .'))

    def test_pipe_is_detected(self):
        self.assertTrue(_shell_command_has_compound_operator("rg foo | head"))

    def test_logical_operators_are_detected(self):
        self.assertTrue(_shell_command_has_compound_operator("rg foo && ls"))
        self.assertTrue(_shell_command_has_compound_operator("rg foo || true"))

    def test_sequencer_and_background_are_detected(self):
        self.assertTrue(_shell_command_has_compound_operator("rg foo; ls"))
        self.assertTrue(_shell_command_has_compound_operator("rg foo & ls"))

    def test_double_quoted_operators_are_treated_as_literals(self):
        self.assertFalse(_shell_command_has_compound_operator('rg "a|b" .'))
        self.assertFalse(_shell_command_has_compound_operator('grep "a;b" .'))
        self.assertFalse(_shell_command_has_compound_operator('rg "x&y" .'))

    def test_single_quoted_operators_are_treated_as_literals(self):
        self.assertFalse(_shell_command_has_compound_operator("rg 'a|b' ."))
        self.assertFalse(_shell_command_has_compound_operator("grep 'a;b' ."))


if __name__ == "__main__":
    unittest.main()
