import unittest

from src.core import status_bar as sb


class StatusBarTokenUsageTests(unittest.TestCase):
    def test_status_bar_includes_chat_usage_percent(self):
        frags, plain = sb.build_status_bar_render_data(
            "gpt-4o-mini",
            "Default",
            "Demo Chat",
            37,
        )
        self.assertIn("(37%)", plain)
        self.assertEqual(frags[-1][0], "fg:ansibrightblack")
        self.assertEqual(frags[-1][1], "(37%)")

    def test_status_usage_percent_is_clamped(self):
        _frags, plain = sb.build_status_bar_render_data(
            "gpt-4o-mini",
            "Default",
            "Demo Chat",
            12345,
        )
        self.assertIn("(999%)", plain)

    def test_refresh_without_service_keeps_cached_usage(self):
        class FakeService:
            def __init__(self):
                self.calls = 0

            def refresh_context_usage_snapshot(self, user_input_hint: str = "", context_hint: str = "") -> None:
                self.calls += 1

        svc = FakeService()
        sb.refresh_status_context_usage_snapshot(svc, user_input_hint="hello", context_hint="ctx")
        self.assertEqual(svc.calls, 1)

    def test_refresh_without_service_is_safe(self):
        sb.refresh_status_context_usage_snapshot(None, user_input_hint="hello", context_hint="ctx")


if __name__ == "__main__":
    unittest.main()
