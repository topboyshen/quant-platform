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
POLICY_V2 = "research.contract-policy.v2"
MAX_TRANSITIONS = 16


def transitions(policy):
    if policy.get("schema") == POLICY_V2:
        return policy["transitions"]
    return ([] if policy["activeContract"] == LEGACY else
            [{"activeContract": CORE, "effectiveTradeDate": policy["effectiveTradeDate"]}])


def contract_for_day(policy, day):
    if not isinstance(day, str) or dt.date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid_contract_day")
    active = LEGACY
    for item in transitions(policy):
        if day < item["effectiveTradeDate"]:
            break
        active = item["activeContract"]
    return active


def first_core_date(policy):
    return next((item["effectiveTradeDate"] for item in transitions(policy)
                 if item["activeContract"] == CORE), None)


def intervals(policy):
    items = transitions(policy)
    return [{"activeContract": item["activeContract"], "fromInclusive": item["effectiveTradeDate"],
             "untilExclusive": items[i + 1]["effectiveTradeDate"] if i + 1 < len(items) else None}
            for i, item in enumerate(items)]


def parse_policy(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate_contract_policy_key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if isinstance(value, dict) and value.get("schema") == POLICY_V2:
        if set(value) != {"schema", "transitions"} or not isinstance(value["transitions"], list) or not 1 <= len(value["transitions"]) <= MAX_TRANSITIONS:
            raise ValueError("invalid_contract_intervals")
        previous_day, previous_contract = "", LEGACY
        for item in value["transitions"]:
            if not isinstance(item, dict) or set(item) != {"activeContract", "effectiveTradeDate"}:
                raise ValueError("invalid_contract_transition")
            day, contract = item["effectiveTradeDate"], item["activeContract"]
            if (not isinstance(day, str) or dt.date.fromisoformat(day).isoformat() != day
                    or day <= previous_day or contract not in {CORE, LEGACY} or contract == previous_contract):
                raise ValueError("nonmonotone_or_redundant_contract_transition")
            previous_day, previous_contract = day, contract
        return value
    if not isinstance(value, dict) or set(value) != {"activeContract", "effectiveTradeDate"}:
        raise ValueError("invalid_contract_policy")
    if value["activeContract"] == LEGACY and value["effectiveTradeDate"] is None:
        return value
    day = value["effectiveTradeDate"]
    if (value["activeContract"] != CORE or not isinstance(day, str)
            or dt.date.fromisoformat(day).isoformat() != day):
        raise ValueError("invalid_contract_policy")
    return value


def read_policy_bytes(path=None):
    if path is None:
        return None
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096:
        raise ValueError("invalid_contract_policy_file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096:
            raise ValueError("invalid_contract_policy_file")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("contract_policy_too_large")
    return raw


def read_policy(path=None):
    raw = read_policy_bytes(path)
    return (parse_policy(raw) if raw is not None else
            {"activeContract": LEGACY, "effectiveTradeDate": None})


def assert_core_history(policy, core_db):
    """Read-only state guard, separate from pure selection: lost policy cannot resume full."""
    import hashlib
    import sqlite3
    path = Path(core_db)
    if path.is_symlink():
        raise ValueError("core_history_database_symlink")
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError("core_history_database_not_regular")
    from core_contract import validate_core
    from snapshot_runtime import _strict_json
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("core_history_database_invalid")
            for day, schema, raw, claimed in conn.execute("SELECT date,schema,payload,payload_sha256 FROM core_market"):
                value = validate_core(_strict_json(raw))
                if (schema != CORE or value["date"] != day
                        or hashlib.sha256(raw.encode()).hexdigest() != claimed
                        or contract_for_day(policy, day) != CORE):
                    raise ValueError("core_history_policy_missing_or_conflicting")
    except sqlite3.Error:
        raise ValueError("core_history_database_unreadable") from None


def select(policy, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("selection_clock_requires_timezone")
    day = now.astimezone(CST).date().isoformat()
    return contract_for_day(policy, day)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--core-db", type=Path, default=Path(__file__).with_name("core-market.db"))
    args = p.parse_args(argv)
    try:
        policy = read_policy(args.policy)
        assert_core_history(policy, args.core_db)
        selected = select(policy, dt.datetime.now(CST))
    except (OSError, ValueError, TypeError):
        p.exit(78, "snapshot contract policy invalid; no writer selected\n")
    print("core" if selected == CORE else "full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
