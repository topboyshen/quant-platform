"""stdin/stdout JSON bridge for the AStock morning workflow."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from akshare_provider import AkShareMorningProvider, get_a_share_morning_context

# Optional SQLite persistence.  Imported lazily so a missing/renamed module
# never breaks the fixed stdin bridge protocol.
try:
    from storage import record_context as _record_context
except Exception:  # pragma: no cover - defensive
    _record_context = None


def run_bridge(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    provider: AkShareMorningProvider | None = None,
) -> dict[str, Any]:
    raw = input_stream.read()
    if not raw.strip():
        raise ValueError("标准输入中没有 request JSON")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ValueError("request JSON 必须是对象")
    response = get_a_share_morning_context(request, provider=provider)
    # Persist to SQLite as an append-only history.  Never let storage
    # errors leak into stdout (which is parsed as one JSON value).
    if _record_context is not None:
        try:
            _record_context(response)
        except Exception as error:  # pragma: no cover - defensive
            sys.stderr.write(f"storage recording skipped: {error}\n")
    output_stream.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    output_stream.flush()
    return response


def main() -> int:
    try:
        run_bridge(sys.stdin, sys.stdout)
        return 0
    except Exception as error:
        # Never mix diagnostics into stdout: the Node bridge parses it as one JSON value.
        sys.stderr.write(f"akshare bridge error: {type(error).__name__}: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
