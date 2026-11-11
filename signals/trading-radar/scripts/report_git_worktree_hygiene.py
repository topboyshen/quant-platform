#!/usr/bin/env python3
"""Classify trading-radar worktree dirt without deleting evidence.

Expected runtime/backup paths stay dirty on purpose. Unexpected source
changes fail closed so they are not mixed into a pin/EOD commit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PREFIXES = (
    "backups/",
    "data/akshare/",
    "data/alertmanager/",
    "data/approved-signals/",
    "data/grafana/",
    "data/index_closes_latest.json",
    "data/prometheus-textfile/",
    "data/reports/",
    "data/eod_daily_report.json",
    "data/research_digest.json",
    "data/research_digest.md",
    "data/runtime-locks/",
    "data/swing_eod_watchdog.json",
    "data/swing_outcome_evidence_v1/",
)


def expected_path(path: str) -> bool:
    # Files match exactly; JSON.py and similarly named source files stay visible.
    return any(path == prefix.rstrip("/") or (prefix.endswith("/") and path.startswith(prefix))
               for prefix in EXPECTED_PREFIXES)


def classify_status_line(line: str) -> str:
    if len(line) < 4 or line[2] != " ":
        return "unexpected"
    paths = line[3:].split(" -> ") if "R" in line[:2] or "C" in line[:2] else [line[3:]]
    # Quoted/octal Git names in text fixtures fail closed. Real status uses -z.
    return "expected" if all(expected_path(path) for path in paths) else "unexpected"


def nul_status_lines(raw: str) -> list[str]:
    entries = iter(raw.split("\0"))
    lines = []
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError("invalid porcelain record")
        if "R" in entry[:2] or "C" in entry[:2]:
            original = next(entries, "")
            if not original:
                raise ValueError("rename source missing")
            # NUL mode gives destination then source; retain both for classification.
            entry = entry[:3] + original + " -> " + entry[3:]
        lines.append(entry)
    return lines


def classify_lines(lines: list[str]) -> dict[str, list[str]]:
    buckets = {"expected": [], "unexpected": []}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        buckets[classify_status_line(line)].append(line)
    return buckets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--porcelain", default="", help="Optional git status --porcelain fixture")
    args = parser.parse_args(argv)
    try:
        if args.porcelain:
            raw = Path(args.porcelain).read_text(encoding="utf-8")
            lines = nul_status_lines(raw) if "\0" in raw else raw.splitlines()
        else:
            output = subprocess.check_output(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=str(ROOT), text=True, timeout=15,
            )
            lines = nul_status_lines(output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("worktree inspection failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    buckets = classify_lines(lines)
    print(f"expected={len(buckets['expected'])} unexpected={len(buckets['unexpected'])}")
    for line in buckets["unexpected"]:
        print(line)
    return 1 if buckets["unexpected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
