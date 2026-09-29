#!/usr/bin/env python3
"""Pure policy selection plus bounded local policy reading; never runs a writer."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import stat

LEGACY = "research.full-snapshot.v1"
CORE = "research.core-market.v1"
CST = dt.timezone(dt.timedelta(hours=8))


def parse_policy(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate_contract_policy_key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict) or set(value) != {"activeContract", "effectiveTradeDate"}:
        raise ValueError("invalid_contract_policy")
    if value["activeContract"] == LEGACY and value["effectiveTradeDate"] is None:
        return value
    day = value["effectiveTradeDate"]
    if (value["activeContract"] != CORE or not isinstance(day, str)
            or dt.date.fromisoformat(day).isoformat() != day):
        raise ValueError("invalid_contract_policy")
    return value


def read_policy(path=None):
    if path is None:
        return {"activeContract": LEGACY, "effectiveTradeDate": None}
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"activeContract": LEGACY, "effectiveTradeDate": None}
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096:
        raise ValueError("invalid_contract_policy_file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096:
            raise ValueError("invalid_contract_policy_file")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("contract_policy_too_large")
    return parse_policy(raw)


def select(policy, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("selection_clock_requires_timezone")
    day = now.astimezone(CST).date().isoformat()
    return (CORE if policy["activeContract"] == CORE and day >= policy["effectiveTradeDate"]
            else LEGACY)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        policy = read_policy(args.policy)
        selected = select(policy, dt.datetime.now(CST))
    except (OSError, ValueError, TypeError):
        p.exit(78, "snapshot contract policy invalid; no writer selected\n")
    print("core" if selected == CORE else "full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
