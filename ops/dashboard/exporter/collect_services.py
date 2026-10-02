#!/usr/bin/env python3
"""Minimal read-only systemd snapshot for the dashboard's 项目状态 page.

Deliberately separate from export_dashboard.py: this is the only component that
talks to systemd. It queries a fixed allowlist of units with an explicit
property list and never reads Environment, ExecStart, EnvironmentFile or any
other credential-bearing field — `systemctl show` without -p would dump those,
so every call passes the property list below.

Judgement:
  * a oneshot that finished normally is healthy, not a fault;
  * a timer that is disabled (for example while the data mode is FROZEN) is
    reported as disabled, not as a failure;
  * only a failed result / failed ActiveState is a fault;
  * a unit that does not exist is 'unknown', never a silent pass.

Usage: collect_services.py --out /var/lib/nng8-dashboard/services.json [--group NAME] [--now ISO]
"""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))
SCHEMA_VERSION = 1
FILE_MODE = 0o640
DIR_MODE = 0o750
TIMEOUT_SECONDS = 20

# (unit, scope, kind). Scope selects `systemctl --user` or the system instance.
UNITS = (
    ("astock-cninfo-precollect.timer", "user", "timer"),
    ("astock-cninfo-precollect.service", "user", "service"),
    ("astock-xhs-source.timer", "user", "timer"),
    ("astock-xhs-source.service", "user", "service"),
    ("astock-xhs-draft.service", "user", "service"),
    # Batch 1 (2026-09-26): the seal is a real publish-branch stage; leaving it
    # out let a failed seal hide behind an all-green list.
    ("astock-research-seal.service", "user", "service"),
    ("astock-morning-notify.service", "user", "service"),
    ("nng8-research-digest.timer", "user", "timer"),
    ("nng8-research-digest.service", "user", "service"),
    ("astock-acceptance-night.timer", "user", "timer"),
    ("astock-acceptance-monday.timer", "user", "timer"),
    ("hermes-telegram-watchdog.timer", "user", "timer"),
    ("research-data-health.timer", "system", "timer"),
    ("research-data-health.service", "system", "service"),
    ("research-data-health-alert.service", "system", "service"),
    ("research-ledger.service", "system", "service"),
    ("research-system-phase2.service", "system", "service"),
)
# Explicit property lists only: these carry state and timestamps, never secrets.
SERVICE_PROPERTIES = ("Id", "LoadState", "Type", "ActiveState", "SubState", "Result",
                      "ExecMainStatus", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
                      "UnitFileState", "NRestarts", "InvocationID", "ConditionResult",
                      "ConditionTimestamp")
TIMER_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
                    "NextElapseUSecRealtime", "NextElapseUSecMonotonic", "LastTriggerUSec")
FORBIDDEN_PROPERTY = re.compile(r"environment|exec(start|stop|reload|condition)|credential|"
                                r"token|secret|passw|argv", re.I)
FAILED_RESULTS = frozenset({"failed", "timeout", "exit-code", "signal", "core-dump", "watchdog",
                            "oom-kill", "resources", "start-limit-hit", "protocol"})
INACTIVE_TIMER_STATES = frozenset({"disabled", "masked", "masked-runtime", "static"})
ONESHOT_TYPES = frozenset({"oneshot"})
LONG_RUNNING_TYPES = frozenset({"simple", "exec", "forking", "notify", "notify-reload", "dbus", "idle"})


def now_cst(override: str | None = None) -> dt.datetime:
    if override:
        return dt.datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(CST)
    return dt.datetime.now(CST)


def parse_show(text: str) -> dict:
    """Parse `systemctl show -p A -p B` output: one Key=Value per line."""
    properties = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if FORBIDDEN_PROPERTY.search(key):
            continue
        properties[key] = value.strip()
    return properties


HUMAN_STAMP = re.compile(r"^\w{3} (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s*(\S+)?$")
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(w|d|h|hr|hrs|min|ms|s)\b")
DURATION_SECONDS = {"w": 604800.0, "d": 86400.0, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
                    "min": 60.0, "s": 1.0, "ms": 0.001}


MAX_MONOTONIC_DELTA = dt.timedelta(days=400)


