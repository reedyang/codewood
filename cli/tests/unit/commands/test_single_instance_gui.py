import os
import sys
import unittest
from unittest.mock import patch

import cli.main as main_mod
from cli.main import (
    _acquire_gui_single_instance_lock,
    _acquire_posix_instance_lock,
    _launch_gui_app,
    _posix_instance_lock_path,
    _release_posix_instance_lock,
)


class SingleInstanceLockTests(unittest.TestCase):
    def tearDown(self):
        # Close any handle this test process still holds so the mutex is
        # released for later tests / real GUI launches on the dev machine.
        if main_mod._GUI_INSTANCE_MUTEX_HANDLE:
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle(main_mod._GUI_INSTANCE_MUTEX_HANDLE)
            except Exception:
                pass
            main_mod._GUI_INSTANCE_MUTEX_HANDLE = None
        _release_posix_instance_lock()

    def test_windows_mutex_acquired_first_time(self):
        if os.name != "nt":
            self.skipTest("named mutex is Windows-only")
        self.assertTrue(_acquire_gui_single_instance_lock())

    def test_windows_mutex_conflicts_while_held(self):
        if os.name != "nt":
            self.skipTest("named mutex is Windows-only")
        self.assertTrue(_acquire_gui_single_instance_lock())
        self.assertFalse(_acquire_gui_single_instance_lock())

    def test_windows_mutex_reacquirable_after_release(self):
        if os.name != "nt":
            self.skipTest("named mutex is Windows-only")
        self.assertTrue(_acquire_gui_single_instance_lock())
        self.assertFalse(_acquire_gui_single_instance_lock())
        self.tearDown()  # close the held handle -> mutex released
        self.assertTrue(_acquire_gui_single_instance_lock())

    def test_posix_lock_acquire_conflict_release(self):
        path = _posix_instance_lock_path()
        path.unlink(missing_ok=True)
        try:
            self.assertTrue(_acquire_posix_instance_lock())
            self.assertFalse(_acquire_posix_instance_lock())
        finally:
            _release_posix_instance_lock()
            self.assertFalse(path.exists())

    def test_posix_lock_reclaims_stale_file(self):
        path = _posix_instance_lock_path()
        path.unlink(missing_ok=True)
        path.write_text("2147483647", encoding="utf-8")  # a dead PID
        try:
            self.assertTrue(_acquire_posix_instance_lock())
            self.assertTrue(path.exists())
            self.assertEqual(
                path.read_text(encoding="utf-8").strip(), str(os.getpid())
            )
        finally:
            _release_posix_instance_lock()

    def test_launch_gui_app_exits_when_instance_running(self):
        # A second launch must not start another GUI: it foregrounds the
        # running window and returns 0 without importing the gui host.
        with patch.object(main_mod, "_acquire_gui_single_instance_lock", return_value=False), patch.object(
            main_mod, "_foreground_running_gui_window", return_value=True
        ) as mock_fg, patch.object(main_mod, "_free_own_console"), patch.object(
            main_mod, "_hide_owned_console_window"
        ):
            result = _launch_gui_app()
        self.assertEqual(result, 0)
        mock_fg.assert_called_once()

    def test_launch_gui_app_proceeds_when_lock_acquired(self):
        # The first launch takes the lock and goes on to start the GUI host;
        # the guard itself must not block it.
        from unittest.mock import MagicMock

        fake_gui = MagicMock()
        fake_gui.main.return_value = 0
        with patch.object(main_mod, "_acquire_gui_single_instance_lock", return_value=True), patch.object(
            main_mod, "_foreground_running_gui_window"
        ) as mock_fg, patch.object(main_mod, "_free_own_console"), patch.object(
            main_mod, "_hide_owned_console_window"
        ), patch.dict(sys.modules, {"gui": fake_gui}):
            result = _launch_gui_app()
        self.assertEqual(result, 0)
        fake_gui.main.assert_called_once()
        mock_fg.assert_not_called()


class ForegroundHelperTests(unittest.TestCase):
    def test_foreground_is_noop_on_non_windows(self):
        if os.name == "nt":
            self.skipTest("would touch the real desktop on Windows")
        self.assertFalse(main_mod._foreground_running_gui_window(timeout=0.1))


if __name__ == "__main__":
    unittest.main()
