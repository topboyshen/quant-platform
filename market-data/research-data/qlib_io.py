"""Strict reader for QLib ``*.day.bin`` feature files.

QLib stores a four-byte little-endian float32 calendar start index followed by
at least one float32 value. Keeping this parsing in one place prevents callers
from accidentally discarding feature values while trying to skip the header.
"""

from __future__ import annotations

import math
import os
import struct

import numpy as np


class QlibBinFormatError(ValueError):
    """Raised when a QLib day feature file is structurally invalid."""


def read_qlib_day_bin(path: str | os.PathLike[str]) -> tuple[int, np.ndarray]:
    """Return ``(calendar_start_index, float32_values)`` from a day bin file.

    The on-disk format is exactly one little-endian float32 start index followed
    by a non-empty, four-byte-aligned sequence of little-endian float32 values.
    The start index is rejected unless it is finite, integral and non-negative.
    """

    with open(path, "rb") as stream:
        header = stream.read(4)
        if len(header) != 4:
            raise QlibBinFormatError(
                f"invalid QLib day bin header: expected 4 bytes, got {len(header)}"
            )
        payload = stream.read()

    start_value = struct.unpack("<f", header)[0]
    if not math.isfinite(start_value):
        raise QlibBinFormatError("invalid QLib day bin start index: value is not finite")
    if not start_value.is_integer():
        raise QlibBinFormatError("invalid QLib day bin start index: value is not an integer")
    if start_value < 0:
        raise QlibBinFormatError("invalid QLib day bin start index: value is negative")

    if not payload:
        raise QlibBinFormatError("invalid QLib day bin payload: payload is empty")
    if len(payload) % 4 != 0:
        raise QlibBinFormatError(
            f"invalid QLib day bin payload: byte length {len(payload)} is not divisible by 4"
        )

    return int(start_value), np.frombuffer(payload, dtype="<f4")


def load_qlib_features(
    qlib_root: str | os.PathLike[str], symbol: str, fields
) -> tuple[int, dict[str, np.ndarray]]:
    """Load aligned day features for one symbol.

    ``FileNotFoundError`` from an absent feature file is deliberately allowed to
    propagate so existing callers can keep treating a missing instrument as
    unavailable. Structural or cross-field alignment failures are hard errors.
    """

    requested_fields = tuple(fields)
    if not requested_fields:
        raise ValueError("at least one QLib feature field is required")

    loaded = {}
    expected_start = None
    expected_length = None
    reference_field = None

    for field in requested_fields:
        path = os.path.join(os.fspath(qlib_root), "features", symbol, f"{field}.day.bin")
        start, values = read_qlib_day_bin(path)
        if expected_start is None:
            expected_start = start
            expected_length = len(values)
            reference_field = field
        else:
            if start != expected_start:
                raise QlibBinFormatError(
                    "QLib feature start index mismatch for "
                    f"{symbol}: {reference_field}={expected_start}, {field}={start}"
                )
            if len(values) != expected_length:
                raise QlibBinFormatError(
                    "QLib feature length mismatch for "
                    f"{symbol}: {reference_field}={expected_length}, {field}={len(values)}"
                )
        loaded[field] = values

    return expected_start, loaded
