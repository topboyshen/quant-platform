"""Small shared primitives for fail-closed service-token authentication."""

from __future__ import annotations

import hmac
import os
import stat

MIN_TOKEN_BYTES = 32
MAX_TOKEN_BYTES = 4096


def read_token_file(file_env_name: str) -> str:
    """Read one ASCII token from a regular, non-symlink secret file.

    A single trailing LF is accepted so standard secret-generation tools can
    safely write the file.  Token bytes must be printable ASCII (0x21-0x7e).
    Every error returns an empty token, which lets callers fail closed without
    logging the path or secret.
    """
    path = os.getenv(file_env_name, "")
    if not path or path != path.strip():
        return ""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    descriptor = None
    try:
        before_open = os.lstat(path)
        if not stat.S_ISREG(before_open.st_mode):
            return ""
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return ""
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return ""
        if (metadata.st_dev, metadata.st_ino) != (
            before_open.st_dev,
            before_open.st_ino,
        ):
            return ""
        if metadata.st_size > MAX_TOKEN_BYTES + 1:
            return ""
        chunks = []
        remaining = MAX_TOKEN_BYTES + 2
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_TOKEN_BYTES + 1:
            return ""
    except (OSError, ValueError):
        return ""
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not MIN_TOKEN_BYTES <= len(raw) <= MAX_TOKEN_BYTES:
        return ""
    if any(byte < 0x21 or byte > 0x7E for byte in raw):
        return ""
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError:
        return ""
    if token != token.strip() or any(character.isspace() for character in token):
        return ""
    return token


def constant_time_matches(supplied: str, expected: str) -> bool:
    return (
        bool(supplied)
        and bool(expected)
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )


def install_flask_token_auth(
    app,
    *,
    token_file_env_name: str,
    header_name: str,
    public_paths,
):
    """Install fail-closed token authentication on a Flask application."""
    from flask import jsonify, request

    public_allowlist = frozenset(public_paths)

    @app.before_request
    def _require_service_token():
        if request.path in public_allowlist:
            return None
        expected = read_token_file(token_file_env_name)
        if not expected:
            response = jsonify({"error": "service_unavailable"})
            response.status_code = 503
        elif not constant_time_matches(request.headers.get(header_name, ""), expected):
            response = jsonify({"error": "unauthorized"})
            response.status_code = 401
        else:
            return None
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return _require_service_token
