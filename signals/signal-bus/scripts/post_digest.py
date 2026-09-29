#!/usr/bin/env python3
"""Retired legacy POST entry point. Never reads credentials or delivery state."""
import json
import sys


def main() -> int:
    print(json.dumps({"status": "held", "reason": "legacy_digest_path_disabled", "sent": 0}), file=sys.stderr)
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
