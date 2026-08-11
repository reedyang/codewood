"""Test that quote normalization allows AI-generated patches with
straight quotes to match files containing typographic/curly quotes."""
import unittest

from cli.tools.apply_patch import (
    _collapse_quote_escapes,
    _lines_match,
    _matches_at,
    _locate_hunk_start,
    _normalize_for_match,
)


class QuoteNormalizationTests(unittest.TestCase):
    def test_normalize_curly_double_quotes(self):
        self.assertEqual(
            _normalize_for_match('\u201ctest\u201d'),
            '"test"',
        )

    def test_normalize_curly_single_quotes(self):
        self.assertEqual(
            _normalize_for_match('\u2018test\u2019'),
            "'test'",
        )

    def test_normalize_low9_double_quotes(self):
        self.assertEqual(
            _normalize_for_match('\u201etest\u201f'),
            '"test"',
        )

    def test_normalize_guillemets(self):
        self.assertEqual(
            _normalize_for_match('\u00abtest\u00bb'),
            '"test"',
        )

    def test_normalize_angle_single_quotes(self):
        self.assertEqual(
            _normalize_for_match('\u2039test\u203a'),
            "'test'",
        )

    def test_normalize_straight_quotes_no_change(self):
        self.assertEqual(
            _normalize_for_match("'straight' and \"double\""),
            "'straight' and \"double\"",
        )

    def test_matches_at_curly_in_file_straight_in_patch(self):
        # File has curly single quotes (U+2018 / U+2019)
        file_lines = [
            "def main():",
            '    print("start \u2018curly content\u2019 end")',
            '    print("second line")',
        ]
        # Patch has straight single quotes (U+0027)
        hunk_lines = [
            " def main():",
            "+    print(\"new line\")",
            "     print(\"start 'curly content' end\")",
            "     print(\"second line\")",
        ]
        self.assertTrue(_matches_at(file_lines, 0, hunk_lines))
        self.assertEqual(_locate_hunk_start(file_lines, 0, 0, hunk_lines, fuzz=2), 0)

    def test_matches_at_curly_in_patch_straight_in_file(self):
        # File has straight single quotes
        file_lines = [
            "def main():",
            "    print(\"start 'straight content' end\")",
            "    print(\"second line\")",
        ]
        # Patch has curly single quotes
        hunk_lines = [
            " def main():",
            "+    print(\"new line\")",
            '     print("start \u2018straight content\u2019 end")',
            "     print(\"second line\")",
        ]
        self.assertTrue(_matches_at(file_lines, 0, hunk_lines))
        self.assertEqual(_locate_hunk_start(file_lines, 0, 0, hunk_lines, fuzz=2), 0)

    def test_matches_at_curly_double_quotes_both_sides(self):
        # File has curly double quotes
        file_lines = [
            "print(\u201chello\u201d)",
        ]
        # Patch has straight double quotes
        hunk_lines = [
            "-print(\"hello\")",
            "+print(\"world\")",
        ]
        self.assertTrue(_matches_at(file_lines, 0, hunk_lines))
        self.assertEqual(_locate_hunk_start(file_lines, 0, 0, hunk_lines, fuzz=2), 0)

    def test_matches_at_straight_quotes_still_works(self):
        file_lines = [
            "def main():",
            '    print("hello")',
        ]
        hunk_lines = [
            " def main():",
            "+    print(\"world\")",
            '-    print("hello")',
        ]
        self.assertTrue(_matches_at(file_lines, 0, hunk_lines))

    def test_no_false_positive_different_content(self):
        """Different content should still fail even after normalization."""
        file_lines = [
            "def main():",
            '    print("completely different text")',
        ]
        hunk_lines = [
            " def main():",
            '     print("totally other text")',
        ]
        self.assertFalse(_matches_at(file_lines, 0, hunk_lines))

    def test_locate_hunk_start_wrong_target_with_curly_quotes(self):
        """Anchor has straight quotes but file has curly quotes, and
        the @@ line number is off.  This tests the anchor-search path in
        _locate_hunk_start where old_lines[probe] == anchor was broken
        before quote normalization was added to that comparison."""
        # File after first edit — curly quotes on line at index 2
        file_lines = [
            "def main():",
            '    print("newly added line")',
            '    print("Alice \u2018was beginning\u2019 test")',
            '    print("second line")',
            '    print("third line")',
        ]
        # Patch says start at line 2 (wrong — target_idx=1), anchor is at index 2
        # Anchor has straight quotes
        hunk_lines = [
            "     print(\"Alice 'was beginning' test\")",
            "     print(\"second line\")",
            "+    print(\"appended line\")",
        ]
        result = _locate_hunk_start(file_lines, 0, 1, hunk_lines, fuzz=2)
        self.assertEqual(result, 2)

    def test_lines_match_tolerates_escaped_quotes_in_patch(self):
        """A patch line copied from JSON-escaped tool output (\\" for ") must
        still match a file line that has the plain quote.  This reproduces the
        ChatTitleBar rename-chat regression where apply_patch failed 6 times
        on ``return `\\"${...}"\\`;`` while the file had plain quotes."""
        file_line = '  return `"${value.replace(/"/g, "")}"`;'
        patch_line = '-  return `\\"${value.replace(/\\\\"/g, "")}"`;'
        self.assertNotEqual(file_line, patch_line[1:])
        self.assertTrue(_lines_match(file_line, patch_line[1:]))

    def test_lines_match_does_not_drop_real_backslashes_in_file(self):
        """The file side is never collapsed: a file line that genuinely
        contains a backslash-quote must not match a patch line that dropped
        the backslash."""
        file_line = r'  return `\"${value}"`;'
        patch_line = '  return `"${value}"`;'
        self.assertFalse(_lines_match(file_line, patch_line))

    def test_collapse_quote_escapes_handles_runs(self):
        self.assertEqual(_collapse_quote_escapes('a\\"b'), 'a"b')
        self.assertEqual(_collapse_quote_escapes('a\\\\"b'), 'a"b')
        self.assertEqual(_collapse_quote_escapes("a\\'b"), "a'b")
        self.assertEqual(_collapse_quote_escapes("a\\`b"), "a`b")
        # A backslash not before a quote is left alone.
        self.assertEqual(_collapse_quote_escapes("a\\nb"), "a\\nb")

    def test_locate_hunk_start_tolerates_escaped_quotes(self):
        """The full hunk-locating path (used before every apply) accepts a
        patch whose deletion lines carry backslash-escaped quotes."""
        file_lines = [
            'import { buildChatMenuItems, chatKey } from "./chatMenu";',
            "",
            "function quote(value: string): string {",
            '  return `"${value.replace(/"/g, "")}"`;',
            "}",
            "",
            "interface MenuState {",
            "  x: number;",
            "  y: number;",
        ]
        hunk_lines = [
            ' import { buildChatMenuItems, chatKey } from "./chatMenu";',
            " ",
            "-function quote(value: string): string {",
            '-  return `\\"${value.replace(/\\\\"/g, "")}"`;',
            "-}",
            "-",
            " interface MenuState {",
            "   x: number;",
            "   y: number;",
        ]
        result = _locate_hunk_start(file_lines, 0, 4, hunk_lines, fuzz=2)
        self.assertIsNotNone(result)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
