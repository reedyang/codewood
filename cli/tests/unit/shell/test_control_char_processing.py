import re
import unittest

from cli.tools.shell import (
    _PTY_CSI_STRIP_RE,
    _collapse_cr_output,
    _handle_backspace_collapse,
)


class HandleBackspaceCollapseTests(unittest.TestCase):
    def test_no_backspace_unchanged(self):
        text = "hello world"
        self.assertEqual(_handle_backspace_collapse(text), text)

    def test_single_backspace_overwrites(self):
        text = "ab\bc"
        self.assertEqual(_handle_backspace_collapse(text), "ac")

    def test_multiple_backspaces(self):
        text = "hello\b\b\b___"
        self.assertEqual(_handle_backspace_collapse(text), "he___")

    def test_countdown_digits_with_backspace(self):
        # Trailing countdown: "...\b4\b3\b2\b1\b0"
        text = "...\b4\b3\b2\b1\b0"
        self.assertEqual(_handle_backspace_collapse(text), "..0")

    def test_countdown_overwrites_digit_at_position(self):
        # Simulates CUP positioning (\b*38 goes back to col 13 = "5" in "5 seconds")
        line = "Waiting for  5 seconds, press a key to continue ..."
        # backspaces from end (len=51, col=51) back to "5" at index 13: 51-13=38
        bs_count = len(line) - line.index("5")
        text = line + ("\b" * bs_count) + "4\b3\b2\b1\b0"
        expected = "Waiting for  0 seconds, press a key to continue ..."
        self.assertEqual(_handle_backspace_collapse(text), expected)

    def test_backspace_at_start_no_effect(self):
        text = "\b\bhello"
        self.assertEqual(_handle_backspace_collapse(text), "hello")

    def test_backspace_between_non_overlapping(self):
        text = "abc\bdef"
        # \b goes back over "c", "d" overwrites "c" → "abdef"
        self.assertEqual(_handle_backspace_collapse(text), "abdef")


class CollapseCROutputTests(unittest.TestCase):
    def test_no_cr_no_bs_unchanged(self):
        text = "plain text\nsecond line"
        self.assertEqual(_collapse_cr_output(text), text)

    def test_crlf_preserved_as_lf(self):
        text = "line1\r\nline2"
        self.assertEqual(_collapse_cr_output(text), "line1\nline2")

    def test_cr_keeps_last_segment(self):
        text = "spinner A\rspinner B\rspinner C"
        self.assertEqual(_collapse_cr_output(text), "spinner C")

    def test_completed_spinner_cleared(self):
        # Multi-frame spinner on a completed line (followed by \n) → cleared
        text = "spinner A\rspinner B\rspinner C\nnext line"
        self.assertEqual(_collapse_cr_output(text), "\nnext line")

    def test_completed_spinner_single_frame_after_cr_cleared(self):
        # "\rspinner\n" — single frame after \r on completed line → cleared
        text = "\rspinner\nnext line"
        self.assertEqual(_collapse_cr_output(text), "\nnext line")

    def test_continuing_line_keeps_last_frame(self):
        # Last line (no trailing \n) with multiple \r frames → keep last
        text = "done\nspinner A\rspinner B\rspinner C"
        self.assertEqual(_collapse_cr_output(text), "done\nspinner C")

    def test_timeout_countdown_captured_output(self):
        # Simulates captured output with CUP \b* positioning + trailing \b digits
        line = "Waiting for  5 seconds, press a key to continue ..."
        bs_count = len(line) - line.index("5")
        text = "D:\\tmp>timeout 5\n\n" + line + ("\b" * bs_count) + "4\b3\b2\b1\b0\n"
        result = _collapse_cr_output(text)
        expected = "D:\\tmp>timeout 5\n\nWaiting for  0 seconds, press a key to continue ...\n"
        self.assertEqual(result, expected)

    def test_many_cr_frames_on_completed_line_cleared(self):
        # 3+ \r frames on a completed line → cleared (true spinner)
        text = "hello\r\nframeA\rframeB\rframeC\n"
        result = _collapse_cr_output(text)
        self.assertEqual(result, "hello\n\n")

    def test_two_cr_frames_on_completed_line_kept(self):
        # Only 2 \r frames → simple overwrite, keep last
        text = "hello\nworld\bx\r\by\n"
        result = _collapse_cr_output(text)
        self.assertEqual(result, "hello\ny\n")

    def test_mixed_cr_and_bs_on_continuing_line_kept(self):
        # Last line (no trailing \n) keeps last segment; \b applied to it
        text = "hello\nworld\bx\r\by"
        result = _collapse_cr_output(text)
        self.assertEqual(result, "hello\ny")

    def test_cr_overwrite_on_completed_line_kept(self):
        # Simple \r overwrite (2 frames) on completed line → not a spinner, keep last
        text = "abc\rdef\ngh\bi"
        result = _collapse_cr_output(text)
        self.assertEqual(result, "def\ngi")


class PTYCSIStripRETests(unittest.TestCase):
    def test_cha_g_passes_through(self):
        # CHA ending with 'G' (col 56) must NOT be stripped
        seq = "\x1b[56G"
        self.assertIsNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_cup_h_passes_through(self):
        # CUP ending with 'H' must NOT be stripped
        seq = "\x1b[1;57H"
        self.assertIsNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_el_k_passes_through(self):
        # EL ending with 'K' must NOT be stripped
        seq = "\x1b[K"
        self.assertIsNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_cuu_a_passes_through(self):
        seq = "\x1b[A"
        self.assertIsNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_sgr_m_passes_through(self):
        # SGR ending with 'm' must NOT be stripped
        seq = "\x1b[32m"
        self.assertIsNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_private_modes_stripped(self):
        # Private modes (with ? prefix) should be stripped
        for seq in ("\x1b[?25h", "\x1b[?25l", "\x1b[?1049h"):
            self.assertIsNotNone(_PTY_CSI_STRIP_RE.match(seq),
                                 f"{repr(seq)} should be matched")

    def test_da_response_stripped(self):
        # DA response ending with 'c' should be stripped
        seq = "\x1b[?62c"
        self.assertIsNotNone(_PTY_CSI_STRIP_RE.match(seq))

    def test_window_op_t_stripped(self):
        # Window op ending with 't' should be stripped
        seq = "\x1b[8;40;120t"
        self.assertIsNotNone(_PTY_CSI_STRIP_RE.match(seq))


if __name__ == "__main__":
    unittest.main()
