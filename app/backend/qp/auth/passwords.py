"""Password hashing: argon2id for new hashes; verify legacy QuantDinger hashes (bcrypt, sha256$salt$hex)."""
from __future__ import annotations

import hashlib
import hmac

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()  # argon2id defaults


def scheme_of(password_hash: str) -> str:
    if password_hash.startswith("$argon2"):
        return "argon2"
    if password_hash.startswith(("$2b$", "$2a$", "$2y$")):
        return "legacy_bcrypt"
    if password_hash.startswith("sha256$"):
        return "legacy_sha256"
    return "unknown"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    scheme = scheme_of(password_hash)
    try:
        if scheme == "argon2":
            return _ph.verify(password_hash, password)
        if scheme == "legacy_bcrypt":
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        if scheme == "legacy_sha256":
            parts = password_hash.split("$")
            if len(parts) != 3:
                return False
            computed = hashlib.sha256((password + parts[1]).encode("utf-8")).hexdigest()
            return hmac.compare_digest(computed, parts[2])
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False
    return False


def needs_rehash(password_hash: str) -> bool:
    if scheme_of(password_hash) != "argon2":
        return True
    try:
        return _ph.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
