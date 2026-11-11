"""通用工具（日志等）。曾存在同名 ``radar/utils.py`` 与 ``radar/utils/`` 包冲突，已合并至此。"""

from __future__ import annotations

import logging
import sys


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    """配置结构化日志"""
    logger = logging.getLogger("radar")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


__all__ = ["setup_logging"]
