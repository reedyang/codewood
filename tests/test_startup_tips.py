import json
import tempfile
import unittest
from pathlib import Path

from src.config.startup_tips import (
    DEFAULT_STARTUP_TIP,
    DEFAULT_STARTUP_TIP_ENTRY,
    format_tip_with_highlights,
    get_random_startup_tip_entry,
    get_random_startup_tip,
    load_startup_tip_entries,
    load_startup_tips,
)


class StartupTipsTests(unittest.TestCase):
    def test_load_startup_tip_entries_from_config_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "startup_tips.json"
            payload = {
                "tips": [
                    {"text": "Tip A", "highlights": ["/a"]},
                    {"text": "Tip B", "highlights": ["/b"]},
                    {"text": "Tip C", "highlights": ["/c"]},
                ]
            }
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tips = load_startup_tip_entries(path=p)
            self.assertEqual(
                tips,
                [
                    {"text": "Tip A", "highlights": ["/a"]},
                    {"text": "Tip B", "highlights": ["/b"]},
                    {"text": "Tip C", "highlights": ["/c"]},
                ],
            )
            self.assertEqual(load_startup_tips(path=p), ["Tip A", "Tip B", "Tip C"])

    def test_get_random_startup_tip_returns_member(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "startup_tips.json"
            payload = {
                "tips": [
                    {"text": "Alpha", "highlights": ["/alpha"]},
                    {"text": "Beta", "highlights": ["/beta"]},
                ]
            }
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            picked = get_random_startup_tip(path=p)
            self.assertIn(picked, ["Alpha", "Beta"])
            picked_entry = get_random_startup_tip_entry(path=p)
            self.assertIn(picked_entry, payload["tips"])

    def test_missing_file_falls_back_to_default_tip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.json"
            tips = load_startup_tips(path=p)
            self.assertEqual(tips, [DEFAULT_STARTUP_TIP])
            self.assertEqual(get_random_startup_tip(path=p), DEFAULT_STARTUP_TIP)
            self.assertEqual(
                load_startup_tip_entries(path=p),
                [DEFAULT_STARTUP_TIP_ENTRY],
            )

    def test_format_tip_with_highlights_marks_substrings(self):
        text = "Use /help then /model to continue."
        out = format_tip_with_highlights(
            text=text,
            highlights=["/help", "/model"],
            highlight_formatter=lambda s: f"<{s}>",
        )
        self.assertEqual(out, "Use </help> then </model> to continue.")


if __name__ == "__main__":
    unittest.main()
