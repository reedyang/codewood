import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.controllers.language_command_controller import handle_language_builtin_command


class _FakeLanguageAgent:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.display_language = "en"
        self._resolved_config_data = {}
        self.reload_calls = 0
        self.remember_first_visible_calls = []
        self.observed_stdout_during_reload = None
        self.recorded_slash_entries = []
        self.history_at_reload = None

    def _reload_chat_history_from_anchor_on_resize(self):
        self.reload_calls += 1
        # Capture the stdout that the controller routed the reload through so
        # tests can verify that any slash-output indentation wrapper was
        # unwrapped before the reload ran. Snapshot the recorded slash
        # history at this point too — the controller must have already
        # pre-recorded the ``/language ...`` line before triggering reload,
        # otherwise the redrawn transcript would be missing the user's
        # most-recent command.
        self.observed_stdout_during_reload = sys.stdout
        self.history_at_reload = list(self.recorded_slash_entries)

    def _remember_active_chat_history_first_visible_index(self, index):
        self.remember_first_visible_calls.append(index)

    def _record_internal_slash_execution_history(
        self, raw_user_command, output_text
    ):
        self.recorded_slash_entries.append(
            {
                "raw_user_command": str(raw_user_command or ""),
                "output_text": str(output_text or ""),
            }
        )


class LanguageCommandControllerTests(unittest.TestCase):
    def test_language_usage_prints_supported_languages(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeLanguageAgent(Path(td))
            buf = io.StringIO()
            with redirect_stdout(buf):
                handled = handle_language_builtin_command(agent, "language")
            self.assertTrue(handled)
            out = buf.getvalue()
            self.assertIn("Usage:", out)
            self.assertIn("/language <language code>", out)
            self.assertIn("Current language:", out)
            self.assertIn("en - English", out)
            self.assertIn("zh-CN - 简体中文", out)

    def test_language_change_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            (cfg_dir / "config.jsonc").write_text("{}", encoding="utf-8")
            agent = _FakeLanguageAgent(cfg_dir)
            handled = handle_language_builtin_command(agent, "language zh-CN")
            self.assertTrue(handled)
            self.assertEqual(agent.display_language, "zh-CN")
            self.assertEqual(agent.reload_calls, 1)
            with open(cfg_dir / "config.jsonc", "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("language"), "zh-CN")
            self.assertNotIn("display_language", data)

    def test_invalid_language_shows_usage(self):
        with tempfile.TemporaryDirectory() as td:
            agent = _FakeLanguageAgent(Path(td))
            buf = io.StringIO()
            with redirect_stdout(buf):
                handled = handle_language_builtin_command(agent, "language fr")
            self.assertTrue(handled)
            out = buf.getvalue()
            self.assertIn("Unsupported language", out)
            self.assertIn("/language <language code>", out)
            self.assertNotIn("/language list", out)

    def test_language_change_resets_first_visible_index(self):
        """Switching language must rewind the active chat to its first
        visible message, mirroring ``/chat reload`` so the user sees a fresh
        replay from the top rather than an arbitrary mid-history snapshot."""
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            (cfg_dir / "config.jsonc").write_text("{}", encoding="utf-8")
            agent = _FakeLanguageAgent(cfg_dir)
            handled = handle_language_builtin_command(agent, "language zh-CN")
            self.assertTrue(handled)
            self.assertEqual(agent.remember_first_visible_calls, [0])
            self.assertEqual(agent.reload_calls, 1)

    def test_language_change_prerecords_slash_history_before_reload(self):
        """The controller must record the ``/language ...`` slash entry into
        chat history BEFORE triggering the reload. Otherwise the redrawn
        chat history (which runs inside the same slash dispatch) would not
        include the user's just-issued command line."""
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            (cfg_dir / "config.jsonc").write_text("{}", encoding="utf-8")
            agent = _FakeLanguageAgent(cfg_dir)
            handled = handle_language_builtin_command(agent, "language zh-CN")
            self.assertTrue(handled)
            # The pre-record happened before reload and included the leading
            # ``/`` so the replayer can recognize the entry as a slash
            # command rather than a literal user message.
            self.assertEqual(len(agent.history_at_reload or []), 1)
            entry = agent.history_at_reload[0]
            self.assertEqual(entry["raw_user_command"], "/language zh-CN")
            self.assertIn("zh-CN", entry["output_text"] + entry["raw_user_command"])
            # The recorded confirmation must end with a newline. Without it
            # the slash-output renderer doesn't break the line and the next
            # prompt arrow gets glued to the success message — leaving the
            # input cursor in the wrong place after the reload.
            self.assertTrue(
                entry["output_text"].endswith("\n"),
                f"recorded confirmation must end with a newline, got {entry['output_text']!r}",
            )
            # The one-shot suppression flag must be set so the runtime loop's
            # ``finally``-block recorder doesn't append a duplicate entry
            # after the controller returns.
            self.assertTrue(
                getattr(agent, "_suppress_next_internal_slash_history_record_once", False)
            )

    def test_language_change_unwraps_indented_stdout_for_reload(self):
        """When a slash command runs, stdout is wrapped by an indenting
        ``_build_internal_slash_output_stream``. The language controller
        must drill through that wrapper before clearing the screen and
        redrawing, otherwise the reloaded banner ends up double-indented."""

        class _IndentingWrapper:
            def __init__(self, base):
                self._primary = base

            def write(self, text):
                return self._primary.write(str(text))

            def flush(self):
                return self._primary.flush()

        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            (cfg_dir / "config.jsonc").write_text("{}", encoding="utf-8")
            agent = _FakeLanguageAgent(cfg_dir)
            real_stdout = io.StringIO()
            wrapper = _IndentingWrapper(real_stdout)
            original_stdout = sys.stdout
            sys.stdout = wrapper
            try:
                handled = handle_language_builtin_command(agent, "language zh-CN")
            finally:
                sys.stdout = original_stdout
            self.assertTrue(handled)
            self.assertEqual(agent.reload_calls, 1)
            self.assertIs(
                agent.observed_stdout_during_reload,
                real_stdout,
                "reload must run against the unwrapped terminal stream so the "
                "redrawn banner is not prefixed by slash-output indentation",
            )


if __name__ == "__main__":
    unittest.main()
