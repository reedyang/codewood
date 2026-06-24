"""Tests for the GUI session-only display-language override.

The desktop GUI backend (serve mode) should render backend-produced text in the
GUI's locale via a runtime override, WITHOUT persisting it as the TUI's
``display_language``. The override takes precedence over the agent's own
language and is never written to disk.
"""

import unittest

from cli.config.i18n import get_display_language


class _Agent:
    def __init__(self, display_language=None, override=None):
        self.display_language = display_language
        if override is not None:
            self._gui_language_override = override


class GuiLanguageOverrideTests(unittest.TestCase):
    def test_override_takes_precedence_over_display_language(self):
        agent = _Agent(display_language="en", override="zh-CN")
        self.assertEqual(get_display_language(agent), "zh-CN")

    def test_gui_code_zh_hans_maps_to_backend_zh_cn(self):
        # The GUI persists Simplified Chinese as "zh-Hans"; the backend override
        # must map it to its canonical "zh-CN" locale.
        agent = _Agent(display_language="en", override="zh-Hans")
        self.assertEqual(get_display_language(agent), "zh-CN")

    def test_without_override_falls_back_to_display_language(self):
        agent = _Agent(display_language="zh-CN")
        self.assertEqual(get_display_language(agent), "zh-CN")

    def test_blank_override_is_ignored(self):
        agent = _Agent(display_language="en", override="")
        self.assertEqual(get_display_language(agent), "en")

    def test_invalid_override_is_ignored(self):
        agent = _Agent(display_language="en", override="not-a-lang")
        self.assertEqual(get_display_language(agent), "en")


if __name__ == "__main__":
    unittest.main()
