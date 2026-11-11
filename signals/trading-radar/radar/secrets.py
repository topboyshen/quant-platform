"""
敏感配置：优先从环境变量或对应的 ``*_FILE`` 覆盖 config JSON，
避免把 token 写进仓库或容器元数据。
环境变量未设置时返回原 dict（不拷贝），便于 Dashboard 等只读缓存路径。
"""

from __future__ import annotations

import copy
import os
import stat
from typing import Any, Dict

MAX_SECRET_FILE_BYTES = 4096
_FILE_BACKED_ENV_VARS = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "LLM_API_KEY",
    }
)


class SecretConfigurationError(ValueError):
    """A secret source is ambiguous, unsafe, unavailable, or empty."""


def _read_secret_file(variable_name: str, path: str) -> str:
    descriptor = None
    try:
        before_open = os.lstat(path)
        if not stat.S_ISREG(before_open.st_mode):
            raise ValueError("not a regular file")
        if before_open.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("file is writable by group or other users")
        if before_open.st_size > MAX_SECRET_FILE_BYTES + 1:
            raise ValueError("file is too large")

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("not a regular file")
        if (metadata.st_dev, metadata.st_ino) != (
            before_open.st_dev,
            before_open.st_ino,
        ):
            raise ValueError("file changed while opening")

        chunks = []
        remaining = MAX_SECRET_FILE_BYTES + 2
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_SECRET_FILE_BYTES + 1:
            raise ValueError("file is too large")
        value = raw.decode("utf-8").strip()
        if not value or any(ord(character) < 0x20 for character in value):
            raise ValueError("file is empty or contains control characters")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise SecretConfigurationError(
            f"{variable_name}_FILE is unavailable or unsafe"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_setting(variable_name: str) -> str:
    direct_value = os.getenv(variable_name, "").strip()
    if variable_name not in _FILE_BACKED_ENV_VARS:
        return direct_value

    file_path = os.getenv(f"{variable_name}_FILE", "").strip()
    if direct_value and file_path:
        raise SecretConfigurationError(
            f"{variable_name} and {variable_name}_FILE are mutually exclusive"
        )
    if file_path:
        return _read_secret_file(variable_name, file_path)
    return direct_value


def merge_env_into_config_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    若设置了相应环境变量，则深拷贝 data 并写入对应配置段。
    覆盖范围：
      - TELEGRAM_BOT_TOKEN(_FILE) / TELEGRAM_CHAT_ID(_FILE)
        -> telegram.{bot_token, chat_id}
      - LLM_API_KEY(_FILE) / LLM_BASE_URL / LLM_MODEL
        -> llm.{api_key, base_url, model}
    无任何环境变量设置时返回原 data 引用，避免无谓拷贝。
    """
    if data is None:
        return data

    overrides = {
        "telegram": {
            "bot_token": _read_setting("TELEGRAM_BOT_TOKEN"),
            "chat_id": _read_setting("TELEGRAM_CHAT_ID"),
        },
        "llm": {
            "api_key": _read_setting("LLM_API_KEY"),
            "base_url": _read_setting("LLM_BASE_URL"),
            "model": _read_setting("LLM_MODEL"),
        },
    }

    has_any = any(v for section in overrides.values() for v in section.values())
    if not has_any:
        return data

    out = copy.deepcopy(data) if data else {}
    for section_name, fields in overrides.items():
        if not any(fields.values()):
            continue
        section = out.setdefault(section_name, {})
        for key, value in fields.items():
            if value:
                section[key] = value
    return out