def parse_timestamp(value, now=None, monotonic_now=None):
    """Parse the formats systemctl actually prints.

    `systemctl --timestamp=unix` converts only some properties (for example
    ExecMainStartTimestamp as ``@<seconds>``), while NextElapseUSecRealtime stays
    human readable and NextElapseUSecMonotonic prints the **absolute monotonic
    instant** as a duration-like string such as ``1w 5d 30min 11.194797s``.
    That value is CLOCK_MONOTONIC time since boot (the same base systemd uses),
    so it must be reduced by the current monotonic clock before being added to
    the wall clock; anything unparsable, already past, or implausibly far away
    returns None instead of a guessed time.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in ("n/a", "infinity"):
        return None
    if text.startswith("@"):
        try:
            seconds = int(text[1:])
        except ValueError:
            return None
        return dt.datetime.fromtimestamp(seconds, CST) if seconds > 0 else None
    match = HUMAN_STAMP.match(text)
    if match:
        try:
            parsed = dt.datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=CST)
    # Remaining shape: an absolute monotonic instant printed as a duration.
    parts = DURATION_PART.findall(text)
    if parts and DURATION_PART.sub("", text).strip() == "":
        target = sum(DURATION_SECONDS[unit] * float(amount) for amount, unit in parts)
        if target <= 0:
            return None
        baseline = now or dt.datetime.now(CST)
        reference = time.monotonic() if monotonic_now is None else monotonic_now
        delta = dt.timedelta(seconds=target - reference)
        if delta <= dt.timedelta(seconds=-60) or delta > MAX_MONOTONIC_DELTA:
            return None
        return baseline + delta
    return None


def iso(value: dt.datetime | None):
    return value.isoformat() if value else None


def classify(kind: str, props: dict):
    """Return (health, note).

    health: ok | failed | stopped | disabled | ended | unknown.

    'ended' (batch 1): a timer whose SubState is 'elapsed' with no next elapse
    will never fire again (for example a one-shot acceptance timer). It is
    shown as 已结束, never as "定时器在运行", and is not a failure.

    The decision uses the unit type and the real execution record, not only the
    ActiveState/Result pair:
      * a oneshot that finished is ok; a oneshot that never ran has no execution
        timestamps and is 'unknown' (no completion record), never ok;
      * a long-running service that is not active is 'stopped', never explained
        as "a oneshot finished";
      * a timer that is enabled but not active is 'stopped' (it should be
        running); only a unit that is genuinely not enabled is 'disabled'.
    No note attributes a cause such as FROZEN — that is not observable here.
    """
    load_state = props.get("LoadState")
    if load_state == "not-found":
        return "unknown", "单元不存在"
    active, result = props.get("ActiveState"), props.get("Result")
    state = props.get("UnitFileState")
    unit_type = props.get("Type") or None
    has_record = bool(props.get("ExecMainStartTimestamp") or props.get("ExecMainExitTimestamp"))
    if active == "failed" or result in FAILED_RESULTS:
        return "failed", f"Result={result or '-'} ActiveState={active or '-'}"
    if active is None:
        return "unknown", "缺少 ActiveState"
    if kind == "timer":
        if active == "active":
            if timer_has_ended(props):
                return "ended", "已结束：一次性定时器已到期，无下次触发时间"
            return "ok", "定时器在运行"
        if state in INACTIVE_TIMER_STATES:
            return "disabled", f"单元未启用（UnitFileState={state}）"
        if active == "inactive":
            return "stopped", "定时器已启用但当前未在运行"
        return "unknown", f"未预期状态 ActiveState={active}"
    # service
    if active == "active":
        return "ok", "oneshot 正在执行" if unit_type in ONESHOT_TYPES else "服务在运行"
    if active in ("activating", "deactivating", "reloading"):
        return "ok", f"过渡状态 ActiveState={active}"
    if active == "inactive":
        if unit_type in ONESHOT_TYPES:
            if has_record:
                # reset-failed may clear Result but must not turn a nonzero
                # process exit (or an incomplete record) into success.
                status = props.get("ExecMainStatus")
                if status is not None and status != "0":
                    return "failed", f"上次执行退出码={status}（未证明恢复）"
                if result == "success" and status == "0" and parse_timestamp(props.get("ExecMainExitTimestamp")) is not None:
                    if condition_skipped_last(props):
                        return "ok", "最近一次按启动条件跳过（未执行，非失败）；上次执行正常结束"
                    return "ok", "oneshot 正常结束"
                return "unknown", "oneshot 缺少成功完成证据"
            if condition_skipped_last(props):
                return "ok", "按启动条件跳过（未执行，非失败）"
            return "unknown", "oneshot 从未执行（无完成记录）"
        detail = f"Type={unit_type or 'unknown'}"
        if state in INACTIVE_TIMER_STATES:
            detail += f"，UnitFileState={state}"
        if unit_type in LONG_RUNNING_TYPES or unit_type is None:
            return "stopped", f"常驻服务未在运行（{detail}）"
        return "unknown", f"未预期的 Type={unit_type} 与 ActiveState={active}"
    return "unknown", f"未预期组合 ActiveState={active} Result={result}"


def timer_has_ended(props: dict) -> bool:
    """systemd marks a timer that can never fire again as SubState=elapsed."""
    realtime = (props.get("NextElapseUSecRealtime") or "").strip()
    monotonic = (props.get("NextElapseUSecMonotonic") or "").strip()
    return (props.get("SubState") == "elapsed" and realtime in ("", "n/a", "0")
            and monotonic in ("", "n/a", "0", "infinity"))


def condition_skipped_last(props: dict) -> bool:
    """The most recent start request was skipped by a Condition*= check.

    A Condition skip changes no unit state (a previously failed unit stays
    failed, which is still reported as failed), so this is only a note.
    """
    if props.get("ConditionResult") != "no":
        return False
    checked = parse_timestamp(props.get("ConditionTimestamp"))
    exited = parse_timestamp(props.get("ExecMainExitTimestamp"))
    return checked is not None and (exited is None or checked >= exited)


def systemctl_show(unit: str, kind: str, scope: str):
    properties = SERVICE_PROPERTIES if kind == "service" else TIMER_PROPERTIES
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    command.append("show")
    for name in properties:
        command.extend(["-p", name])
    command.extend(["--timestamp=unix", "--no-pager", unit])
    result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                            check=False)
    return result.returncode, result.stdout


def collect(now: dt.datetime, monotonic_now=None):
    units, errors = [], []
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    for unit, scope, kind in UNITS:
        entry: dict[str, object] = {"unit": unit, "scope": scope, "kind": kind}
        try:
            code, stdout = systemctl_show(unit, kind, scope)
        except (OSError, subprocess.SubprocessError):
            entry.update({"health": "unknown", "note": "systemctl 调用失败"})
            units.append(entry)
            errors.append(f"{unit}:systemctl_call_failed")
            continue
        if code != 0 and not stdout.strip():
            entry.update({"health": "unknown", "note": f"systemctl 退出码 {code}"})
            units.append(entry)
            errors.append(f"{unit}:exit_{code}")
            continue
        props = parse_show(stdout)
        health, note = classify(kind, props)
        next_elapse = parse_timestamp(props.get("NextElapseUSecRealtime"), now, monotonic_now)
        next_elapse_source = "realtime" if next_elapse else None
        if next_elapse is None:
            next_elapse = parse_timestamp(props.get("NextElapseUSecMonotonic"), now, monotonic_now)
            next_elapse_source = "monotonic" if next_elapse else None
        last_trigger = parse_timestamp(props.get("LastTriggerUSec"), now, monotonic_now)
        entry.update({
            "health": health,
            "note": note,
            "loadState": props.get("LoadState"),
            "activeState": props.get("ActiveState"),
            "subState": props.get("SubState"),
            "unitFileState": props.get("UnitFileState"),
            "result": props.get("Result"),
            "execMainStatus": props.get("ExecMainStatus"),
            "unitType": props.get("Type"),
            "invocationId": props.get("InvocationID"),
            "restarts": props.get("NRestarts"),
            "lastStart": iso(parse_timestamp(props.get("ExecMainStartTimestamp"))),
            "lastExit": iso(parse_timestamp(props.get("ExecMainExitTimestamp"))),
            "nextElapse": iso(next_elapse),
            "nextElapseSource": next_elapse_source,
            "lastTrigger": iso(last_trigger),
        })
        units.append(entry)
    return {"schemaVersion": SCHEMA_VERSION, "checkedAt": iso(now),
            "scope": "read-only allowlist; state, result and timer times only",
            "readErrors": errors, "units": units}


def write_atomic(path: Path, body: bytes, group: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(path.parent, DIR_MODE)
    if path.parent.is_symlink() or (path.exists() and (path.is_symlink() or not path.is_file())):
        raise ValueError("snapshot_path_not_regular_file")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, FILE_MODE)
        if group:
            os.chown(temporary, -1, grp.getgrnam(group).gr_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/var/lib/nng8-dashboard/services.json")
    parser.add_argument("--group", default=None)
    parser.add_argument("--now")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect(now_cst(args.now))
    if args.group:
        payload["group"] = args.group
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    write_atomic(Path(args.out), body, args.group)
    health = {}
    for entry in payload["units"]:
        health[entry["health"]] = health.get(entry["health"], 0) + 1
    print(json.dumps({"checkedAt": payload["checkedAt"], "units": len(payload["units"]),
                      "health": health, "readErrors": payload["readErrors"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
