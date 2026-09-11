"""
Application main-process logging: by default writes UTF-8 application log files under the config directory, and child loggers use the application prefix.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from ...config.app_info import get_app_log_filename, get_app_logger_root


class _MillisecondFormatter(logging.Formatter):
    """Log formatter with millisecond-precision timestamps.

    ``logging``'s built-in ``formatTime`` has no ``%f`` support, so emit the
    date-time plus a 3-digit millisecond fraction manually. Kept intentionally
    small; the fraction only appears when the date format carries ``.%f``.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        ct = self.converter(record.created)
        if datefmt and "%f" in datefmt:
            base = time.strftime(datefmt.replace(".%f", ""), ct)
            return f"{base}.{int(record.msecs):03d}"
        return super().formatTime(record, datefmt)


_LOGGER_NAME = get_app_logger_root()
_file_handler_installed = False
_log_file_path: Optional[Path] = None

# Third-party loggers that must never write to the console. ``watchdog``'s
# macOS FSEvents emitter logs "Unhandled exception in FSEventsEmitter" with a
# full traceback from its observer thread (e.g. when a path is already
# scheduled); unbuffered stderr would splice that traceback into the TUI
# transcript. Their records are routed to the application log file instead.
_THIRD_PARTY_LOGGER_NAMES = ("fsevents", "watchdog")


def _route_third_party_loggers_to_file(root: logging.Logger) -> None:
    """Attach the application file handler to third-party loggers directly.

    ``root.propagate`` cannot be used here (setting it would create a cycle,
    since these loggers are children of ``root``); the file handler is attached
    to each child instead and propagation to the console-only root logger is
    disabled.
    """
    file_handler = next(
        (h for h in root.handlers if isinstance(h, logging.FileHandler)), None
    )
    if file_handler is None:
        return
    for name in _THIRD_PARTY_LOGGER_NAMES:
        child = logging.getLogger(name)
        child.propagate = False
        if file_handler not in child.handlers:
            child.addHandler(file_handler)


def setup_app_logging(config_dir: Optional[Path] = None, *, level: int = logging.INFO) -> logging.Logger:
    if os.environ.get("CODEWOOD_DEBUG") == "1":
        level = logging.DEBUG
    """
    Configure the root logger to write the application log file into the config directory.
    Safe to call repeatedly; only one file handler is attached per process.
    """
    global _file_handler_installed, _log_file_path

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(level)
    # Do not bubble up to the root logger to avoid polluting global logging.
    root.propagate = False

    if config_dir is None or _file_handler_installed:
        return root

    try:
        config_dir = Path(config_dir)
        logs_dir = config_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / get_app_log_filename()
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(
            _MillisecondFormatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S.%f",
            )
        )
        root.addHandler(fh)
        _log_file_path = log_path
        _file_handler_installed = True
        _route_third_party_loggers_to_file(root)
    except OSError:
        pass

    return root


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Get a logger; common child logger names are <app>.knowledge and <app>.agent."""
    return logging.getLogger(name)


def get_log_file_path() -> Optional[Path]:
    """Current application log file path (None when not configured or on failure)."""
    return _log_file_path


def shutdown_app_logging_handlers() -> None:
    """
    Close any handlers attached to the application logger (tests call this before deleting a temporary config directory to avoid Windows file locks).
    You can call setup_app_logging again afterward to reattach them.
    """
    global _file_handler_installed, _log_file_path

    root = logging.getLogger(_LOGGER_NAME)
    for h in list(root.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        try:
            root.removeHandler(h)
        except Exception:
            pass
        for name in _THIRD_PARTY_LOGGER_NAMES:
            child = logging.getLogger(name)
            if h in child.handlers:
                child.removeHandler(h)
    _file_handler_installed = False
    _log_file_path = None
