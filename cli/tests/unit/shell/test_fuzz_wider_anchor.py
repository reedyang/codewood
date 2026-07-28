import unittest

from cli.tools.apply_patch import _locate_hunk_start


class LocateHunkStartWiderAnchorTests(unittest.TestCase):
    def test_exact_match_at_target(self):
        old_lines = ["line a", "line b", "line c"]
        hunk_lines = [" line b", "-line c", "+line B"]
        result = _locate_hunk_start(old_lines, 0, 1, hunk_lines, fuzz=2)
        self.assertEqual(result, 1)

    def test_anchor_with_exact_context(self):
        old_lines = ["x", "a", "b", "c", "y"]
        hunk_lines = [" a", "-b", "+B"]
        result = _locate_hunk_start(old_lines, 0, 1, hunk_lines, fuzz=2)
        self.assertEqual(result, 1)

    def test_anchor_not_found(self):
        old_lines = ["x", "y", "z"]
        hunk_lines = [" missing", "-a", "+b"]
        result = _locate_hunk_start(old_lines, 0, 0, hunk_lines, fuzz=2)
        self.assertIsNone(result)

    def test_context_offset_exceeds_fuzz(self):
        """Fake anchor at index 2, real context at index 8 (delta=6 > fuzz=2).

        The wider search from the first (wrong) candidate bridges the gap and
        finds the correct position.  Without a wider window the function would
        still find it via the second candidate, but this test guarantees the
        broader search path is exercised and returns the correct result."""
        old_lines = [
            "",
            "class FakeTool:",
            '        "properties": {',
            '            "wrong_field": {"type": "string"},',
            "        },",
            "",
            "class RealTool:",
            "",
            '        "properties": {',
            '            "path": {"type": "string", "description": "..."},',
            '            "offset": {"type": "integer", "description": "..."},',
            '            "limit": {"type": "integer", "description": "..."},',
            '            "prompt": {"type": "string", "description": "..."},',
            "        },",
        ]
        hunk_lines = [
            '         "properties": {',
            '-            "path": {"type": "string", "description": "..."},',
            '-            "offset": {"type": "integer", "description": "..."},',
            '-            "limit": {"type": "integer", "description": "..."},',
            '             "prompt": {"type": "string", "description": "..."},',
        ]

        result = _locate_hunk_start(old_lines, 0, 2, hunk_lines, fuzz=2)
        self.assertEqual(result, 8)

    def test_wider_search_skips_non_anchor_positions(self):
        """The wider search must not return a position whose first line
        doesn't match the anchor — that would cause hunk application to
        fail later (exact line-by-line comparison)."""
        old_lines = [
            "class Foo:",
            '    "properties": {',
            "    }",
            "",
            "class Bar:",
            "",
            '    "properties": {',
            '    "path": {"type": "string"},',
            '    "offset": {"type": "integer"},',
            '    "limit": {"type": "integer"},',
            '    "prompt": {"type": "string"},',
        ]
        hunk_lines = [
            '     "properties": {',
            '-    "path": {"type": "string"},',
            '-    "offset": {"type": "integer"},',
            '-    "limit": {"type": "integer"},',
            '+    "newname": {"type": "string"},',
            '     "prompt": {"type": "string"},',
        ]

        result = _locate_hunk_start(old_lines, 0, 0, hunk_lines, fuzz=2)
        # The close fake anchor at index 1 doesn't match the full context.
        # Wider search should reach index 6 (real anchor, delta=5 > fuzz=2)
        # and return it — NOT return a non-anchor position like index 4
        # (which would happen without the old_lines[test_pos]==anchor guard).
        self.assertEqual(result, 6)


if __name__ == "__main__":
    unittest.main()
