"""Unit tests for the desktop host's windowed launcher.

The launcher module lives under ``desktop/host`` (not a Python package on the
import path), so it is loaded by file path — mirroring ``test_browser_overlay``.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_LAUNCHER_PATH = (
    Path(__file__).resolve().parents[4] / "desktop" / "host" / "launcher.py"
)


def _load_launcher_module():
    spec = importlib.util.spec_from_file_location(
        "codewood_test_launcher", str(_LAUNCHER_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher_mod = _load_launcher_module()


class LauncherCommandTests(unittest.TestCase):
    def test_normal_launch_runs_app(self):
        with patch.object(launcher_mod.sys, "argv", ["codewood-gui.exe"]):
            cmd = launcher_mod._backend_command()
        self.assertEqual(cmd[-1], "app")
        self.assertNotIn("--toast-activate", cmd)

    def test_toast_activate_is_passed_through(self):
        args = ["codewood-gui.exe", "--toast-activate", "codewood-activate:activate"]
        with patch.object(launcher_mod.sys, "argv", args):
            cmd = launcher_mod._backend_command()
        self.assertEqual(cmd[-2:], ["--toast-activate", "codewood-activate:activate"])
        self.assertNotEqual(cmd[-1], "app")


if __name__ == "__main__":
    unittest.main()
