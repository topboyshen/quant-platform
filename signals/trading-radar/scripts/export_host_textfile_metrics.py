#!/usr/bin/env python3
"""Export host textfile metrics for Prometheus node_exporter.

Writes:
  - disk usage gauges from `df`
  - datasource / Swing EOD outcome gauges from recent journal + index cache

No secrets. Read-only journal + JSON + df, plus activated immutable EOD receipts.
"""
from __future__ import annotations

import json
import hashlib
import stat
import math
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime_contracts.watchdog_metrics import watchdog_metrics as render_watchdog_metrics
from runtime_contracts.calendar import sessions as calendar_sessions, expected_session, instant
from runtime_contracts.eod import validate_watchdog

CALENDAR_PATH = Path(os.environ.get("RADAR_TRADING_CALENDAR_FILE", str(ROOT / "data/trading_calendar.json")))
OUT_DIR = Path(os.environ.get("RADAR_TEXTFILE_DIR", "/home/ubuntu/trading-radar/data/prometheus-textfile"))
INDEX_CACHE = Path(os.environ.get("RADAR_INDEX_CACHE", "/home/ubuntu/trading-radar/data/index_closes_latest.json"))
WATCHDOG_PATH = Path(os.environ.get("RADAR_SWING_WATCHDOG", "/home/ubuntu/trading-radar/data/swing_eod_watchdog.json"))
JOURNAL_SINCE = os.environ.get("RADAR_EOD_JOURNAL_SINCE", "14 days ago")


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def disk_metrics() -> str:
    lines = [
        "# HELP node_textfile_disk_used_percent Disk used percent from df (host textfile).",
        "# TYPE node_textfile_disk_used_percent gauge",
        "# HELP node_textfile_disk_avail_bytes Disk available bytes from df.",
        "# TYPE node_textfile_disk_avail_bytes gauge",
    ]
    try:
        out = subprocess.check_output(
            ["df", "-PB1", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs"],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "\n".join(lines) + "\n"
    for row in out.splitlines()[1:]:
        parts = row.split()
        if len(parts) < 6:
            continue
        filesystem, _blocks, used, avail, pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if mount not in {"/", "/home", "/var", "/var/lib/docker"}:
            # Keep root always; also emit docker data if distinct
            if mount != "/":
                continue
        try:
            used_pct = float(pct.rstrip("%"))
            avail_i = int(avail)
            used_i = int(used)
        except ValueError:
            continue
        mlabel = mount.replace("\\", "\\\\").replace('"', '\\"')
        flabel = filesystem.replace("\\", "\\\\").replace('"', '\\"')
        labels = f'mountpoint="{mlabel}",filesystem="{flabel}"'
        lines.append(f"node_textfile_disk_used_percent{{{labels}}} {used_pct}")
        lines.append(f"node_textfile_disk_avail_bytes{{{labels}}} {avail_i}")
        lines.append(f"node_textfile_disk_used_bytes{{{labels}}} {used_i}")
    return "\n".join(lines) + "\n"


def index_cache_metrics() -> str:
    lines = [
        "# HELP radar_datasource_index_cache_is_primary 1 if index close cache source is pure tencent.",
        "# TYPE radar_datasource_index_cache_is_primary gauge",
        "# HELP radar_datasource_index_cache_uses_fallback 1 if sina appears in index cache source.",
        "# TYPE radar_datasource_index_cache_uses_fallback gauge",
        "# HELP radar_datasource_index_cache_source Source indicator for index close cache.",
        "# TYPE radar_datasource_index_cache_source gauge",
    ]
    primary = 0
    fallback = 0
    source = "unknown"
    try:
        data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("index_cache_not_object")
        source = str(data.get("source") or "unknown")
        primary = int(source == "tencent")
        fallback = int("sina" in source.split("+") or source == "sina")
    except (OSError, ValueError, TypeError, RecursionError, json.JSONDecodeError):
        source = "unreadable"
    safe = re.sub(r"[^a-zA-Z0-9_+.-]", "_", source)[:64]
    lines.append(f"radar_datasource_index_cache_is_primary {primary}")
    lines.append(f"radar_datasource_index_cache_uses_fallback {fallback}")
    lines.append(f'radar_datasource_index_cache_source{{source="{safe}"}} 1')
    return "\n".join(lines) + "\n"


# Journal identity is authoritative; never infer a successful run from log text.
# Retry is the same decision job after data_unavailable; outcomes/repair stay excluded.
EOD_UNIT = "stock-radar-swing-eod.service"
EOD_RETRY_UNIT = "stock-radar-swing-eod-retry.service"
EOD_UNITS = frozenset({EOD_UNIT, EOD_RETRY_UNIT})
JOURNAL_MAX_LINES = 5000
JOURNAL_MAX_BYTES = 8 * 1024 * 1024
JOURNAL_TIMEOUT = 12
# Existing EOD wrapper: timeout 45m plus kill-after 5m; not a new run budget.
MAX_EOD_RUN_SECONDS = 3000
STARTING_ID = "7d4958e842da4a758f6c1cdc7b36dcc5"
FINISHED_ID = "39f53479d3a045ac8e11786248231fbf"
SKIPPED_ID = "0e4284a0caca4bfc81c0bb6786972673"
FAILED_JOB_ID = "be02cf6855d2428ba40df7e9d022f03d"
FAILED_UNIT_ID = "d9b373ed55a64feb8242e02dbe79a49c"
TZ = ZoneInfo("Asia/Shanghai")
INVOCATION_RE = re.compile(r"[0-9a-f]{32}\Z")
KLINE_RESULT = re.compile(r"\[fetch_tencent_kline\].*?腾讯\s*K线获取(成功|失败)(?=[:：\s])")
AKSHARE_RESULT = re.compile(r"\[fetch_akshare_kline\].*?(akshare_[a-zA-Z0-9_]+).*?获取历史数据(成功|失败)(?=[:：\s])")
MACRO_SINA = re.compile(r"(?:来源|source)\s*[:：]\s*sina(?:\+akshare)?", re.I)
KLINE_OBS = re.compile(r"kline_observation (\{.*\})\s*$")
OBS_STATES = frozenset({"ready", "stale", "invalid", "empty", "error"})
OBS_ROUTES = frozenset({"adapter", "direct", "proxy", "cache"})


def read_eod_journal() -> str:
    """Bound subprocess time, row count and in-memory bytes; fail, never truncate."""
    command = ["journalctl", "-u", EOD_UNIT, "-u", EOD_RETRY_UNIT, "--since", JOURNAL_SINCE,
               "--no-pager", "-o", "json", "-n", str(JOURNAL_MAX_LINES),
               "--output-fields=__CURSOR,__REALTIME_TIMESTAMP,_PID,_SYSTEMD_UNIT,"
               "_SYSTEMD_INVOCATION_ID,INVOCATION_ID,UNIT,MESSAGE_ID,JOB_TYPE,"
               "JOB_RESULT,UNIT_RESULT,MESSAGE"]
    deadline = time.monotonic() + JOURNAL_TIMEOUT
    body = bytearray()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        try:
            assert child.stdout is not None  # stdout=PIPE above; narrow the subprocess type.
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, JOURNAL_TIMEOUT)
                    if not selector.select(min(remaining, 0.2)):
                        continue
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    if len(body) + len(chunk) > JOURNAL_MAX_BYTES:
                        raise ValueError("journal_byte_limit")
                    body.extend(chunk)
            status = child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if status:
                raise subprocess.CalledProcessError(status, command)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
    return body.decode("utf-8")


def empty_eod() -> dict:
    return {"parsed": 0, "complete": 0, "has_samples": 0, "in_progress": 0,
            "latest_incomplete": 0, "window_capped": 0, "event": None,
            "counts": None, "skipped_invocations": [], "error": ""}


def manager_result(row: dict):
    """Recognize manager lifecycle fields, not English/localized MESSAGE."""
    mid = row.get("MESSAGE_ID")
    if mid == FINISHED_ID and row.get("JOB_TYPE") == "start" and row.get("JOB_RESULT") == "done":
        return "success"
    if mid == FAILED_JOB_ID and row.get("JOB_TYPE") == "start" and row.get("JOB_RESULT") not in (None, "", "done"):
        return "failed"
    if mid == FAILED_UNIT_ID and row.get("UNIT_RESULT") not in (None, "", "success"):
        return "failed"
    return None


def journal_message(value) -> str:
    """Decode only journald's single binary-field representation, not repeated fields.

    journalctl JSON encodes non-printable fields as arrays of byte integers.
    Repeated fields are arrays of strings/arrays and remain ambiguous evidence.
    Identity fields are deliberately never normalized this way.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(type(byte) is int and 0 <= byte <= 255 for byte in value):
        try:
            return bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("journal_message_invalid_utf8") from exc
    raise ValueError("journal_message_type_invalid")


def _kline_observation(message: str) -> dict | None:
    match = KLINE_OBS.search(message)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    source = str(payload.get("source") or "other")[:64] or "other"
    route = str(payload.get("route") or "other")
    state = str(payload.get("state") or "other")
    if route not in OBS_ROUTES:
        route = "other"
    if state not in OBS_STATES:
        state = "other"
    return {"source": source, "route": route, "state": state}


def count_datasource_lines(rows: list) -> dict:
    counts = {"kline_fail": 0, "kline_ok": 0, "total_amount_fail": 0,
              "fallback_kline": 0, "fallback_macro": 0, "sina_ok": 0,
              "sina_fail": 0, "akshare_ok": 0, "akshare_fail": 0,
              "observation_count": 0}
    observations = []
    for row in rows:
        parsed = _kline_observation(row["MESSAGE"])
        if parsed:
            observations.append(parsed)
    counts["observation_count"] = len(observations)
    use_observations = bool(observations)
    if use_observations:
        labeled = Counter((item["source"], item["route"], item["state"]) for item in observations)
        counts["_obs"] = tuple((*key, n) for key, n in sorted(labeled.items()))
        for item in observations:
            source, state = item["source"], item["state"]
            if source == "tencent_kline" and state == "ready":
                counts["kline_ok"] += 1
            elif source == "tencent_kline" and state in {"error", "empty", "invalid"}:
                counts["kline_fail"] += 1
            akshare_route = source.startswith("akshare") and source != "akshare_kline"
            if akshare_route and state == "ready":
                counts["fallback_kline"] += 1
                counts["akshare_ok"] += 1
                if "sina" in source:
                    counts["sina_ok"] += 1
            elif akshare_route and state in {"error", "empty", "invalid"}:
                counts["akshare_fail"] += 1
                if "sina" in source:
                    counts["sina_fail"] += 1
    for row in rows:
        message = row["MESSAGE"]
        if not use_observations:
            primary = KLINE_RESULT.search(message)
            if primary:
                counts["kline_ok" if primary.group(1) == "成功" else "kline_fail"] += 1
        if "[_tencent_total_amount]" in message and "采集失败" in message:
            counts["total_amount_fail"] += 1
        # Mutually exclusive line classification: macro source text is not a
        # second K-line fallback, even if the same line names both adapters.
        if "宏观切片" in message and MACRO_SINA.search(message):
            counts["fallback_macro"] += 1
        elif not use_observations:
            adapter = AKSHARE_RESULT.search(message)
            if adapter:
                source, result = adapter.groups()
                key = "ok" if result == "成功" else "fail"
                counts["akshare_" + key] += 1
                if source.endswith("_sina"):
                    counts["sina_" + key] += 1
                if result == "成功":
                    counts["fallback_kline"] += 1
    counts["kline_samples"] = counts["kline_fail"] + counts["kline_ok"]
    counts["fallback"] = counts["fallback_kline"] + counts["fallback_macro"]
    return counts


def analyze_eod_journal(body: str, now=None) -> dict:
    """Select the latest executed Invocation; retain verified condition skips as audit."""
    result = empty_eod()
    try:
        now = time.time() if now is None else now
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now <= 0:
            raise ValueError("invalid_sampling_clock")
        if not isinstance(body, str) or len(body.encode("utf-8")) > JOURNAL_MAX_BYTES:
            raise ValueError("journal_byte_limit")
        raw_rows = [line for line in body.splitlines() if line.strip()]
        if len(raw_rows) > JOURNAL_MAX_LINES:
            raise ValueError("journal_line_limit")
        result["window_capped"] = int(len(raw_rows) == JOURNAL_MAX_LINES)
        groups, seen = {}, {}
        for line in raw_rows:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("journal_row_not_object")
            manager = row.get("_PID") == "1" and row.get("UNIT") in EOD_UNITS
            application = row.get("_SYSTEMD_UNIT") in EOD_UNITS and row.get("_PID") != "1"
            if not manager and not application:
                continue  # Never mix outcomes/repair/other units into EOD.
            invocation = row.get("INVOCATION_ID") if manager else row.get("_SYSTEMD_INVOCATION_ID")
            cursor, raw_time = row.get("__CURSOR"), row.get("__REALTIME_TIMESTAMP")
            message = journal_message(row.get("MESSAGE"))
            if (not isinstance(invocation, str) or not INVOCATION_RE.fullmatch(invocation)
                    or not isinstance(cursor, str) or not cursor
                    or not isinstance(raw_time, str) or not raw_time.isdigit()
                    or int(raw_time) <= 0):
                raise ValueError("journal_identity_or_message_invalid")
            timestamp = int(raw_time)
            # Reject repeated-field arrays or corrupt duplicate cursors.
            signature = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if cursor in seen:
                if seen[cursor] != signature:
                    raise ValueError("journal_cursor_conflict")
                continue
            seen[cursor] = signature
            unit = row.get("UNIT") if manager else row.get("_SYSTEMD_UNIT")
            group = groups.setdefault(invocation, {"id": invocation, "unit": unit, "start": None,
                                                  "terminal": None, "last": 0, "rows": [], "skips": []})
            if group["unit"] != unit:
                raise ValueError("journal_invocation_unit_conflict")
            group["last"] = max(group["last"], timestamp)
            if manager:
                if row.get("MESSAGE_ID") == SKIPPED_ID and row.get("UNIT_RESULT") == "exec-condition":
                    group["skips"].append(timestamp)
                if row.get("MESSAGE_ID") == STARTING_ID:
                    group["start"] = timestamp if group["start"] is None else min(timestamp, group["start"])
                terminal = manager_result(row)
                if terminal:
                    if group["terminal"] and group["terminal"]["result"] != terminal:
                        raise ValueError("journal_terminal_conflict")
                    if group["terminal"] is None or timestamp >= group["terminal"]["time"]:
                        group["terminal"] = {"time": timestamp, "result": terminal}
            else:
                group["rows"].append({"time": timestamp, "MESSAGE": message})
        result["parsed"] = 1
        lifecycle_complete = [g for g in groups.values() if g["start"] is not None and g["terminal"]
                              and g["start"] <= g["terminal"]["time"]
                              and all(g["start"] <= r["time"] <= g["terminal"]["time"] for r in g["rows"])]
        skipped = set()
        for group in groups.values():
            if not group["skips"]:
                continue
            # A successful systemd start job can finish without running ExecStart.
            # Require the structured manager verdict and a complete lifecycle;
            # missing/truncated skip evidence must remain unknown.
            if group["terminal"] and group["terminal"]["result"] != "success":
                raise ValueError("journal_skip_terminal_conflict")
            if group not in lifecycle_complete:
                continue
            if not all(group["start"] <= stamp <= group["terminal"]["time"] for stamp in group["skips"]):
                continue
            # Conditions may log, but datasource execution evidence contradicts
            # an ExecCondition skip. Never silently discard such an invocation.
            if any(count_datasource_lines(group["rows"]).values()):
                raise ValueError("journal_skip_datasource_conflict")
            skipped.add(group["id"])
            result["skipped_invocations"].append({
                "invocation_id": group["id"], "unit": group["unit"],
                "start_unixtime": group["start"] / 1_000_000,
                "unixtime": group["terminal"]["time"] / 1_000_000,
                "result": "skipped", "reason": "exec-condition",
            })
        result["skipped_invocations"].sort(key=lambda item: (item["unixtime"], item["invocation_id"]))
        complete = [g for g in lifecycle_complete if not g["skips"]]
        selected = max(complete, key=lambda g: g["terminal"]["time"], default=None)
        latest = max((g for g in groups.values() if g["id"] not in skipped),
                     key=lambda g: g["last"], default=None)
        if latest:
            # An interrupted run is not 'running' forever. Require a genuine
            # start within the already-deployed wrapper's bounded run budget.
            age = now - latest["start"] / 1_000_000 if latest["start"] is not None else None
            result["in_progress"] = int(not latest["skips"] and latest["terminal"] is None and age is not None
                                        and 0 <= age < MAX_EOD_RUN_SECONDS)
            result["latest_incomplete"] = int(latest not in complete)
        if selected:
            counts = count_datasource_lines(selected["rows"])
            result["counts"] = counts
            result["complete"] = 1
            result["has_samples"] = int(
                counts["kline_samples"] + counts["total_amount_fail"] + counts["fallback"]
                + counts.get("observation_count", 0) > 0
            )
            ended = datetime.fromtimestamp(selected["terminal"]["time"] / 1_000_000, TZ)
            result["event"] = {"invocation_id": selected["id"], "event_date": ended.date().isoformat(),
                               "event_time": ended.isoformat(timespec="seconds"),
                               "unixtime": selected["terminal"]["time"] / 1_000_000,
                               "result": selected["terminal"]["result"]}
    except (ValueError, TypeError, OverflowError, OSError, RecursionError) as exc:
        result = empty_eod()
        result["error"] = str(exc)[:80]
    return result


def render_eod_metrics(result: dict, sampled_at=None) -> str:
    sampled_at = int(time.time()) if sampled_at is None else sampled_at
    lines = []
    def gauge(name, help_text, value):
        lines.extend(["# HELP " + name + " " + help_text,
                      "# TYPE " + name + " gauge", name + " " + str(value)])
    prefix = "radar_datasource_"
    counts: dict = result["counts"] if isinstance(result["counts"], dict) else {}
    known = bool(counts) and result["parsed"] == 1 and result["complete"] == 1
    def value(key):
        return counts[key] if known else "NaN"
    denominator = counts["kline_samples"] if known else 0
    ratio = counts["kline_fail"] / denominator if denominator else "NaN"
    for key in ("parsed", "complete", "has_samples", "in_progress", "latest_incomplete", "window_capped"):
        name = "eod_journal_parsed" if key == "parsed" else "eod_" + key
        gauge(prefix + name, "Evidence flag for latest completed EOD journal event; not realtime service health.", result[key])
    gauge(prefix + "eod_event_unixtime", "Completion timestamp of selected historical EOD Invocation; NaN if unavailable.",
          result["event"]["unixtime"] if known else "NaN")
    gauge(prefix + "eod_terminal_success", "Systemd terminal result for selected completed EOD; not datasource success.",
          int(result["event"]["result"] == "success") if known else "NaN")
    gauge(prefix + "primary_kline_failure_ratio", "Recognized Tencent K-line failure log entries / recognized success+failure log entries; NaN without denominator.", ratio)
    for metric, key in (("primary_kline_failed_log_entries", "kline_fail"),
                        ("primary_kline_ok_log_entries", "kline_ok"),
                        ("primary_kline_sample_log_entries", "kline_samples"),
                        ("total_amount_failed_log_entries", "total_amount_fail"),
                        ("fallback_kline_log_entries", "fallback_kline"),
                        ("fallback_macro_log_entries", "fallback_macro"),
                        ("fallback_log_entries", "fallback")):
        gauge(prefix + metric, "Recognized log entries in selected completed EOD only; not HTTP requests or current health.", value(key))
    if known:
        event = result["event"]
        labels = {k: event[k] for k in ("invocation_id", "event_date", "event_time", "result")}
        labels.update({k: str(counts[k]) for k in ("kline_fail", "kline_ok", "kline_samples", "total_amount_fail", "fallback", "fallback_kline", "fallback_macro")})
        label_text = ",".join(k + "=" + json.dumps(v) for k, v in labels.items())
        lines.extend(["# HELP " + prefix + "eod_info Latest complete historical EOD event identity and log sample counts.",
                      "# TYPE " + prefix + "eod_info gauge", prefix + "eod_info{" + label_text + "} 1"])
        records = counts.get("_obs") or ()
        if records:
            lines.extend([
                "# HELP " + prefix + "kline_observation_entries Provider/route/state counts from kline_observation logs; not HTTP requests.",
                "# TYPE " + prefix + "kline_observation_entries gauge",
            ])
            for source, route, state, n in records:
                lines.append(
                    prefix + "kline_observation_entries{source=%s,route=%s,state=%s} %s"
                    % (json.dumps(source), json.dumps(route), json.dumps(state), n)
                )
    # Compatibility only: these are gauges, never cumulative request counters.
    lines.extend(["# HELP " + prefix + "requests_total DEPRECATED compatibility gauge; selected EOD log entries, not requests/window totals.",
                  "# TYPE " + prefix + "requests_total gauge"])
    for source, state, key in (("tencent", "fail", "kline_fail"), ("tencent", "ok", "kline_ok"),
                               ("sina", "ok", "sina_ok"), ("sina", "fail", "sina_fail"),
                               ("akshare", "ok", "akshare_ok"), ("akshare", "fail", "akshare_fail")):
        lines.append(prefix + 'requests_total{source="' + source + '",result="' + state + '"} ' + str(value(key)))
    gauge(prefix + "fallback_total", "DEPRECATED selected EOD fallback log entries; not cumulative counter.", value("fallback"))
    gauge(prefix + "primary_failure_ratio", "DEPRECATED alias of primary_kline_failure_ratio; no samples is NaN.", ratio)
    gauge(prefix + "metrics_unixtime", "Textfile sampling time only, never evidence of EOD completion or recovery.", sampled_at)
    return "\n".join(lines) + "\n"


# Future-only retention. The install-time activation is outside the business
# tree, root-owned, immutable to the collector, and binds exact producer/code.
EOD_EVIDENCE_ROOT = Path('/var/lib/quant-eod-evidence')
EOD_EVIDENCE_ACTIVATION = EOD_EVIDENCE_ROOT / 'ACTIVATION.json'
EOD_EVIDENCE_RECEIPTS = EOD_EVIDENCE_ROOT / 'receipts'
RETENTION_MAX_FILES = 512
RETENTION_MAX_BYTES = 16384
RETENTION_TIMEOUT = 3.0
IDENTITY_PREFIX = 'eod_evidence_identity '
IDENTITY_SCHEMA = 'quant.eod-completion.v1'
ACTIVATION_SCHEMA = 'quant.eod-retention-activation.v1'
RECEIPT_SCHEMA = 'quant.eod-journal-receipt.v1'
RETENTION_CONTRACT_PATHS = ('runtime_contracts/eod.py', 'runtime_contracts/calendar.py',
                            'runtime_contracts/watchdog_metrics.py', 'runtime_contracts/notification_health.py',
                            'runtime_contracts/__init__.py')
SHA_RE = re.compile(r'[0-9a-f]{64}\Z')
TOKEN_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
IDENTITY_KEYS = frozenset({'schema', 'run_id', 'decision_date', 'status', 'outcome',
                           'rule_sha256', 'decision_source_sha256', 'effective_config_sha256'})
COUNT_KEYS = frozenset({'kline_fail', 'kline_ok', 'kline_samples', 'total_amount_fail',
                        'fallback_kline', 'fallback_macro', 'fallback', 'sina_ok',
                        'sina_fail', 'akshare_ok', 'akshare_fail', 'observation_count'})


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError('invalid_json_number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _read_regular(path, *, owner, mode, limit=RETENTION_MAX_BYTES, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != owner or stat.S_IMODE(before.st_mode) != mode
                or before.st_size > limit):
            raise ValueError('retention_file_identity')
        body = bytearray()
        while len(body) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(fd)
        if len(body) > limit or (before.st_ino, before.st_size, before.st_mtime_ns,
                               before.st_ctime_ns) != (after.st_ino, after.st_size,
                               after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('retention_read_changed')
        return bytes(body)
    finally:
        os.close(fd)


def _utc_timestamp(raw):
    if not isinstance(raw, str):
        raise ValueError('activation_clock')
    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('activation_clock_naive')
    return parsed.timestamp()


def load_eod_retention_activation(now):
    # Check fixed ancestor trust; never follow a business-controlled symlink.
    for path in (EOD_EVIDENCE_ROOT.parent.parent, EOD_EVIDENCE_ROOT.parent, EOD_EVIDENCE_ROOT):
        meta = path.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) & 0o022:
            raise ValueError('activation_parent_identity')
    raw = _read_regular(EOD_EVIDENCE_ACTIVATION, owner=0, mode=0o444)
    value = _strict_json(raw)
    if set(value) != {'schema', 'release', 'activated_at', 'producer_sha256', 'collector_sha256', 'contract_sha256'}:
        raise ValueError('activation_shape')
    if (value['schema'] != ACTIVATION_SCHEMA or not TOKEN_RE.fullmatch(value['release'])
            or not SHA_RE.fullmatch(value['producer_sha256'])
            or not SHA_RE.fullmatch(value['collector_sha256'])
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != value['collector_sha256']):
        raise ValueError('activation_source_identity')
    contracts = value['contract_sha256']
    if not isinstance(contracts, dict) or set(contracts) != set(RETENTION_CONTRACT_PATHS):
        raise ValueError('activation_contract_shape')
    for relative, expected_sha in contracts.items():
        if (not isinstance(expected_sha, str) or not SHA_RE.fullmatch(expected_sha)
                or hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected_sha):
            raise ValueError('activation_contract_identity')
    cutoff = _utc_timestamp(value['activated_at'])
    if cutoff <= 0 or cutoff > now:
        raise ValueError('activation_time')
    return value, hashlib.sha256(raw).hexdigest(), cutoff


def _completion_identity(rows):
    markers = []
    for row in rows:
        message = journal_message(row.get('MESSAGE'))
        if IDENTITY_PREFIX in message:
            # Only the exact suffix is decoded; logger prefix is not retained.
            markers.append(_strict_json(message.split(IDENTITY_PREFIX, 1)[1]))
    if len(markers) != 1:
        raise ValueError('completion_identity_missing_or_conflicting')
    identity = markers[0]
    if not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS:
        raise ValueError('completion_identity_shape')
    if (identity['schema'] != IDENTITY_SCHEMA
            or not INVOCATION_RE.fullmatch(identity['run_id'])
            or identity['status'] not in {'success', 'partial', 'failed'}
            or identity['outcome'] not in {'emitted', 'blocked', 'no_signal', 'data_unavailable'}):
        raise ValueError('completion_identity_values')
    if datetime.strptime(identity['decision_date'], '%Y-%m-%d').date().isoformat() != identity['decision_date']:
        raise ValueError('completion_identity_date')
    for key in ('rule_sha256', 'decision_source_sha256', 'effective_config_sha256'):
        if not isinstance(identity[key], str) or not SHA_RE.fullmatch(identity[key]):
            raise ValueError('completion_identity_hash')
    return identity


def build_retention_records(body, activation, activation_sha, cutoff, now):
    """Pure projection of this natural journal read; never query a DB or provider."""
    rows = [_strict_json(line) for line in body.splitlines() if line.strip()]
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('journal_row_not_object')
        manager = row.get('_PID') == '1' and row.get('UNIT') in EOD_UNITS
        application = row.get('_SYSTEMD_UNIT') in EOD_UNITS and row.get('_PID') != '1'
        if not manager and not application:
            continue
        invocation = row.get('INVOCATION_ID') if manager else row.get('_SYSTEMD_INVOCATION_ID')
        if not isinstance(invocation, str) or not INVOCATION_RE.fullmatch(invocation):
            raise ValueError('journal_invocation_invalid')
        groups.setdefault(invocation, []).append(row)
    if len(groups) > 128:
        raise ValueError('retention_group_budget')
    records = []
    for invocation, scoped in groups.items():
        result = analyze_eod_journal('\n'.join(_canonical(row).decode() for row in scoped), now=now)
        if not result['complete'] or result['latest_incomplete'] or result['window_capped']:
            continue
        starts = [int(row['__REALTIME_TIMESTAMP']) / 1_000_000 for row in scoped
                  if row.get('_PID') == '1' and row.get('MESSAGE_ID') == STARTING_ID]
        if not starts or min(starts) < cutoff:
            continue  # Absolutely no installation-time historical backfill.
        event = result['event']
        if event['unixtime'] > now:
            raise ValueError('retention_future_event')
        try:
            identity = _completion_identity([row for row in scoped if row.get('_PID') != '1'])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('post_activation_completion_identity_unavailable') from exc
        if (identity['decision_source_sha256'] != activation['producer_sha256']
                or identity['decision_date'] != event['event_date']):
            raise ValueError('retention_producer_or_date_mismatch')
        # Safe projections only: no raw journal MESSAGE, response body or URLs.
        value = {
            'schema': RECEIPT_SCHEMA, 'activation_sha256': activation_sha,
            'collector_sha256': activation['collector_sha256'], 'identity': identity,
            'event': event, 'started_unixtime': min(starts),
            'unit': next(row['UNIT'] for row in scoped if row.get('_PID') == '1'),
            'input_journal_sha256': _digest(scoped),
            'counts': result['counts'], 'has_samples': result['has_samples'],
        }
        value['receipt_sha256'] = _digest(value)
        if len(_canonical(value)) > RETENTION_MAX_BYTES:
            raise ValueError('retention_record_budget')
        records.append(value)
    return records


def _validate_receipt(value, activation, activation_sha, cutoff, now):
    expected = {'schema', 'activation_sha256', 'collector_sha256', 'identity', 'event',
                'started_unixtime', 'unit', 'input_journal_sha256', 'counts', 'has_samples', 'receipt_sha256'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('receipt_shape')
    unsigned = {key: item for key, item in value.items() if key != 'receipt_sha256'}
    if value['receipt_sha256'] != _digest(unsigned):
        raise ValueError('receipt_hash_conflict')
    if (value['schema'] != RECEIPT_SCHEMA or value['activation_sha256'] != activation_sha
            or value['collector_sha256'] != activation['collector_sha256']
            or value['unit'] not in EOD_UNITS
            or not SHA_RE.fullmatch(value['input_journal_sha256'])):
        raise ValueError('receipt_binding')
    identity = _completion_identity([{'MESSAGE': IDENTITY_PREFIX + _canonical(value['identity']).decode()}])
    if identity['decision_source_sha256'] != activation['producer_sha256']:
        raise ValueError('receipt_producer')
    event = value['event']
    if (not isinstance(event, dict) or set(event) != {'invocation_id','event_date','event_time','unixtime','result'}
            or not INVOCATION_RE.fullmatch(event['invocation_id'])
            or event['result'] not in {'success', 'failed'}
            or type(event['unixtime']) not in (int, float)
            or not math.isfinite(event['unixtime'])
            or type(value['started_unixtime']) not in (int, float)
            or not math.isfinite(value['started_unixtime'])
            or not cutoff <= value['started_unixtime'] <= event['unixtime'] <= now):
        raise ValueError('receipt_event')
    expected_time = datetime.fromtimestamp(event['unixtime'], TZ)
    if (event['event_date'] != expected_time.date().isoformat()
            or event['event_time'] != expected_time.isoformat(timespec='seconds')
            or identity['decision_date'] != event['event_date']):
        raise ValueError('receipt_event_date')
    counts = value['counts']
    if not isinstance(counts, dict) or set(counts) - {'_obs'} != COUNT_KEYS:
        raise ValueError('receipt_counts_shape')
    if any(type(counts[key]) is not int or not 0 <= counts[key] <= JOURNAL_MAX_LINES for key in COUNT_KEYS):
        raise ValueError('receipt_counts')
    if counts['kline_samples'] != counts['kline_fail'] + counts['kline_ok'] or counts['fallback'] != counts['fallback_kline'] + counts['fallback_macro']:
        raise ValueError('receipt_counts_sum')
    observations = counts.get('_obs', [])
    if not isinstance(observations, (list, tuple)) or len(observations) > JOURNAL_MAX_LINES:
        raise ValueError('receipt_observation_shape')
    for row in observations:
        if (not isinstance(row, (list, tuple)) or len(row) != 4
                or not isinstance(row[0], str) or not TOKEN_RE.fullmatch(row[0])
                or row[1] not in OBS_ROUTES | {'other'} or row[2] not in OBS_STATES | {'other'}
                or type(row[3]) is not int or not 0 < row[3] <= JOURNAL_MAX_LINES):
            raise ValueError('receipt_observation')
    if sum(row[3] for row in observations) != counts['observation_count']:
        raise ValueError('receipt_observation_count')
    expected_samples = int(counts['kline_samples'] + counts['total_amount_fail'] + counts['fallback'] + counts['observation_count'] > 0)
    if type(value['has_samples']) is not int or value['has_samples'] != expected_samples:
        raise ValueError('receipt_samples')
    return value


def _receipt_name(value):
    return '{}-{}-{}.json'.format(value['identity']['decision_date'], value['identity']['run_id'], value['event']['invocation_id'])


def _receipt_directory():
    # Installer must create this, with the existing collector's uid. No auto mkdir.
    fd = os.open(EOD_EVIDENCE_RECEIPTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    meta = os.fstat(fd)
    if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o700:
        os.close(fd)
        raise ValueError('receipt_directory_identity')
    return fd


def _receipt_names(fd):
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= RETENTION_MAX_FILES:
                raise ValueError('receipt_directory_budget')
            names.add(entry.name)
    return names


def _store_receipt(fd, value, names):
    name, raw = _receipt_name(value), _canonical(value)
    if name in names:
        prior = _read_regular(name, owner=os.geteuid(), mode=0o444, dir_fd=fd)
        if prior != raw:
            raise ValueError('receipt_existing_conflict')
        return
    if len(names) >= RETENTION_MAX_FILES:
        raise ValueError('receipt_capacity_reached')
    # Link is atomic and cannot replace a pre-existing final name. Temp remains
    # recognizably incomplete after a crash and is never accepted as a receipt.
    temporary = '.pending-' + str(os.getpid()) + '-' + os.urandom(8).hex()
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(out, view)
            if count <= 0:
                raise OSError('short_receipt_write')
            view = view[count:]
        os.fchmod(out, 0o444)
        os.fsync(out)
    finally:
        os.close(out)
    try:
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.unlink(temporary, dir_fd=fd)
    names.add(name)


def _read_anchor_artifact(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in {0, os.geteuid()} or stat.S_IMODE(before.st_mode) & 0o002
                or before.st_size > limit):
            raise ValueError('anchor_file_identity')
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)
        if len(raw) > limit or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('anchor_changed')
        return _strict_json(raw), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def load_eod_retention_anchor(now):
    """Require a genuine current-session final watchdog, never just receipt age.

    Before 15:50 or when a retry is still unresolved, retained receipts cannot
    make missing journal evidence complete. Existing journal evidence is unchanged.
    """
    clock = datetime.fromtimestamp(now, TZ)
    calendar, calendar_sha = _read_anchor_artifact(CALENDAR_PATH, 1024 * 1024)
    required = expected_session(calendar_sessions(calendar, clock), clock)
    watchdog, watchdog_sha = _read_anchor_artifact(WATCHDOG_PATH, 65536)
    validated = validate_watchdog(watchdog, clock)
    latest = watchdog['attempts']['latest']
    checked = instant(watchdog['checked_at'])
    final_due = datetime.combine(required, datetime.min.time(), TZ).replace(hour=15, minute=50)
    if (validated['trade_date'] != required or checked < final_due or checked.timestamp() > now
            or watchdog['deferred'] or watchdog['reason'] not in {'success', 'partial', 'failed', 'evidence_incomplete'}
            or not isinstance(latest, dict) or not isinstance(latest.get('run_id'), str)
            or not INVOCATION_RE.fullmatch(latest['run_id'])
            or latest.get('decision_date') != required.isoformat()
            or latest['status'] not in {'success', 'partial', 'failed'}):
        raise ValueError('retention_final_anchor_unavailable')
    completed = instant(latest['completed_at']).timestamp()
    return {'required_date': required.isoformat(), 'run_id': latest['run_id'],
            'status': latest['status'], 'outcome': latest['decision_outcome'],
            'completed_unixtime': completed, 'checked_unixtime': checked.timestamp(),
            'watchdog_sha256': watchdog_sha, 'calendar_sha256': calendar_sha}


def _receipt_matches_anchor(value, anchor):
    identity = value['identity']
    return (identity['decision_date'] == anchor['required_date']
            and identity['run_id'] == anchor['run_id']
            and identity['status'] == anchor['status'] and identity['outcome'] == anchor['outcome']
            and value['started_unixtime'] <= anchor['completed_unixtime'] <= value['event']['unixtime']
            and value['event']['unixtime'] <= anchor['checked_unixtime'])


def apply_eod_retention(body, current, now=None):
    """Current query failures/conflicts never become healthy from a stale receipt."""
    now = time.time() if now is None else now
    current = dict(current)
    current['retention_success'] = 0
    current['retained_selected'] = 0
    current['retention_status'] = 'not_activated'
    if current['parsed'] != 1 or current['window_capped']:
        current['retention_status'] = 'current_journal_unusable'
        return current
    try:
        activation, activation_sha, cutoff = load_eod_retention_activation(now)
    except FileNotFoundError:
        return current
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        current['retention_status'] = 'activation_invalid'
        return current
    fd = None
    try:
        fd = _receipt_directory()
        deadline = time.monotonic() + RETENTION_TIMEOUT
        names = _receipt_names(fd)
        if len(names) > RETENTION_MAX_FILES or any(not name.endswith('.json') for name in names):
            raise ValueError('receipt_directory_budget_or_incomplete')
        candidates = build_retention_records(body, activation, activation_sha, cutoff, now)
        # Validate before writing so bad source labels never enter the store.
        for value in candidates:
            if time.monotonic() >= deadline:
                raise ValueError('retention_time_budget')
            _validate_receipt(value, activation, activation_sha, cutoff, now)
            _store_receipt(fd, value, names)
        receipts = []
        for name in sorted(names):
            if time.monotonic() >= deadline:
                raise ValueError('retention_time_budget')
            raw = _read_regular(name, owner=os.geteuid(), mode=0o444, dir_fd=fd)
            value = _validate_receipt(_strict_json(raw), activation, activation_sha, cutoff, now)
            if name != _receipt_name(value):
                raise ValueError('receipt_filename_identity')
            receipts.append(value)
        if time.monotonic() >= deadline or _receipt_names(fd) != names:
            raise ValueError('receipt_directory_changed')
        current['retention_success'] = 1
        current['retention_status'] = 'valid'
        latest = max(receipts, key=lambda value: value['event']['unixtime'], default=None)
        # A later active/incomplete current Invocation must remain visible. A
        # newer complete journal result likewise always wins over old receipts.
        if latest is not None and not current['in_progress'] and not current['latest_incomplete']:
            event = current['event']
            if event is None or latest['event']['unixtime'] > event['unixtime']:
                try:
                    anchor = load_eod_retention_anchor(now)
                except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    current['retention_status'] = 'final_anchor_unavailable'
                else:
                    if _receipt_matches_anchor(latest, anchor):
                        current.update({'event': latest['event'], 'counts': latest['counts'],
                                        'has_samples': latest['has_samples'], 'complete': 1,
                                        'retained_selected': 1, 'retention_anchor': anchor})
                    else:
                        current['retention_status'] = 'receipt_not_current_final_batch'
    except (OSError, ValueError, TypeError, KeyError, RecursionError, StopIteration):
        current['retention_status'] = 'unavailable_or_conflicting'
        # Existing journal evidence is still evidence; never fabricate success.
        current['retention_success'] = 0
    finally:
        if fd is not None:
            os.close(fd)
    return current


def watchdog_metrics() -> str:
    return render_watchdog_metrics(WATCHDOG_PATH, CALENDAR_PATH)


def parse_eod_journal() -> str:
    try:
        body = read_eod_journal()
        result = apply_eod_retention(body, analyze_eod_journal(body))
    except (OSError, ValueError, subprocess.SubprocessError):
        result = empty_eod()
    retention = "\n".join([
        "# HELP radar_datasource_eod_retention_success 1 only when activated receipt store validates; not business success.",
        "# TYPE radar_datasource_eod_retention_success gauge",
        "radar_datasource_eod_retention_success " + str(result.get("retention_success", 0)),
        "# HELP radar_datasource_eod_retained_selected 1 when valid retained completion evidence supplements expired journal.",
        "# TYPE radar_datasource_eod_retained_selected gauge",
        "radar_datasource_eod_retained_selected " + str(result.get("retained_selected", 0)),
    ]) + "\n"
    return render_eod_metrics(result) + retention + watchdog_metrics()


def main() -> int:
    disk_body = disk_metrics()
    ds_body = index_cache_metrics() + parse_eod_journal()
    atomic_write(OUT_DIR / "disk.prom", disk_body)
    atomic_write(OUT_DIR / "datasource.prom", ds_body)
    print(f"wrote {OUT_DIR / 'disk.prom'} and {OUT_DIR / 'datasource.prom'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
