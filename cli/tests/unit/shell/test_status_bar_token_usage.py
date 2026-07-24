import unittest

from cli.core import status_bar as sb


class StatusBarTokenUsageTests(unittest.TestCase):
    def test_status_bar_includes_chat_usage_percent(self):
        frags, plain, usage = sb.build_status_bar_render_data(
            "gpt-4o-mini",
            "Default",
            "Demo Chat",
            37,
        )
        self.assertEqual(usage, "(37%)")
        self.assertNotIn("(37%)", plain)

    def test_status_usage_percent_is_clamped(self):
        _frags, plain, usage = sb.build_status_bar_render_data(
            "gpt-4o-mini",
            "Default",
            "Demo Chat",
            12345,
        )
        self.assertEqual(usage, "(999%)")
        self.assertNotIn("(999%)", plain)

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
