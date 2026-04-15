"""
Smart Shell 主流程日志：默认写入配置目录下 smartshell.log（UTF-8），子 logger 使用 smartshell.* 前缀。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_LOGGER_NAME = "smartshell"
_file_handler_installed = False
_log_file_path: Optional[Path] = None


def setup_app_logging(config_dir: Optional[Path] = None, *, level: int = logging.INFO) -> logging.Logger:
    """
    配置根 logger「smartshell」：向配置目录写入 smartshell.log。
    可重复调用；同一进程内仅挂载一次文件 Handler。
    """
    global _file_handler_installed, _log_file_path

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(level)
    # 不向 root logger 冒泡，避免污染全局 logging
    root.propagate = False

    if config_dir is None or _file_handler_installed:
        return root

    try:
        config_dir = Path(config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        log_path = config_dir / "smartshell.log"
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
    """获取 logger，常用子模块名：smartshell.knowledge、smartshell.agent。"""
    return logging.getLogger(name)


def get_log_file_path() -> Optional[Path]:
    """当前应用日志文件路径（未配置或失败时为 None）。"""
    return _log_file_path


def shutdown_app_logging_handlers() -> None:
    """
    关闭 smartshell 已挂载的 Handler（测试在删除临时 config 目录前调用，避免 Windows 下占用 smartshell.log）。
    之后可再次调用 setup_app_logging 重新挂载。
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
