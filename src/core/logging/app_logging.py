"""
Application main-process logging: by default writes UTF-8 application log files under the config directory, and child loggers use the application prefix.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ...config.app_info import get_app_log_filename, get_app_logger_root

_LOGGER_NAME = get_app_logger_root()
_file_handler_installed = False
_log_file_path: Optional[Path] = None


def setup_app_logging(config_dir: Optional[Path] = None, *, level: int = logging.INFO) -> logging.Logger:
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
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(fh)
        _log_file_path = log_path
        _file_handler_installed = True
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
    _file_handler_installed = False
    _log_file_path = None
