#!/usr/bin/env python3
"""Publish bounded, numeric ops evidence; collection failure is never health.

The run timestamp proves publication, not successful source collection. Each of
the four required sources always has an explicit success gauge. A successful
systemctl query is distinct from the queried unit's business outcome.
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

DEST_DIR = "/home/ubuntu/trading-radar/data/prometheus-textfile"
NAME = "qd_ops.prom"
STATE = "/var/lib/qd-ops-metrics/state.json"
SYNC_UNITS = ("quant-nng8-sync.service",)
COLLECTORS = {
    "host": "/var/lib/qd-redacted-log/status.json",
    "frontend": "/var/lib/qd-redacted-log-frontend/status.json",
}
COMMAND_TIMEOUT = 5
MAX_FUTURE_SKEW = 5


def sh(args):
    """Do not interpret stdout from an unsuccessful or incomplete command."""
    result = subprocess.run(args, capture_output=True, text=True,
                            timeout=COMMAND_TIMEOUT)
    if result.returncode != 0:
        raise ValueError("source_command_failed")
    return result.stdout


def timestamp(value, now, integral=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_timestamp_type")
    # Range comparisons precede float conversion so oversized JSON integers
    # become ordinary invalid evidence instead of raising OverflowError.
    if value <= 0 or value > now + MAX_FUTURE_SKEW or not math.isfinite(value):
        raise ValueError("invalid_timestamp_range")
    if integral and not isinstance(value, int):
        raise ValueError("invalid_cache_timestamp")
    return value


def load_state(now):
    try:
        with open(STATE, encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError, TypeError):
        return {}, False
    if not isinstance(raw, dict):
        return {}, False
    result = {}
    valid = True
    for unit in SYNC_UNITS:
        if unit in raw:
            try:
                result[unit] = timestamp(raw[unit], now, integral=True)
            except ValueError:
                valid = False
    return result, valid


def sync_observation(unit, now):
    output = sh(["systemctl", "show", unit, "--timestamp=unix", "-p",
                 "Result,ExecMainStatus,ExecMainExitTimestamp"])
    properties = {}
    for line in output.splitlines():
        if not line:
            continue
        if "=" not in line:
            raise ValueError("malformed_unit_properties")
        key, value = line.split("=", 1)
        if key in properties:
            raise ValueError("duplicate_unit_property")
        properties[key] = value
    required = ("Result", "ExecMainStatus", "ExecMainExitTimestamp")
    if any(key not in properties for key in required):
        raise ValueError("missing_unit_property")
    result = properties["Result"]
    if not re.fullmatch(r"[a-z][a-z-]*", result):
        raise ValueError("invalid_unit_result")
    if not properties["ExecMainStatus"].isdigit():
        raise ValueError("invalid_unit_exit_status")
    raw_timestamp = properties["ExecMainExitTimestamp"]
    if raw_timestamp in ("", "n/a"):
        observed = None
    elif re.fullmatch(r"@?[0-9]+", raw_timestamp):
        observed = timestamp(int(raw_timestamp.lstrip("@")), now, integral=True)
    else:
        raise ValueError("invalid_unit_exit_timestamp")
    if result == "success" and properties["ExecMainStatus"] == "0":
        return observed
    return None


def collector_observation(path, now):
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("invalid_collector_status")
    heartbeat = timestamp(value["heartbeat_unix"], now)
    raw = value["raw_messages_persisted"]
    if not isinstance(raw, bool):
        raise ValueError("invalid_collector_raw_flag")
    return heartbeat, int(raw)


def atomic_write(path, text, mode):
    """Replace only a complete file, cleaning temporary data on all failures."""
    fd, temporary = tempfile.mkstemp(prefix=".qd_ops.",
                                      dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(dry):
    now = time.time()
    timestamp(now, now)
    state, state_valid = load_state(now)
    sources = {name: False for name in
               ("failed_units", "sync", "host_log", "frontend_log")}
    out = ["# HELP qd_systemd_failed_units Number of systemd units in failed state.",
           "# TYPE qd_systemd_failed_units gauge"]
    try:
        output = sh(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain"])
        rows = [line for line in output.splitlines() if line.strip()]
        if any(not re.fullmatch(r"[^\s]+\.[A-Za-z]+", line.split()[0]) for line in rows):
            raise ValueError("malformed_failed_unit_list")
        out.append("qd_systemd_failed_units %d" % len(rows))
        sources["failed_units"] = True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    out += ["# HELP qd_unit_last_success_timestamp_seconds Last observed successful exit of a periodic unit.",
            "# TYPE qd_unit_last_success_timestamp_seconds gauge",
            "# HELP qd_sync_success_seen 1 only after a trustworthy successful observation.",
            "# TYPE qd_sync_success_seen gauge"]
    sync_valid = True
    for unit in SYNC_UNITS:
        try:
            observed = sync_observation(unit, now)
            if observed is not None:
                state[unit] = max(observed, state.get(unit, observed))
        except (OSError, ValueError, subprocess.SubprocessError):
            sync_valid = False
        label = unit[:-8]
        out.append('qd_sync_success_seen{unit="%s"} %d' % (label, int(unit in state)))
        if unit in state:
            out.append('qd_unit_last_success_timestamp_seconds{unit="%s"} %d' %
                       (label, state[unit]))
    sources["sync"] = sync_valid
    out += ["# HELP qd_log_collector_heartbeat_timestamp_seconds Redacted log collector heartbeat.",
            "# TYPE qd_log_collector_heartbeat_timestamp_seconds gauge",
            "# HELP qd_log_collector_raw_persisted 1 if a collector reports raw messages persisted.",
            "# TYPE qd_log_collector_raw_persisted gauge"]
    for role, path in COLLECTORS.items():
        try:
            heartbeat, raw = collector_observation(path, now)
            out.append('qd_log_collector_heartbeat_timestamp_seconds{role="%s"} %.0f' %
                       (role, heartbeat))
            out.append('qd_log_collector_raw_persisted{role="%s"} %d' % (role, raw))
            sources[role + "_log"] = True
        except (OSError, KeyError, ValueError, TypeError):
            pass
    out += ["# HELP qd_ops_source_success 1 if the named required source was successfully read and validated.",
            "# TYPE qd_ops_source_success gauge"]
    for source, success in sources.items():
        out.append('qd_ops_source_success{source="%s"} %d' % (source, int(success)))
    out += ["# HELP qd_ops_collection_success All required sources and the saved cache were valid.",
            "# TYPE qd_ops_collection_success gauge",
            "qd_ops_collection_success %d" % int(state_valid and all(sources.values())),
            "# HELP qd_ops_metrics_last_run_timestamp_seconds Exporter publication time, not source health.",
            "# TYPE qd_ops_metrics_last_run_timestamp_seconds gauge",
            "qd_ops_metrics_last_run_timestamp_seconds %.0f" % now]
    text = "\n".join(out) + "\n"
    if dry:
        sys.stdout.write(text)
        return 0
    os.makedirs(os.path.dirname(STATE), mode=0o700, exist_ok=True)
    # Publish failure status even when individual sources fail. Persisting a
    # truthful cache first avoids a new healthy publication if that write fails.
    # These are separate atomic files, not a claimed joint transaction: failure
    # replacing the textfile can leave a newer cache and the old complete metrics.
    atomic_write(STATE, json.dumps(state, sort_keys=True) + "\n", 0o600)
    atomic_write(os.path.join(DEST_DIR, NAME), text, 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
