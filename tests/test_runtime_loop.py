import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.runtime_loop import _format_startup_directory


class RuntimeLoopTests(unittest.TestCase):
    def test_format_startup_directory_replaces_user_home_with_tilde(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fake_home = base / "home_user"
            inside_path = fake_home / "projects" / "demo"
            outside_path = base / "outside" / "demo"
            inside_path.mkdir(parents=True, exist_ok=True)
            outside_path.parent.mkdir(parents=True, exist_ok=True)

            with patch("src.runtime.runtime_loop.Path.home", return_value=fake_home):
                self.assertEqual(
                    _format_startup_directory(str(inside_path)),
                    f"~{os.sep}projects{os.sep}demo",
                )
                self.assertEqual(_format_startup_directory(str(fake_home)), "~")
                self.assertEqual(
                    _format_startup_directory(str(outside_path)),
                    str(outside_path),
                )


if __name__ == "__main__":
    unittest.main()
