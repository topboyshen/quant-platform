#!/usr/bin/env python3
"""Enforce the dependency-audit policy.

Policy:
- every Python advisory is blocking unless an unexpired exception matches it;
- npm high/critical advisories are blocking unless an unexpired exception matches
  the package;
- npm moderate/low advisories are reported as "investigate" but not blocking.

The exception file is ``app/backend/dependency-exceptions.json``.  An exception
must carry ``id`` (advisory id or npm package), optional ``package``, ``expires``
(YYYY-MM-DD) and ``reason``.  An expired exception is itself a blocking failure.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "app" / "backend"
FRONTEND = ROOT / "app" / "frontend"
EXCEPTIONS = BACKEND / "dependency-exceptions.json"


def _load_exceptions() -> list[dict[str, Any]]:
    if not EXCEPTIONS.is_file():
        return []
    data = json.loads(EXCEPTIONS.read_text(encoding="utf-8"))
    return list(data.get("exceptions") or [])


def _active_exception(
    entries: list[dict[str, Any]], *, advisory: str, package: str
) -> str | None:
    today = dt.date.today()
    for entry in entries:
        ids = {str(entry.get("id") or ""), str(entry.get("package") or "")}
        if advisory not in ids and package not in ids:
            continue
        try:
            expires = dt.date.fromisoformat(str(entry.get("expires") or ""))
        except ValueError:
            return f"exception {advisory}/{package} has invalid expiry"
        if expires < today:
            return f"exception {advisory}/{package} expired {expires.isoformat()}"
        return None
    return None


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, check=False, cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout


def _json_after_prefix(text: str) -> dict[str, Any] | None:
    """npm may print a banner before JSON; find the first JSON object."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def main() -> int:
    entries = _load_exceptions()
    blocked: list[str] = []
    excepted: list[str] = []
    investigate: list[str] = []

    lock = BACKEND / "requirements.lock"
    code, raw = _run(
        [sys.executable, "-m", "pip_audit", "-r", str(lock), "--format", "json"]
    )
    audit = _json_after_prefix(raw)
    if audit is None:
        blocked.append(f"pip-audit did not produce JSON (exit {code})")
    else:
        python_vulns = 0
        for dep in audit.get("dependencies", []):
            package = str(dep.get("name") or "")
            for vuln in dep.get("vulns", []):
                advisory = str(vuln.get("id") or "unknown")
                python_vulns += 1
                reason = _active_exception(entries, advisory=advisory, package=package)
                target = f"{package} {advisory}"
                if reason:
                    blocked.append(reason)
                elif any(
                    advisory
                    in {str(entry.get("id") or ""), str(entry.get("package") or "")}
                    or package
                    in {str(entry.get("id") or ""), str(entry.get("package") or "")}
                    for entry in entries
                ):
                    excepted.append(target)
                else:
                    blocked.append(target)
        print(
            f"python: dependencies={len(audit.get('dependencies', []))} advisories={python_vulns}"
        )

    code, raw = _run(["npm", "audit", "--omit=dev", "--json"], cwd=FRONTEND)
    npm = _json_after_prefix(raw)
    if npm is None:
        blocked.append(f"npm audit did not produce JSON (exit {code})")
    else:
        vulnerabilities = npm.get("vulnerabilities") or {}
        for package, item in vulnerabilities.items():
            severity = str(item.get("severity") or "")
            if severity not in ("high", "critical"):
                investigate.append(f"{package} ({severity})")
                continue
            reason = _active_exception(entries, advisory=package, package=package)
            target = f"{package} ({severity})"
            if reason:
                blocked.append(reason)
            elif any(
                package in {str(entry.get("id") or ""), str(entry.get("package") or "")}
                for entry in entries
            ):
                excepted.append(target)
            else:
                blocked.append(target)
        print(
            f"npm: high={npm.get('metadata', {}).get('vulnerabilities', {}).get('high', 0)} "
            f"critical={npm.get('metadata', {}).get('vulnerabilities', {}).get('critical', 0)}"
        )

    if excepted:
        print("excepted (time-boxed):")
        for item in excepted:
            print(f"  - {item}")
    if investigate:
        print("investigate (non-blocking):")
        for item in investigate:
            print(f"  - {item}")
    if blocked:
        print("BLOCKED:")
        for item in blocked:
            print(f"  - {item}")
        return 1
    print("dependency audit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
