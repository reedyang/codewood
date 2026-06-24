import unittest

from cli.core.config.gui_config import (
    default_console_options,
    normalize_console_options,
)


class ConsoleOptionsTests(unittest.TestCase):
    def test_defaults(self):
        opts = default_console_options()
        self.assertEqual(opts["fontFamily"], "")
        self.assertEqual(opts["bufferLines"], 1000)

    def test_normalize_none(self):
        opts = normalize_console_options(None)
        self.assertEqual(opts, {"fontFamily": "", "bufferLines": 1000})

    def test_clamps_buffer_lines(self):
        self.assertEqual(normalize_console_options({"bufferLines": 5})["bufferLines"], 100)
        self.assertEqual(
            normalize_console_options({"bufferLines": 9999999})["bufferLines"], 100000
        )

    def test_bounds_font_length(self):
        long_font = "x" * 500
        self.assertEqual(len(normalize_console_options({"fontFamily": long_font})["fontFamily"]), 128)

    def test_trims_font(self):
        self.assertEqual(
            normalize_console_options({"fontFamily": "  Consolas  "})["fontFamily"],
            "Consolas",
        )

    def test_invalid_buffer_falls_back(self):
        self.assertEqual(
            normalize_console_options({"bufferLines": "abc"})["bufferLines"], 1000
        )


if __name__ == "__main__":
    unittest.main()
