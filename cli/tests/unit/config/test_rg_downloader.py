import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from cli.config import rg_downloader
from cli.config.app_info import get_app_logger_root
from cli.core.logging.app_logging import (
    get_log_file_path,
    setup_app_logging,
    shutdown_app_logging_handlers,
)


class RgDownloaderFailureTests(unittest.TestCase):
    def setUp(self):
        rg_downloader._rg_status = rg_downloader.RG_STATUS_IDLE
        rg_downloader._rg_status_message = ""
        self.bin_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        rg_downloader._rg_status = rg_downloader.RG_STATUS_IDLE
        rg_downloader._rg_status_message = ""

    def test_github_unreachable_is_not_reported_as_unsupported_platform(self):
        with mock.patch.object(
            rg_downloader,
            "urlopen",
            side_effect=URLError("getaddrinfo failed"),
        ):
            ok = rg_downloader._download_and_extract_rg(self.bin_dir, is_update=False)

        self.assertFalse(ok)
        self.assertEqual(rg_downloader.get_rg_status(), rg_downloader.RG_STATUS_FAILED)
        message = rg_downloader.get_rg_status_message()
        self.assertIn("cannot reach GitHub", message)
        self.assertNotIn("unsupported platform", message)

    def test_github_unreachable_during_update_is_reported_as_update_failure(self):
        with mock.patch.object(
            rg_downloader,
            "urlopen",
            side_effect=URLError("timed out"),
        ):
            ok = rg_downloader._download_and_extract_rg(self.bin_dir, is_update=True)

        self.assertFalse(ok)
        message = rg_downloader.get_rg_status_message()
        self.assertIn("rg update failed", message)
        self.assertIn("cannot reach GitHub", message)
        self.assertNotIn("unsupported platform", message)

    def test_genuinely_unsupported_platform_still_reports_unsupported(self):
        with mock.patch.object(rg_downloader.platform, "system", return_value="FreeBSD"), mock.patch.object(
            rg_downloader.platform, "machine", return_value="mips"
        ):
            ok = rg_downloader._download_and_extract_rg(self.bin_dir, is_update=False)

        self.assertFalse(ok)
        message = rg_downloader.get_rg_status_message()
        self.assertIn("unsupported platform", message)


class RgDownloaderLoggingTests(unittest.TestCase):
    """rg downloader logs must reach the app log file, never the GUI."""

    def test_logger_is_child_of_app_logger_tree(self):
        self.assertTrue(
            rg_downloader._logger.name.startswith(f"{get_app_logger_root()}."),
            rg_downloader._logger.name,
        )
        self.assertTrue(rg_downloader._logger.propagate)

    def test_error_log_goes_to_file_not_stderr(self):
        prev_path = get_log_file_path()
        try:
            shutdown_app_logging_handlers()
            with tempfile.TemporaryDirectory() as td:
                try:
                    setup_app_logging(Path(td))
                    log_file = get_log_file_path()
                    self.assertIsNotNone(log_file)
                    bridge = io.StringIO()
                    with mock.patch.object(sys, "stderr", bridge):
                        rg_downloader._logger.error("rg test error line")
                    self.assertNotIn("rg test error line", bridge.getvalue())
                    self.assertIn(
                        "rg test error line",
                        Path(log_file).read_text(encoding="utf-8"),
                    )
                finally:
                    shutdown_app_logging_handlers()
        finally:
            shutdown_app_logging_handlers()
            if prev_path is not None:
                setup_app_logging(prev_path.parent.parent)


class EnsureRgSyncTests(unittest.TestCase):
    """ensure_rg_sync (used by the packaging scripts) downloads rg only when
    the binary or the version file is missing."""

    def setUp(self):
        rg_downloader._rg_status = rg_downloader.RG_STATUS_IDLE
        rg_downloader._rg_status_message = ""
        self.bin_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        rg_downloader._rg_status = rg_downloader.RG_STATUS_IDLE
        rg_downloader._rg_status_message = ""

    def test_returns_true_without_download_when_both_files_present(self):
        (self.bin_dir / rg_downloader._RG_BINARY_NAME).write_bytes(b"MZ")
        (self.bin_dir / rg_downloader._VERSION_FILENAME).write_text(
            "14.1.0\n", encoding="utf-8"
        )
        with mock.patch.object(
            rg_downloader, "_download_and_extract_rg"
        ) as download:
            ok = rg_downloader.ensure_rg_sync(self.bin_dir)
        self.assertTrue(ok)
        download.assert_not_called()

    def test_downloads_when_binary_missing(self):
        (self.bin_dir / rg_downloader._VERSION_FILENAME).write_text(
            "14.1.0\n", encoding="utf-8"
        )
        with mock.patch.object(
            rg_downloader, "_download_and_extract_rg", return_value=True
        ) as download:
            ok = rg_downloader.ensure_rg_sync(self.bin_dir)
        self.assertTrue(ok)
        download.assert_called_once_with(self.bin_dir, is_update=False)

    def test_downloads_when_version_file_missing(self):
        (self.bin_dir / rg_downloader._RG_BINARY_NAME).write_bytes(b"MZ")
        with mock.patch.object(
            rg_downloader, "_download_and_extract_rg", return_value=True
        ) as download:
            ok = rg_downloader.ensure_rg_sync(self.bin_dir)
        self.assertTrue(ok)
        download.assert_called_once_with(self.bin_dir, is_update=False)

    def test_returns_false_when_download_fails(self):
        with mock.patch.object(
            rg_downloader, "_download_and_extract_rg", return_value=False
        ) as download:
            ok = rg_downloader.ensure_rg_sync(self.bin_dir)
        self.assertFalse(ok)
        download.assert_called_once_with(self.bin_dir, is_update=False)

    def test_last_resort_disabled_blocks_fall_through_logs(self):
        # Mirrors the serve-process guard: with ``logging.lastResort`` disabled,
        # a logger with no handler cannot write to the bridged stderr.
        bridge = io.StringIO()
        stray = logging.getLogger("cli.config.stray")
        stray.propagate = False
        prev = logging.lastResort
        try:
            logging.lastResort = None
            with mock.patch.object(sys, "stderr", bridge):
                stray.error("stray log line")
            self.assertNotIn("stray log line", bridge.getvalue())
        finally:
            logging.lastResort = prev


if __name__ == "__main__":
    unittest.main()
