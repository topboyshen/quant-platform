"""Bounded read-only, business-day-aware summary schedule readiness.

Readiness is not delivery success. An older completed failure remains explicitly
recorded and requires reconciliation; this module never resets units or UNKNOWN.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from time import monotonic
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "quant.summary-availability.v2"
MARKER = Path("/var/lib/quant-notify-v2/maintenance.paused")
TIMER = "quant-research-daily-summary.timer"
SERVICE = "quant-research-daily-summary.service"
SYSTEMCTL = "/usr/bin/systemctl"
TIMEOUT_SECONDS = 2
MAX_OUTPUT = 16384
SERVICE_FILE = Path("/etc/systemd/system/quant-research-daily-summary.service")
SERVICE_SHA256 = "6ee8b1b745d400512c30a703f55d062d2225fd9df05841e889ff089f24f15636"
DROPIN = Path("/etc/systemd/system/quant-research-daily-summary.service.d/99-notify-v2-maintenance-20261013.conf")
DROPIN_SHA256 = "5056afc961fc83d32703b6d458232ae40d78f0c29f4fa0d14a2bb98f6b3791a7"
EXECUTABLE = "/bin/bash"
SCHEDULED_SOURCE = "/home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/run_scheduled.sh"
COMMON_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
                     "FragmentPath", "DropInPaths")
TIMER_PROPERTIES = COMMON_PROPERTIES + ("NextElapseUSecRealtime",)
SERVICE_PROPERTIES = COMMON_PROPERTIES + (
    "Result", "ExecMainCode", "ExecMainStatus", "ExecMainStartTimestamp",
    "ExecMainExitTimestamp", "InvocationID", "ExecStart",
)
# Kept as a public documentation alias, not used to query unrelated properties.
PROPERTIES = tuple(dict.fromkeys(TIMER_PROPERTIES + SERVICE_PROPERTIES))
MAINTENANCE_SUFFIX = (
    "\n⚠️ 统一日报接管不可用：本条保留原始研究结果并提示通知维护异常；"
    "不代表已补发或已送达，送达未知时不会自动重发。\n"
).encode("utf-8")
REASONS = frozenset({
    "ready", "scheduled_with_previous_failure", "invalid_clock",
    "maintenance_present", "maintenance_path_invalid", "maintenance_directory_missing",
    "maintenance_status_unavailable", "timer_probe_timeout", "service_probe_timeout",
    "timer_probe_unavailable", "service_probe_unavailable", "timer_probe_invalid",
    "service_probe_invalid", "timer_not_loaded", "timer_not_enabled", "timer_not_active",
    "service_not_loaded", "service_failed", "service_busy", "service_state_invalid",
    "service_attempt_unproven", "service_identity_unproven", "next_trigger_unavailable",
    "next_trigger_not_today", "next_trigger_not_future", "availability_probe_failed",
    "availability_changed", "schedule_window_elapsed",
})


def _unit(unit, *, runner):
    properties = TIMER_PROPERTIES if unit == TIMER else SERVICE_PROPERTIES
    response = runner(
        [SYSTEMCTL, "show", unit, "--no-pager", "--property=" + ",".join(properties)],
        capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
             "TZ": "UTC", "SYSTEMD_COLORS": "0"},
    )
    if response.returncode:
        raise OSError("unit_unavailable")
    raw = response.stdout
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
        raise ValueError("unit_output_limit")
    values = {}
    for line in raw.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in properties or key in values:
            raise ValueError("unit_output_shape")
        values[key] = value
    if values.get("Id") != unit or set(values) != set(properties):
        raise ValueError("unit_identity_or_shape")
    return values


def _systemd_clock(value):
    # The fixed subprocess gets TZ=UTC. Never interpret ambiguous "CST".
    match = re.fullmatch(
        r"([A-Za-z]{3}) (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))? UTC", value
    )
    if not match:
        raise ValueError("clock_format")
    parsed = datetime.strptime(match[2], "%Y-%m-%d %H:%M:%S").replace(
        microsecond=int((match[3] or "").ljust(6, "0") or "0"), tzinfo=timezone.utc
    )
    if parsed.strftime("%a") != match[1]:
        raise ValueError("clock_weekday")
    return parsed


def _verify_file(path, expected):
    # These fixed configuration paths have root-owned /etc trust boundaries.
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError("configuration_parent_boundary")
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (0, 0, 0o644)
            or before.st_size > MAX_OUTPUT):
        raise ValueError("configuration_file_identity")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(opened):
            raise ValueError("configuration_changed")
        raw = bytearray()
        while len(raw) <= MAX_OUTPUT:
            part = os.read(fd, min(4096, MAX_OUTPUT + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)
        if (len(raw) > MAX_OUTPUT or identity(before) != identity(after)
                or identity(before) != identity(path.lstat())
                or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError("configuration_changed_or_hash")
    finally:
        os.close(fd)


def _fixed_files():
    _verify_file(SERVICE_FILE, SERVICE_SHA256)
    _verify_file(DROPIN, DROPIN_SHA256)
    return True


def _loaded_identity(service):
    if (service["FragmentPath"] != str(SERVICE_FILE)
            or service["DropInPaths"] != str(DROPIN)
            or service["UnitFileState"] != "static"):
        raise ValueError("loaded_service_identity")
    # Dynamic PID/start/exit fields may follow these static fields. Only one
    # loaded command and its exact path/argv/ignore_errors prefix are accepted.
    raw = service["ExecStart"]
    match = re.fullmatch(
        r"\{\s*path=([^;{}]+?)\s*;\s*argv\[\]=([^;{}]+?)\s*;"
        r"\s*ignore_errors=no\s*;[^{}]*\}\s*", raw,
    )
    if (not match or match[1] != EXECUTABLE
            or match[2] != EXECUTABLE + " " + SCHEDULED_SOURCE):
        raise ValueError("loaded_exec_identity")


def _completed_attempt(service, now):
    if (not re.fullmatch(r"[0-9a-f]{32}", service["InvocationID"])
            or service["InvocationID"] == "0" * 32
            or service["ExecMainCode"] != "1"
            or not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", service["ExecMainStatus"])):
        raise ValueError("attempt_identity")
    code = int(service["ExecMainStatus"])
    if code > 255:
        raise ValueError("exit_range")
    started = _systemd_clock(service["ExecMainStartTimestamp"])
    exited = _systemd_clock(service["ExecMainExitTimestamp"])
    if not started <= exited <= now:
        raise ValueError("attempt_clock_order")
    return {
        "invocationId": service["InvocationID"], "result": service["Result"],
        "exitCode": code, "startedAt": started.isoformat(),
        "exitedAt": exited.isoformat(),
        "businessDate": exited.astimezone(TZ).date().isoformat(),
    }


def _valid_day_clock(day, now):
    return (isinstance(now, datetime) and now.tzinfo is not None
            and isinstance(day, str) and date.fromisoformat(day).isoformat() == day
            and now.astimezone(TZ).date().isoformat() == day)


def probe(*, business_date=None, now=None, marker=MARKER, runner=None,
          identity_checker=None):
    """Production uses fixed paths; runner + identity checker are test-only seams.

    A filesystem identity seam is accepted only together with an explicit
    synthetic subprocess runner. There are no CLI/environment path overrides.
    """
    started_clock = monotonic()
    now = now or datetime.now(timezone.utc)
    result = {"schema": SCHEMA, "available": False, "status": "unknown",
              "reason": "invalid_clock", "checkedAt": None,
              "service_health": {"status": "unknown"},
              "schedule_readiness": {"status": "unknown"},
              "batch_coverage": {"status": "not_evaluated", "reason": "readiness_does_not_prove_delivery"}}
    try:
        day = business_date or now.astimezone(TZ).date().isoformat()
        if not _valid_day_clock(day, now):
            return result
    except (AttributeError, TypeError, ValueError):
        return result
    result.update(checkedAt=now.isoformat(), businessDate=day)

    def fail(reason, status="unknown"):
        result.update(available=False, reason=reason, status=status)
        return result

    marker = Path(marker)
    def inspect_marker():
        try:
            info = marker.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or any(p.is_symlink() for p in marker.parents):
                return fail("maintenance_path_invalid")
            try:
                info = marker.lstat()
            except FileNotFoundError:
                result["maintenancePresent"] = False
                return None
            result["maintenancePresent"] = True
            return fail("maintenance_present" if stat.S_ISREG(info.st_mode)
                        else "maintenance_path_invalid", "blocked")
        except FileNotFoundError:
            return fail("maintenance_directory_missing")
        except OSError:
            return fail("maintenance_status_unavailable")

    blocked = inspect_marker()
    if blocked is not None:
        return blocked
    synthetic_runner = runner is not None
    runner = runner or subprocess.run
    for key, unit in (("timer", TIMER), ("service", SERVICE)):
        try:
            result[key] = _unit(unit, runner=runner)
        except subprocess.TimeoutExpired:
            return fail(key + "_probe_timeout")
        except (OSError, subprocess.SubprocessError):
            return fail(key + "_probe_unavailable")
        except (ValueError, UnicodeError, TypeError):
            return fail(key + "_probe_invalid")
    blocked = inspect_marker()
    if blocked is not None:
        return blocked
    timer, service = result["timer"], result["service"]
    try:
        _loaded_identity(service)
        if identity_checker is not None and not synthetic_runner:
            raise ValueError("runtime_identity_override_refused")
        if (identity_checker or _fixed_files)() is not True:
            raise ValueError("identity_unproven")
    except (OSError, ValueError, TypeError):
        service.pop("ExecStart", None)
        return fail("service_identity_unproven")
    service.pop("ExecStart", None)  # No dynamic command-line representation in archives.
    result["identity"] = {"verified": True, "serviceUnitSha256": SERVICE_SHA256,
                          "dropinSha256": DROPIN_SHA256,
                          "scheduledSource": SCHEDULED_SOURCE}
    # Service health and future scheduling are independent facts. A completed
    # daily slot must not short-circuit the check for a real service failure.
    health = _service_health(service, now, day)
    schedule = _schedule_readiness(timer, now, day, started_clock)
    result["service_health"] = health
    result["schedule_readiness"] = schedule
    if "lastFailure" in health:
        result["lastFailure"] = health["lastFailure"]
    for key in ("nextTriggerAt", "checkedUntilAt"):
        if key in schedule:
            result[key] = schedule[key]
    if health["status"] not in ("healthy", "previous_failure"):
        return fail(health["reason"], health["overallStatus"])
    if schedule["status"] != "ready":
        return fail(schedule["reason"], schedule["overallStatus"])
    degraded = health["status"] == "previous_failure"
    result.update(available=True, status="ready_degraded" if degraded else "ready",
                  reason="scheduled_with_previous_failure" if degraded else "ready")
    return result


def _service_health(service, now, day):
    def bad(reason, status="unknown"):
        return {"status": "failed" if reason == "service_failed" else "unavailable",
                "reason": reason, "overallStatus": status}
    if service["LoadState"] != "loaded":
        return bad("service_not_loaded", "blocked")
    state = (service["ActiveState"], service["SubState"])
    if state == ("failed", "failed"):
        try:
            attempt = _completed_attempt(service, now)
        except (ValueError, TypeError):
            return bad("service_attempt_unproven")
        if attempt["result"] != "exit-code" or not 1 <= attempt["exitCode"] <= 255:
            return bad("service_attempt_unproven")
        failure = {**attempt, "requiresReconciliation": True,
                   "deliveryStatus": "not_evaluated_here", "unknownPreserved": True}
        if attempt["businessDate"] >= day:
            return {**bad("service_failed", "blocked"), "lastFailure": failure}
        return {"status": "previous_failure", "reason": "scheduled_with_previous_failure",
                "overallStatus": "ready_degraded", "lastFailure": failure}
    if state != ("inactive", "dead"):
        return bad("service_busy" if service["ActiveState"] in
                   ("active", "activating", "deactivating") else "service_state_invalid", "blocked")
    if (service["Result"] != "success" or service["ExecMainStatus"] not in ("", "0")
            or service["ExecMainCode"] not in ("", "0", "1")):
        return bad("service_state_invalid")
    fields = ("InvocationID", "ExecMainStartTimestamp", "ExecMainExitTimestamp")
    if any(service[k] for k in fields):
        try:
            attempt = _completed_attempt(service, now)
            if attempt["exitCode"] != 0:
                raise ValueError("success_exit_mismatch")
        except (ValueError, TypeError):
            return bad("service_attempt_unproven")
    elif service["ExecMainCode"] not in ("", "0"):
        return bad("service_attempt_unproven")
    return {"status": "healthy", "reason": "ready", "overallStatus": "ready"}


def _schedule_readiness(timer, now, day, started_clock):
    def bad(reason, status="unknown", **extra):
        return {"status": "window_elapsed" if reason == "schedule_window_elapsed" else "unavailable",
                "reason": reason, "overallStatus": status, **extra}
    if timer["LoadState"] != "loaded":
        return bad("timer_not_loaded", "blocked")
    if (timer["FragmentPath"] != "/etc/systemd/system/quant-research-daily-summary.timer"
            or timer["DropInPaths"] != ""):
        return bad("timer_probe_invalid")
    if timer["UnitFileState"] not in ("enabled", "enabled-runtime"):
        return bad("timer_not_enabled", "blocked")
    if timer["ActiveState"] != "active" or timer["SubState"] not in ("waiting", "running"):
        return bad("timer_not_active", "blocked")
    try:
        trigger = _systemd_clock(timer["NextElapseUSecRealtime"])
    except (ValueError, TypeError):
        return bad("next_trigger_unavailable")
    until = now + timedelta(seconds=max(0, monotonic() - started_clock))
    times = {"nextTriggerAt": trigger.isoformat(), "checkedUntilAt": until.isoformat()}
    if trigger.astimezone(TZ).date().isoformat() != day:
        # The installed daily summary contract closes at16:20 Beijing. Only a
        # future later-day trigger after that cutoff is a passed window; a bad
        # timer before the cutoff remains a scheduling fault.
        cutoff = datetime.fromisoformat(day + "T16:20:00").replace(tzinfo=TZ)
        if trigger > until and trigger.astimezone(TZ).date().isoformat() > day and until >= cutoff:
            return bad("schedule_window_elapsed", "blocked", **times)
        return bad("next_trigger_not_today", "blocked", **times)
    if trigger <= until:
        return bad("next_trigger_not_future", "blocked", **times)
    return {"status": "ready", "reason": "ready", "overallStatus": "ready", **times}


def _checked_shape(value, business_date, now):
    if (not _valid_day_clock(business_date, now) or not isinstance(value, dict)
            or value.get("schema") != SCHEMA or type(value.get("available")) is not bool
            or value.get("businessDate") != business_date
            or value.get("reason") not in REASONS
            or value.get("status") not in ("ready", "ready_degraded", "blocked", "unknown")):
        return False
    checked = datetime.fromisoformat(value["checkedAt"])
    if checked.tzinfo is None or checked != now:
        return False
    if not value["available"]:
        if value["reason"] == "schedule_window_elapsed":
            return _checked_elapsed_shape(value, business_date, now)
        return value["status"] in ("blocked", "unknown") and value["reason"] not in (
            "ready", "scheduled_with_previous_failure")
    health = value.get("service_health", {})
    schedule = value.get("schedule_readiness", {})
    if (health.get("status") != ("previous_failure" if value["status"] == "ready_degraded" else "healthy")
            or schedule.get("status") != "ready" or schedule.get("reason") != "ready"
            or value.get("batch_coverage") != {"status": "not_evaluated", "reason": "readiness_does_not_prove_delivery"}):
        return False
    identity = value.get("identity", {})
    if (not isinstance(identity, dict) or identity.get("verified") is not True
            or identity != {"verified": True, "serviceUnitSha256": SERVICE_SHA256,
                     "dropinSha256": DROPIN_SHA256, "scheduledSource": SCHEDULED_SOURCE}
            or value.get("maintenancePresent") is not False):
        return False
    timer, service = value.get("timer", {}), value.get("service", {})
    if (not isinstance(timer, dict) or set(timer) != set(TIMER_PROPERTIES)
            or not isinstance(service, dict)
            or set(service) != set(SERVICE_PROPERTIES) - {"ExecStart"}
            or timer["Id"] != TIMER or timer["LoadState"] != "loaded"
            or timer["ActiveState"] != "active" or timer["SubState"] not in ("waiting", "running")
            or timer["UnitFileState"] not in ("enabled", "enabled-runtime")
            or timer["FragmentPath"] != "/etc/systemd/system/quant-research-daily-summary.timer"
            or timer["DropInPaths"] != "" or service["Id"] != SERVICE
            or service["LoadState"] != "loaded" or service["UnitFileState"] != "static"
            or service["FragmentPath"] != str(SERVICE_FILE)
            or service["DropInPaths"] != str(DROPIN)):
        return False
    trigger = datetime.fromisoformat(value["nextTriggerAt"])
    until = datetime.fromisoformat(value["checkedUntilAt"])
    if (trigger.tzinfo is None or until.tzinfo is None or not now <= until < trigger
            or trigger.astimezone(TZ).date().isoformat() != business_date
            or _systemd_clock(timer["NextElapseUSecRealtime"]) != trigger):
        return False
    if value["status"] == "ready":
        if (value["reason"] != "ready" or "lastFailure" in value
                or (service["ActiveState"], service["SubState"]) != ("inactive", "dead")
                or service["Result"] != "success" or service["ExecMainStatus"] not in ("", "0")
                or service["ExecMainCode"] not in ("", "0", "1")):
            return False
        fields = ("InvocationID", "ExecMainStartTimestamp", "ExecMainExitTimestamp")
        if any(service[k] for k in fields):
            return _completed_attempt(service, now)["exitCode"] == 0
        return service["ExecMainCode"] in ("", "0")
    if value["status"] != "ready_degraded" or value["reason"] != "scheduled_with_previous_failure":
        return False
    failure = value.get("lastFailure", {})
    fields = {"invocationId", "result", "exitCode", "startedAt", "exitedAt",
              "businessDate", "requiresReconciliation", "deliveryStatus", "unknownPreserved"}
    if (not isinstance(failure, dict) or set(failure) != fields
            or not re.fullmatch(r"[0-9a-f]{32}", failure["invocationId"])
            or failure["invocationId"] == "0" * 32 or failure["result"] != "exit-code"
            or type(failure["exitCode"]) is not int or not 1 <= failure["exitCode"] <= 255
            or failure["requiresReconciliation"] is not True
            or failure["unknownPreserved"] is not True
            or failure["deliveryStatus"] != "not_evaluated_here"):
        return False
    if ((service["ActiveState"], service["SubState"]) != ("failed", "failed")
            or {k: failure[k] for k in ("invocationId", "result", "exitCode", "startedAt",
                                       "exitedAt", "businessDate")}
            != _completed_attempt(service, now)):
        return False
    began = datetime.fromisoformat(failure["startedAt"])
    ended = datetime.fromisoformat(failure["exitedAt"])
    return (began.tzinfo is not None and ended.tzinfo is not None
            and began <= ended <= now
            and ended.astimezone(TZ).date().isoformat() == failure["businessDate"]
            and failure["businessDate"] < business_date)


def _checked_elapsed_shape(value, business_date, now):
    """The late-coverage path gets the same fixed identity gates as ready input."""
    if (value.get("status") != "blocked" or value.get("maintenancePresent") is not False
            or value.get("identity") != {"verified": True, "serviceUnitSha256": SERVICE_SHA256,
                "dropinSha256": DROPIN_SHA256, "scheduledSource": SCHEDULED_SOURCE}
            or value.get("batch_coverage") != {"status": "not_evaluated", "reason": "readiness_does_not_prove_delivery"}):
        return False
    timer, service = value.get("timer"), value.get("service")
    if (not isinstance(timer, dict) or set(timer) != set(TIMER_PROPERTIES)
            or not isinstance(service, dict) or set(service) != set(SERVICE_PROPERTIES) - {"ExecStart"}
            or timer.get("Id") != TIMER or service.get("Id") != SERVICE
            or service.get("FragmentPath") != str(SERVICE_FILE)
            or service.get("DropInPaths") != str(DROPIN)
            or service.get("UnitFileState") != "static"):
        return False
    health = _service_health(service, now, business_date)
    schedule = _schedule_readiness(timer, now, business_date, monotonic())
    if (health.get("status") not in ("healthy", "previous_failure")
            or value.get("service_health") != health
            or value.get("lastFailure") != health.get("lastFailure")
            or schedule.get("status") != "window_elapsed"):
        return False
    actual = value.get("schedule_readiness", {})
    if not isinstance(actual, dict) or set(actual) != set(schedule):
        return False
    if any(actual[k] != schedule[k] for k in schedule if k != "checkedUntilAt"):
        return False
    until = datetime.fromisoformat(actual["checkedUntilAt"])
    trigger = datetime.fromisoformat(actual["nextTriggerAt"])
    return (until.tzinfo is not None and now <= until < trigger
            and value.get("checkedUntilAt") == actual["checkedUntilAt"]
            and value.get("nextTriggerAt") == actual["nextTriggerAt"])


def checked_probe(loader, *, business_date, now):
    """Accept only the dated v2 contract; malformed/legacy-ready input fails closed."""
    try:
        value = loader(business_date=business_date, now=now)
        if not _checked_shape(value, business_date, now):
            raise ValueError("availability_shape")
        return value
    except Exception:
        return {"schema": SCHEMA, "available": False, "status": "unknown",
                "reason": "availability_probe_failed",
                "checkedAt": now.isoformat() if isinstance(now, datetime) else None,
                "businessDate": business_date}

