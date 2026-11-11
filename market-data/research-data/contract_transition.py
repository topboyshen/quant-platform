#!/usr/bin/env python3
"""Explicit, offline policy transition. Append a future boundary; never rewrite rows.

This is not run by the scheduler. Operator approval and a current policy SHA are
required. Both writer locks are held while comparing state and replacing policy.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

import snapshot_contract as contract
from snapshot_runtime import _strict_json


def policy_hash(path):
    raw = contract.read_policy_bytes(path)
    if raw is None:
        return "missing"
    contract.parse_policy(raw)
    return hashlib.sha256(raw).hexdigest()


def rows(path, table):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("database_symlink")
    if not path.exists():
        return {}
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("database_not_regular")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("database_integrity_uncertain")
        if table not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            raise ValueError("database_contract_table_missing")
        result = {day: hashlib.sha256(raw.encode()).hexdigest()
                  for day, raw in conn.execute("SELECT date,payload FROM " + table)}
        if table == "core_market":
            from core_storage import read_core
            from core_contract import digest
            for day, sha in result.items():
                if digest(read_core(day, path)) != sha:
                    raise ValueError("core_row_hash_or_contract_invalid")
        return result


def append_transition(policy, *, active_contract, effective_day, now, full_rows, core_rows):
    if now.tzinfo is None:
        raise ValueError("transition_clock_requires_timezone")
    today = now.astimezone(contract.CST).date().isoformat()
    if effective_day <= today:
        raise ValueError("contract_transition_must_be_future")
    # Existing assignments, including missing days, are immutable: only append.
    old = contract.transitions(policy)
    proposed = {"schema": contract.POLICY_V2, "transitions": [*old,
        {"activeContract": active_contract, "effectiveTradeDate": effective_day}]}
    contract.parse_policy(json.dumps(proposed))
    for expected, data in ((contract.LEGACY, full_rows), (contract.CORE, core_rows)):
        for day in data:
            if day > today or contract.contract_for_day(policy, day) != expected:
                raise ValueError("existing_row_contract_conflict")
            if contract.contract_for_day(proposed, day) != expected:
                raise ValueError("existing_row_assignment_changed")
    return proposed


def transition(path, *, expected_sha256, active_contract, effective_day,
               full_db, core_db, state_root, now=None):
    path, state_root = Path(path), Path(state_root)
    now = now or dt.datetime.now(contract.CST)
    with contextlib.ExitStack() as stack:
        # Same fixed order for all operators, plus the actual writer lock paths.
        for directory in (state_root, state_root / "core-market"):
            directory.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(open(directory / "snapshot.lock", "a+"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if policy_hash(path) != expected_sha256:
            raise ValueError("contract_policy_compare_and_set_failed")
        before_full, before_core = rows(full_db, "snapshot"), rows(core_db, "core_market")
        old = contract.read_policy(path)
        proposed = append_transition(old, active_contract=active_contract,
            effective_day=effective_day, now=now, full_rows=before_full, core_rows=before_core)
        # Recheck immediately before the only mutation. No history table is opened rw.
        if (policy_hash(path) != expected_sha256 or rows(full_db, "snapshot") != before_full
                or rows(core_db, "core_market") != before_core):
            raise ValueError("contract_transition_evidence_changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".contract-policy-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(proposed, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"status": "policy_transition_prepared", "policySha256": policy_hash(path),
                "policy": proposed, "fullRowHashes": before_full, "coreRowHashes": before_core,
                "historyWrites": 0}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--expected-sha256", required=True)
    p.add_argument("--contract", choices=(contract.CORE, contract.LEGACY), required=True)
    p.add_argument("--effective-date", required=True)
    p.add_argument("--full-db", type=Path, required=True)
    p.add_argument("--core-db", type=Path, required=True)
    p.add_argument("--state-root", type=Path, required=True)
    a = p.parse_args(argv)
    result = transition(a.policy, expected_sha256=a.expected_sha256, active_contract=a.contract,
        effective_day=a.effective_date, full_db=a.full_db, core_db=a.core_db, state_root=a.state_root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
