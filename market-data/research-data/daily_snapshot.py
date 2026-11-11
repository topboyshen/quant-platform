#!/usr/bin/env python3
"""统一每日数据快照层: qlib 量价 + akshare 板块/活跃度 → SQLite append-only。

用法: python3 daily_snapshot.py   (仅采集本地 Qlib 最新完整交易日；不支持历史回填)
输出: 打印摘要 JSON; 全量快照写入 ~/research-data/snapshot.db (表 snapshot, date 主键)
"""
import datetime
import fcntl
import hashlib
import io
import json
import math
import numbers
import os
import re
import sqlite3
import stat
import sys
import syslog
import time
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import qlib_summary
from qlib_summary import limit_pct
import snapshot_runtime as runtime
from snapshot_runtime import (QlibDateError, SnapshotValidationError, SnapshotUsageError, SnapshotTemporaryError, SnapshotDeferredError, _supervised_run_id, _await_supervisor_gate, _supervised_soft_deadline, _atomic_run_json, _snapshot_payload, _payload_sha256, _reject_json_constant, _strict_json, source_error_paths)

from qlib_io import load_qlib_features, read_qlib_day_bin
from cninfo_source import CninfoClient, CninfoError, safe_diagnostic
from concept_source import ConceptError, safe_concept_diagnostic
from ths_concept_source import ThsConceptClient as ConceptClient
from ths_source import SourceError, safe_source_diagnostic
from shareholder_source import ShareholderClient
from source_contract import validate_current_source_payloads
from source_policy import required_source_errors
from collection_window import validate_stage_collection_time

# akshare 需直连中国源, 去掉代理环境变量
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

QLIB = os.path.expanduser("~/qlib/data/cn_data")
DB = os.environ.get(
    "RESEARCH_DATA_SNAPSHOT_DB",
    os.path.expanduser("~/research-data/snapshot.db"),
)
STATE_DIR = os.environ.get(
    "RESEARCH_DATA_STATE_DIR",
    os.path.expanduser("~/.local/state/research-data"),
)
AKSHARE_STAGE_DIR = os.environ.get("RESEARCH_DATA_AKSHARE_STAGE_DIR")
AKSHARE_STAGE_SCHEMA_VERSION = 1
AKSHARE_STAGE_MAX_BYTES = 4 * 1024 * 1024
AKSHARE_STAGE_MIN_SECTORS = 50
AKSHARE_STAGE_REQUIRED_ACTIVITY_COUNTS = (
    "上涨",
    "下跌",
    "平盘",
    "涨停",
    "跌停",
)
SNAPSHOT_DEFERRED_EXIT = 69
QLIB_MIN_ELIGIBLE = 5000
QLIB_MIN_COVERAGE = 0.995  # 容忍每日正常停牌（~28只），大规模数据缺失仍告警
SQLITE_TIMEOUT = 30
_RUN_PROGRESS = {}


def write_run_checkpoint(run_id, **fields):
    return runtime.write_run_checkpoint(run_id, state_dir=STATE_DIR, **fields)


def _verify_supervisor_lock():
    return runtime._verify_supervisor_lock(STATE_DIR)


class AkshareMarketSourceError(RuntimeError):
    """Identify a failed endpoint without exposing an upstream response."""

    def __init__(self, endpoint, cause):
        self.endpoint = endpoint
        self.error_type = type(cause).__name__
        super().__init__(
            f"akshare_market_source_failed:{endpoint}:{self.error_type}"
        )


def write_last_run(record, state_dir=None):
    return runtime.write_last_run(record, state_dir or STATE_DIR)


def emit_status(record, stream=None):
    stream = stream or sys.stdout
    record = dict(record)
    record.setdefault("timestamp", datetime.datetime.now().astimezone().isoformat())
    line = _snapshot_payload(record)
    print(line, file=stream, flush=True)
    try:
        syslog.openlog("research-data-snapshot")
        syslog.syslog(syslog.LOG_INFO, line)
    except (OSError, RuntimeError):
        pass
    write_last_run(record)


def snapshot_run_lock(state_dir=None):
    return runtime.snapshot_run_lock(state_dir or STATE_DIR)


def _ensure_snapshot_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS snapshot("
        "date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
    )


def existing_snapshot(date, db_path=None):
    db_path = db_path or DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT) as conn:
            _ensure_snapshot_table(conn)
            row = conn.execute(
                "SELECT generated_at, payload FROM snapshot WHERE date = ?", (date,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise SnapshotTemporaryError(f"SQLite preflight is busy: {exc}") from exc
        raise SnapshotValidationError(f"SQLite preflight failed: {exc}") from exc
    except sqlite3.Error as exc:
        raise SnapshotValidationError(f"SQLite preflight failed: {exc}") from exc
    if row is None:
        return None
    try:
        payload = _strict_json(row[1])
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            f"existing snapshot {date} contains invalid JSON: {exc}"
        ) from exc
    if payload.get("date") != date or (payload.get("qlib") or {}).get("date") != date:
        raise SnapshotValidationError(
            f"existing snapshot {date} has inconsistent internal dates"
        )
    existing_activity_date = activity_trade_date(payload.get("akshare") or {})
    if existing_activity_date != date:
        raise SnapshotValidationError(
            f"existing snapshot {date} is mixed-date: "
            f"akshare_activity={existing_activity_date}"
        )
    errors = required_source_errors(source_error_paths(payload))
    if errors:
        raise SnapshotValidationError(
            f"existing snapshot {date} contains source errors: {', '.join(errors[:5])}"
        )
    return {
        "generatedAt": row[0],
        "payloadSha256": _payload_sha256(row[1]),
    }


def store_snapshot(snapshot, db_path=None, *, expected_day=None):
    """Persist one immutable snapshot, allowing only an exact-payload retry."""
    _supervised_soft_deadline("SQLite snapshot transaction")
    live_write = expected_day is not None or _supervised_run_id() is not None
    if live_write:
        # Only the live invocation can write. Existing historical records are
        # readable but cannot be recreated or relabelled by today's collector.
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        if snapshot.get("date") != today or (expected_day is not None and expected_day != today):
            raise SnapshotValidationError("historical_snapshot_write_forbidden")
        write_run_checkpoint(_supervised_run_id(), activePhase="sqlite_transaction")
    db_path = db_path or DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    payload = _snapshot_payload(snapshot)
    payload_sha256 = _payload_sha256(payload)
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if live_write:
                _supervised_soft_deadline("SQLite snapshot write")
                today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                if snapshot.get("date") != today:
                    raise SnapshotValidationError("historical_snapshot_write_forbidden")
            _ensure_snapshot_table(conn)
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise SnapshotValidationError(f"SQLite quick_check failed: {integrity}")
            row = conn.execute(
                "SELECT payload FROM snapshot WHERE date = ?", (snapshot["date"],)
            ).fetchone()
            if row is not None:
                if row[0] == payload:
                    return {"status": "idempotent", "payloadSha256": payload_sha256}
                raise SnapshotValidationError(
                    f"snapshot conflict for {snapshot['date']}: immutable row already exists"
                )
            conn.execute(
                "INSERT INTO snapshot(date, generated_at, payload) VALUES(?,?,?)",
                (snapshot["date"], snapshot["generatedAt"], payload),
            )
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise SnapshotValidationError(f"SQLite quick_check failed: {integrity}")
            if live_write:
                _supervised_soft_deadline("SQLite snapshot commit")
                today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                if snapshot.get("date") != today:
                    raise SnapshotValidationError("historical_snapshot_write_forbidden")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise SnapshotTemporaryError(f"SQLite transaction is busy: {exc}") from exc
        raise SnapshotValidationError(f"SQLite transaction failed: {exc}") from exc
    except sqlite3.Error as exc:
        raise SnapshotValidationError(f"SQLite transaction failed: {exc}") from exc
    return {
        "status": "inserted",
        "payloadSha256": payload_sha256,
        "quickCheck": integrity,
    }


def json_scalar(value, allow_nan_as_null=False):
    """Convert numeric scalars to strict JSON, with an explicit NaN whitelist."""
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Real):
        numeric = float(value)
        if math.isnan(numeric):
            if allow_nan_as_null:
                return None
            raise SnapshotValidationError("non-finite numeric source value: NaN")
        if not math.isfinite(numeric):
            raise SnapshotValidationError(
                f"non-finite numeric source value: {numeric}"
            )
        if isinstance(value, numbers.Integral):
            return int(value)
        if type(value) is not float:
            return numeric
    return value


def collect_source(func, *args, attempts=2, _allow_error_payload=False, **kwargs):
    """Retry read-only source collection on exceptions or explicit error payloads."""
    last_error = None
    supervised_failures = []
    for attempt in range(1, attempts + 1):
        try:
            _supervised_soft_deadline("source collection")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if _supervised_run_id() is not None:
                    from bounded_source import call, CollectionBoundaryError, CollectionFailure
                    source_name = getattr(func, "__name__", "source")
                    provider = "akshare" if source_name in {"collect_market_attempt", "akshare_market"} else "source_collection"
                    write_run_checkpoint(_supervised_run_id(), activeSource=source_name,
                                         sourceAttempt=attempt, activeEndpoint=None)
                    budget = min(75, float(os.environ["RESEARCH_DATA_SOFT_DEADLINE_EPOCH"]) - time.time())
                    try:
                        result = call(func, args, kwargs, timeout=budget, state_dir=STATE_DIR)
                    except CollectionBoundaryError as exc:
                        failure = SnapshotTemporaryError(str(exc))
                        detail = {"attempt": attempt, "errorType": "TimeoutError" if str(exc) in {
                            "collector_budget_exhausted", "collector_deadline_exceeded", "collector_budget_exceeded"
                        } else "RuntimeError"}
                        try:
                            with open(os.path.join(STATE_DIR, "runs", _supervised_run_id() + ".json"), encoding="utf-8") as progress_file:
                                progress = _strict_json(progress_file.read())
                            if (provider == "akshare" and progress.get("runId") == _supervised_run_id()
                                    and progress.get("activeSource") == source_name and progress.get("activeEndpoint") in {
                                    "stock_board_industry_summary_ths", "stock_market_activity_legu"}):
                                detail["endpoint"] = progress["activeEndpoint"]
                        except (OSError, ValueError, TypeError, AttributeError):
                            pass  # Unknown endpoint stays unknown; never infer from call order.
                        supervised_failures.append(detail)
                        failure.source_failure = {"provider": provider, "attemptCount": attempt,
                                                  "attempts": supervised_failures}
                        raise failure from None
                    except CollectionFailure as exc:
                        details = {"attempt": attempt, "errorType": exc.error_type}
                        if exc.endpoint is not None:
                            details["endpoint"] = exc.endpoint
                        supervised_failures.append(details)
                        exc.source_failure = {"provider": provider, "attemptCount": attempt,
                                              "attempts": list(supervised_failures)}
                        raise
                else:
                    result = func(*args, **kwargs)
            errors = source_error_paths(result)
            if errors and not _allow_error_payload:
                raise RuntimeError(f"error payloads: {', '.join(errors[:5])}")
            return result
        except SnapshotTemporaryError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(1)
    raise last_error


def activity_trade_date(akshare_payload):
    value = (akshare_payload.get("activity") or {}).get("统计日期")
    if value is None:
        return None
    candidate = str(value).strip()[:10]
    try:
        return datetime.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def validate_daily_source_dates(target_trade_date, akshare_payload):
    if akshare_payload.get("error"):
        raise SnapshotValidationError(
            f"AkShare market activity unavailable: {akshare_payload['error']}"
        )
    activity_date = activity_trade_date(akshare_payload)
    if activity_date is None:
        raise SnapshotValidationError("AkShare market activity has no valid 统计日期")
    if activity_date != target_trade_date:
        raise SnapshotValidationError(
            "trade-date mismatch: "
            f"qlib={target_trade_date}, akshare_activity={activity_date}"
        )
    return activity_date


def _akshare_stage_directory(stage_dir=None):
    return stage_dir or AKSHARE_STAGE_DIR or os.path.join(STATE_DIR, "akshare-market")


def _validate_iso_timestamp(value, field):
    if not isinstance(value, str):
        raise SnapshotValidationError(f"AkShare stage {field} is not a timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotValidationError(
            f"AkShare stage {field} is not a valid timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise SnapshotValidationError(
            f"AkShare stage {field} must include a timezone"
        )
    return parsed


def validate_akshare_market_payload(akshare_payload, expected_date=None):
    if not isinstance(akshare_payload, dict):
        raise SnapshotValidationError("AkShare market payload is not an object")
    errors = source_error_paths(akshare_payload)
    if errors:
        raise SnapshotValidationError(
            f"AkShare market payload contains errors: {', '.join(errors[:5])}"
        )
    sectors = akshare_payload.get("sectors")
    if not isinstance(sectors, list) or len(sectors) < AKSHARE_STAGE_MIN_SECTORS:
        raise SnapshotValidationError(
            "AkShare sector summary is incomplete: "
            f"rows={len(sectors) if isinstance(sectors, list) else 'invalid'}, "
            f"minimum={AKSHARE_STAGE_MIN_SECTORS}"
        )
    required_sector_fields = {
        "name", "chg", "netInflow", "upCount", "downCount", "leader",
    }
    for index, sector in enumerate(sectors):
        if not isinstance(sector, dict) or not required_sector_fields.issubset(sector):
            raise SnapshotValidationError(
                f"AkShare sector row {index} has invalid fields"
            )
        if not isinstance(sector["name"], str) or not sector["name"].strip():
            raise SnapshotValidationError(
                f"AkShare sector row {index} has no valid name"
            )
    activity = akshare_payload.get("activity")
    if not isinstance(activity, dict):
        raise SnapshotValidationError("AkShare market activity is not an object")
    for field in AKSHARE_STAGE_REQUIRED_ACTIVITY_COUNTS:
        value = activity.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, numbers.Real)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise SnapshotValidationError(
                f"AkShare market activity field is invalid: {field}"
            )
    breadth_total = sum(float(activity[field]) for field in ("上涨", "下跌", "平盘"))
    if breadth_total < 1000:
        raise SnapshotValidationError(
            f"AkShare market activity coverage is too low: {breadth_total:g}"
        )
    activity_date = activity_trade_date(akshare_payload)
    if activity_date is None:
        raise SnapshotValidationError("AkShare market activity has no valid 统计日期")
    if expected_date is not None and activity_date != expected_date:
        raise SnapshotValidationError(
            "trade-date mismatch: "
            f"expected={expected_date}, akshare_activity={activity_date}"
        )
    _snapshot_payload(akshare_payload)
    return activity_date


def _akshare_stage_filename(trade_date):
    try:
        normalized = datetime.date.fromisoformat(str(trade_date)).isoformat()
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            f"invalid AkShare stage trade date: {trade_date}"
        ) from exc
    if normalized != trade_date:
        raise SnapshotValidationError(
            f"non-canonical AkShare stage trade date: {trade_date}"
        )
    return f"{normalized}.json"


def _akshare_stage_path(trade_date, stage_dir=None):
    return os.path.join(
        _akshare_stage_directory(stage_dir), _akshare_stage_filename(trade_date)
    )


def _open_private_stage_directory(stage_dir, create=False):
    existed = os.path.lexists(stage_dir)
    if create:
        try:
            os.makedirs(stage_dir, mode=0o700, exist_ok=True)
            if not existed:
                os.chmod(stage_dir, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotValidationError(
                f"AkShare stage directory is unavailable: {exc}"
            ) from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(stage_dir, flags)
    except FileNotFoundError:
        if not create:
            return None
        raise SnapshotValidationError("AkShare stage directory was not created")
    except OSError as exc:
        raise SnapshotValidationError(
            f"AkShare stage directory cannot be opened: {exc}"
        ) from exc
    try:
        if create and not existed:
            os.fchmod(directory_fd, 0o700)
        stage_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(stage_stat.st_mode):
            raise SnapshotValidationError(
                "AkShare stage path is not a regular directory"
            )
        if stage_stat.st_uid != os.geteuid():
            raise SnapshotValidationError(
                "AkShare stage directory owner is not the current user"
            )
        if stat.S_IMODE(stage_stat.st_mode) != 0o700:
            raise SnapshotValidationError(
                "AkShare stage directory mode must be 0700"
            )
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _validate_akshare_stage_document(document, expected_date):
    required_keys = {
        "schemaVersion",
        "tradeDate",
        "collectionWindow",
        "akshare",
        "payloadSha256",
        "recordSha256",
    }
    if not isinstance(document, dict) or set(document) != required_keys:
        raise SnapshotValidationError("AkShare stage schema fields are invalid")
    if document["schemaVersion"] != AKSHARE_STAGE_SCHEMA_VERSION:
        raise SnapshotValidationError(
            f"unsupported AkShare stage schema: {document['schemaVersion']}"
        )
    if document["tradeDate"] != expected_date:
        raise SnapshotValidationError(
            "AkShare stage filename/date mismatch: "
            f"expected={expected_date}, stored={document['tradeDate']}"
        )
    window = document["collectionWindow"]
    if not isinstance(window, dict) or set(window) != {"startedAt", "completedAt"}:
        raise SnapshotValidationError("AkShare stage collection window is invalid")
    started_at = _validate_iso_timestamp(window["startedAt"], "startedAt")
    completed_at = _validate_iso_timestamp(window["completedAt"], "completedAt")
    if completed_at < started_at:
        raise SnapshotValidationError(
            "AkShare stage collection window ends before it starts"
        )
    validate_akshare_market_payload(document["akshare"], expected_date)
    payload_sha256 = _payload_sha256(_snapshot_payload(document["akshare"]))
    if document["payloadSha256"] != payload_sha256:
        raise SnapshotValidationError("AkShare stage payload hash mismatch")
    unsigned = dict(document)
    record_sha256 = unsigned.pop("recordSha256")
    expected_record_sha256 = _payload_sha256(_snapshot_payload(unsigned))
    if record_sha256 != expected_record_sha256:
        raise SnapshotValidationError("AkShare stage record hash mismatch")
    return document


def _load_akshare_market_stage_from_fd(trade_date, directory_fd):
    filename = _akshare_stage_filename(trade_date)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(filename, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotValidationError(f"AkShare stage cannot be opened: {exc}") from exc
    try:
        stage_stat = os.fstat(fd)
        if not stat.S_ISREG(stage_stat.st_mode):
            raise SnapshotValidationError("AkShare stage is not a regular file")
        if stage_stat.st_uid != os.geteuid():
            raise SnapshotValidationError("AkShare stage owner is not the current user")
        if stat.S_IMODE(stage_stat.st_mode) != 0o600:
            raise SnapshotValidationError("AkShare stage mode must be 0600")
        if stage_stat.st_size <= 0 or stage_stat.st_size > AKSHARE_STAGE_MAX_BYTES:
            raise SnapshotValidationError(
                f"AkShare stage size is invalid: {stage_stat.st_size}"
            )
        with os.fdopen(fd, "rb") as stage_file:
            fd = -1
            raw_bytes = stage_file.read(AKSHARE_STAGE_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw_bytes) > AKSHARE_STAGE_MAX_BYTES:
        raise SnapshotValidationError("AkShare stage exceeds the size limit")
    try:
        raw = raw_bytes.decode("utf-8")
        document = _strict_json(raw)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"AkShare stage is invalid JSON: {exc}") from exc
    return _validate_akshare_stage_document(document, trade_date)


def load_akshare_market_stage(trade_date, stage_dir=None):
    _akshare_stage_filename(trade_date)
    stage_dir = _akshare_stage_directory(stage_dir)
    directory_fd = _open_private_stage_directory(stage_dir, create=False)
    if directory_fd is None:
        return None
    try:
        return _load_akshare_market_stage_from_fd(trade_date, directory_fd)
    finally:
        os.close(directory_fd)


def seal_akshare_market_stage(
    akshare_payload, collection_started_at, collection_completed_at, stage_dir=None,
    *, expected_day=None
):
    _supervised_soft_deadline("AkShare stage seal")
    trade_date = validate_akshare_market_payload(akshare_payload)
    live_write = expected_day is not None or _supervised_run_id() is not None
    if live_write:
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        if trade_date != today or (expected_day is not None and expected_day != today):
            raise SnapshotDeferredError("historical_stage_write_forbidden", target=today)
    started_at = _validate_iso_timestamp(collection_started_at, "startedAt")
    completed_at = _validate_iso_timestamp(collection_completed_at, "completedAt")
    if completed_at < started_at:
        raise SnapshotValidationError(
            "AkShare stage collection window ends before it starts"
        )
    try:
        validate_stage_collection_time(trade_date, collection_started_at, collection_completed_at)
    except ValueError as exc:
        raise SnapshotDeferredError(f"AkShare stage collection deferred: {exc}") from None
    stage_dir = _akshare_stage_directory(stage_dir)
    path = _akshare_stage_path(trade_date, stage_dir)
    payload_sha256 = _payload_sha256(_snapshot_payload(akshare_payload))
    document = {
        "schemaVersion": AKSHARE_STAGE_SCHEMA_VERSION,
        "tradeDate": trade_date,
        "collectionWindow": {
            "startedAt": collection_started_at,
            "completedAt": collection_completed_at,
        },
        "akshare": akshare_payload,
        "payloadSha256": payload_sha256,
    }
    document["recordSha256"] = _payload_sha256(_snapshot_payload(document))
    if _supervised_run_id() is not None:
        write_run_checkpoint(_supervised_run_id(), activePhase="stage_seal",
                             stageCandidateTradeDate=trade_date,
                             stageCandidatePayloadSha256=document["payloadSha256"],
                             stageCandidateRecordSha256=document["recordSha256"])
    encoded = (_snapshot_payload(document) + "\n").encode("utf-8")
    if len(encoded) > AKSHARE_STAGE_MAX_BYTES:
        raise SnapshotValidationError("AkShare stage exceeds the size limit")

    directory_fd = _open_private_stage_directory(stage_dir, create=True)
    try:
        existing = _load_akshare_market_stage_from_fd(trade_date, directory_fd)
    except Exception:
        os.close(directory_fd)
        raise
    if existing is not None:
        os.close(directory_fd)
        if existing["payloadSha256"] != payload_sha256:
            raise SnapshotValidationError(
                "AkShare stage conflict for "
                f"{trade_date}: immutable payload already exists"
            )
        return {**existing, "status": "idempotent", "path": path}

    temporary_name = f".{trade_date}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    filename = _akshare_stage_filename(trade_date)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stage_file:
            fd = -1
            stage_file.write(encoded)
            stage_file.flush()
            os.fsync(stage_file.fileno())
        try:
            if live_write:
                _supervised_soft_deadline("AkShare stage publication")
                today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                if trade_date != today:
                    raise SnapshotDeferredError("historical_stage_write_forbidden", target=today)
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published = _load_akshare_market_stage_from_fd(
                trade_date, directory_fd
            )
            if published["recordSha256"] != document["recordSha256"]:
                raise SnapshotValidationError(
                    "AkShare stage post-publish verification failed"
                )
            document = published
            status = "stored"
        except FileExistsError:
            existing = _load_akshare_market_stage_from_fd(
                trade_date, directory_fd
            )
            if existing is None or existing["payloadSha256"] != payload_sha256:
                raise SnapshotValidationError(
                    "AkShare stage conflict for "
                    f"{trade_date}: immutable payload already exists"
                )
            document = existing
            status = "idempotent"
        os.fsync(directory_fd)
    except OSError as exc:
        raise SnapshotValidationError(f"AkShare stage write failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return {**document, "status": status, "path": path}


def capture_current_akshare_market(stage_dir=None, *, expected_day=None):
    collection_started_at = datetime.datetime.now().astimezone().isoformat()
    attempt_count = 0
    attempt_failures = []

    def collect_market_attempt():
        nonlocal attempt_count
        attempt_count += 1
        try:
            return akshare_market()
        except Exception as exc:
            diagnostic = {"attempt": attempt_count, "errorType": type(exc).__name__}
            if isinstance(exc, AkshareMarketSourceError):
                diagnostic["endpoint"] = exc.endpoint
                diagnostic["errorType"] = exc.error_type
            attempt_failures.append(diagnostic)
            raise

    try:
        akshare_payload = collect_source(collect_market_attempt)
    except Exception as exc:
        source_failure = getattr(exc, "source_failure", None) or {
            "provider": "akshare",
            "attemptCount": attempt_count,
            "attempts": attempt_failures,
        }
        raise SnapshotDeferredError(
            f"current AkShare market staging failed: {type(exc).__name__}: {exc}",
            source_failure=source_failure,
        ) from exc
    collection_completed_at = datetime.datetime.now().astimezone().isoformat()
    return seal_akshare_market_stage(
        akshare_payload,
        collection_started_at,
        collection_completed_at,
        stage_dir,
        expected_day=expected_day,
    )


def validate_source_completeness(sources, watch_codes, fund_flow_codes):
    # Optional-source tolerance is defined once in source_policy.py and shared
    # with existing_snapshot() and data_health.has_source_error(); the recorded
    # mootdx error is kept as evidence, not deleted to make a row pass.
    errors = required_source_errors(source_error_paths(sources))
    if errors:
        source_details = []
        for source_name, sanitize in (("announcements", safe_diagnostic),
                                      ("concepts", lambda value: safe_source_diagnostic(value)
                                       or safe_concept_diagnostic(value)),
                                      ("insiders", safe_source_diagnostic)):
            records = sources.get(source_name)
            if isinstance(records, dict):
                for value in records.values():
                    detail = sanitize(value.get("error")) if isinstance(value, dict) else None
                    if detail is not None:
                        source_details.append(detail)
        raise SnapshotValidationError(
            f"source collection contains errors: {', '.join(errors[:8])}",
            details={"sourceErrors": source_details[:8]} if source_details else {},
        )

    akshare_payload = sources["akshare"]
    if not akshare_payload.get("sectors"):
        raise SnapshotValidationError("AkShare sector summary is empty")
    spot_total = ((sources["spot"].get("breadth") or {}).get("total") or 0)
    if spot_total < 1000:
        raise SnapshotValidationError(f"spot market coverage too low: {spot_total}")
    if not sources["news"]:
        raise SnapshotValidationError("news collection is empty")
    if not sources["hotStocks"]:
        raise SnapshotValidationError("hot-stock collection is empty")
    if len(sources["globalMarkets"]) < 3:
        raise SnapshotValidationError("global-market collection is incomplete")

    expected_by_source = {
        # mootdx excluded: optional source (IDC-IP block, see validate_source_completeness)
        "tencent": watch_codes,
        "baostock": watch_codes,
        "announcements": watch_codes,
        "concepts": watch_codes,
        "insiders": watch_codes,
        "fundFlow": fund_flow_codes,
    }
    for source_name, expected_codes in expected_by_source.items():
        missing = sorted(set(expected_codes) - set(sources[source_name]))
        if missing:
            raise SnapshotValidationError(
                f"{source_name} is missing expected codes: {','.join(missing)}"
            )

    nonempty_required = {
        "fundFlow": fund_flow_codes,
        # mootdx excluded: optional source (IDC-IP block, see validate_source_completeness)
        "tencent": watch_codes,
        "baostock": watch_codes,
    }
    for source_name, expected_codes in nonempty_required.items():
        empty = [code for code in expected_codes if not sources[source_name].get(code)]
        if empty:
            raise SnapshotValidationError(
                f"{source_name} has empty payloads for: {','.join(sorted(empty))}"
            )

    northbound_values = [
        sources["northbound"].get(key) for key in ("hgt", "sgt", "total")
    ]
    if not any(
        isinstance(value, numbers.Real) and math.isfinite(float(value))
        for value in northbound_values
    ):
        raise SnapshotValidationError("northbound flow has no finite market value")
    try:
        validate_current_source_payloads(sources["concepts"], sources["insiders"], watch_codes)
    except ValueError as exc:
        raise SnapshotValidationError(str(exc)) from None


def requested_trade_date(argv):
    if argv:
        raise SnapshotUsageError(
            "historical date arguments are disabled because live sources cannot be "
            "validated as historical observations"
        )
    return None


def read_bin(path):
    return read_qlib_day_bin(path)


def qlib_generation():
    return qlib_summary.qlib_generation(QLIB)


def reference_complete_date(calendar, symbol):
    return qlib_summary.reference_complete_date(calendar, symbol, QLIB, load_qlib_features)


def qlib_market(date):
    return qlib_summary.qlib_market(date, qlib_root=QLIB,
        min_eligible=QLIB_MIN_ELIGIBLE, min_coverage=QLIB_MIN_COVERAGE,
        feature_loader=load_qlib_features, generation_reader=qlib_generation,
        reference_reader=reference_complete_date)

def akshare_market():
    import akshare as ak
    try:
        write_run_checkpoint(_supervised_run_id(), activeEndpoint="stock_board_industry_summary_ths")
        sectors = ak.stock_board_industry_summary_ths()
        sector_list = []
        for _, r in sectors.iterrows():
            sector_list.append({
                "name": json_scalar(r.get("板块")),
                "chg": json_scalar(r.get("涨跌幅")),
                "netInflow": json_scalar(r.get("净流入")),
                "upCount": json_scalar(r.get("上涨家数")),
                "downCount": json_scalar(r.get("下跌家数")),
                "leader": json_scalar(r.get("领涨股")),
            })
    except Exception as exc:
        raise AkshareMarketSourceError("stock_board_industry_summary_ths", exc) from None
    try:
        write_run_checkpoint(_supervised_run_id(), activeEndpoint="stock_market_activity_legu")
        activity = ak.stock_market_activity_legu()
        act = {}
        for _, r in activity.iterrows():
            act[str(r["item"])] = json_scalar(r["value"])
    except Exception as exc:
        raise AkshareMarketSourceError("stock_market_activity_legu", exc) from None
    write_run_checkpoint(_supervised_run_id(), activeEndpoint=None)
    return {"sectors": sector_list, "activity": act}


def akshare_lhb(date):
    import akshare as ak
    d = date.replace("-", "")
    df = ak.stock_lhb_detail_daily_sina(date=d)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "code": str(r.get("股票代码")),
            "name": json_scalar(r.get("股票名称")),
            "close": json_scalar(r.get("收盘价")),
            "chg": json_scalar(r.get("对应值")),
            "reason": json_scalar(r.get("指标"), allow_nan_as_null=True),
        })
    return {"date": date, "count": len(rows), "rows": rows}


def spot_market():
    import akshare as ak
    df = ak.stock_zh_a_spot()  # sina 全A现货(实时)
    up = down = flat = 0
    limit_up = []
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码"))
        name = r.get("名称")
        try:
            chg = json_scalar(float(r.get("涨跌幅")))
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"spot change is invalid for {code}: {r.get('涨跌幅')}"
            ) from exc
        if chg > 0.01:
            up += 1
        elif chg < -0.01:
            down += 1
        else:
            flat += 1
        lim = 30 if code.startswith("bj") else (20 if code[2:].startswith(("30", "68")) else 10)
        if chg >= lim - 0.5:
            limit_up.append({"sym": code, "name": name, "chg": chg})
        try:
            amt = json_scalar(float(r.get("成交额")))
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"spot amount is invalid for {code}: {r.get('成交额')}"
            ) from exc
        rows.append((code, name, chg, amt))
    rows.sort(key=lambda r: -r[2])
    return {
        "date": "current",
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitUp": limit_up[:30],
        "topGainers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "name": r[1], "amount": r[3]} for r in sorted(rows, key=lambda r: -r[3])[:10]],
    }


def fund_flow_sina(codes, days=5):
    import urllib.request, json
    results = {}
    for code in codes:
        pre = ("bj" if code.startswith(("92", "8")) else "sh" if code.startswith(("6", "9")) else "sz") + code
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                t = r.read().decode("utf-8", "ignore")
            arr = json.loads(t[t.index("["):t.rindex("]") + 1])
            results[code] = [{"date": x.get("opendate"), "close": x.get("trade"),
                              "net_amount": x.get("netamount"), "turnover": x.get("turnover")} for x in arr]
        except Exception as e:
            results[code] = {"error": str(e)}
    return results


def lhb_official(date):
    import urllib.request, json, ssl
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    out = {"date": date, "szse": [], "sse": ""}
    su = (f"https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
          f"&CATALOGID=1842_xxpl&TABKEY=tab1&txtStart={date}&txtEnd={date}&random=0.9")
    req = urllib.request.Request(su, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/disclosure/supervision/dealinfo/index.html"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            d = json.loads(r.read())
        for row in d[0].get("data", []):
            out["szse"].append({"code": row.get("zqdm"), "name": row.get("zqjc"), "amount": row.get("cjje"), "reason": row.get("plyy")})
    except Exception as e:
        out["szse_error"] = str(e)
    eu = (f"https://query.sse.com.cn/infodisplay/showTradePublicFile.do?"
          f"jsonCallBack=cb&isPagination=false&dateTx={date}")
    req = urllib.request.Request(eu, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/disclosure/diclosure/public/"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            t = r.read().decode("utf-8", "ignore")
        out["sse"] = "\n".join(json.loads(t[t.index("(") + 1:t.rindex(")")]).get("fileContents", []))
    except Exception as e:
        out["sse_error"] = str(e)
    return out


# Verified-reachable TongDaXin quote servers (TCP + protocol handshake tested).
# The library default picks an unverified entry from HQ_HOSTS, which caused
# intermittent post-close failures (2026-09-10/11 missing watch codes).
MOOTDX_SERVERS = (
    ("218.75.126.9", 7709),
    ("123.60.84.66", 7709),
    ("115.238.90.165", 7709),
)


def mootdx_quotes(codes):
    from mootdx.quotes import Quotes
    df = None
    last_error = None
    for server in MOOTDX_SERVERS:
        try:
            client = Quotes.factory(market="std", server=server, timeout=10)
            df = client.quotes(symbol=codes)
            if df is not None and not df.empty:
                break
        except Exception as exc:  # try the next verified server
            last_error = exc
            continue
    result = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            result[code] = {
                "price": json_scalar(r.get("price")),
                "bid1": json_scalar(r.get("bid1")),
                "ask1": json_scalar(r.get("ask1")),
                "bid_vol1": json_scalar(r.get("bid_vol1")),
                "ask_vol1": json_scalar(r.get("ask_vol1")),
            }
    if not result:
        # Empty for the whole request is a quotes-API failure, not "these codes
        # are missing from an otherwise populated book". Do not substitute another source.
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"mootdx quotes API returned empty{detail}")
    return result


def tencent_quote(codes):
    import urllib.request
    prefixed = []
    for c in codes:
        if c.startswith(("92", "8")):
            p = "bj" + c
        elif c.startswith(("6", "9", "5")):
            p = "sh" + c
        else:
            p = "sz" + c
        prefixed.append(p)
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    result = {}
    for line in data.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        result[key[2:]] = {
            "name": vals[1], "price": vals[3], "pe_ttm": vals[39], "pb": vals[46],
            "mcap_yi": vals[45], "float_mcap_yi": vals[44], "turnover_pct": vals[38],
            "limit_up": vals[47], "limit_down": vals[48],
        }
    return result


def baostock_valuation(codes, days=30):
    import baostock as bs
    import datetime
    import contextlib, io
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
    result = {}
    with contextlib.redirect_stdout(io.StringIO()):  # 抑制 baostock login/logout 的 stdout 输出
        bs.login()
        for code in codes:
            pre = ("sh." if code.startswith(("6", "9", "5")) else "sz." if code.startswith(("0", "3")) else "bj.") + code
            try:
                rs = bs.query_history_k_data_plus(pre, "date,code,close,peTTM,pbMRQ,psTTM,turn",
                                                  start_date=start, end_date=end, frequency="d", adjustflag="3")
                rows = []
                while rs.next():
                    r = rs.get_row_data()
                    rows.append({"date": r[0], "close": r[2], "pe_ttm": r[3], "pb": r[4], "ps": r[5], "turn": r[6]})
                result[code] = rows
            except Exception as e:
                result[code] = {"error": str(e)}
        bs.logout()
    return result


def cninfo_announcements(code, page_size=10, *, client=None):
    if client is not None:
        return client.announcements(code, page_size)
    with CninfoClient() as owned_client:
        return owned_client.announcements(code, page_size)


def collect_cninfo_announcements(codes):
    """Retry only a failed request; keep successful securities within this run."""
    result = {}
    with CninfoClient() as client:
        for code in codes:
            try:
                result[code] = cninfo_announcements(code, client=client)
            except CninfoError as exc:
                result[code] = {"error": exc.diagnostic}
    return result


def cls_telegraph(page_size=20):
    import requests, hashlib
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5", "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    r = requests.get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}",
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}, timeout=10)
    rolls = (r.json().get("data") or {}).get("roll_data") or []
    return [{"title": n.get("title", ""), "time": n.get("ctime")} for n in rolls]


def northbound_flow():
    import urllib.request, json
    req = urllib.request.Request("https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                                 headers={"User-Agent": "Mozilla/5.0", "Host": "data.hexin.cn",
                                          "Referer": "https://data.hexin.cn/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])
    hgt_close = hgt[-1] if hgt else None
    sgt_close = sgt[-1] if sgt else None
    hgt_open = hgt[0] if hgt else None
    sgt_open = sgt[0] if sgt else None
    return {"hgt": hgt_close, "sgt": sgt_close,
            "total": (hgt_close + sgt_close) if hgt and sgt else None,
            "hgt_trend": (hgt_close - hgt_open) if hgt else None,
            "sgt_trend": (sgt_close - sgt_open) if sgt else None}


def concept_blocks(code, *, client=None):
    if client is not None:
        return client.concepts(code)
    with ConceptClient() as owned_client:
        return owned_client.concepts(code)


def collect_concepts(codes):
    """Preserve successful securities while retaining explicit failures for the gate."""
    result = {}
    with ConceptClient() as client:
        for code in codes:
            try:
                result[code] = concept_blocks(code, client=client)
            except (ConceptError, SourceError) as exc:
                result[code] = {"error": exc.diagnostic}
    return result


def insider_transactions(code):
    """Compatibility function name: this is a current F10 excerpt, not trades."""
    with ShareholderClient() as client:
        return client.shareholders(code)


def collect_shareholders(codes):
    result = {}
    with ShareholderClient() as client:
        for code in codes:
            try:
                result[code] = client.shareholders(code)
            except SourceError as exc:
                result[code] = {"error": exc.diagnostic}
    return result


def hot_stocks():
    import urllib.request, json, datetime
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    rows = d.get("data") or []
    return [{"code": r.get("code"), "name": r.get("name"), "reason": r.get("reason", "")} for r in rows]


def global_markets():
    import akshare as ak
    result = {}
    errors = []
    for sym, name in [(".DJI", "道指"), (".IXIC", "纳指"), (".INX", "标普")]:
        try:
            df = ak.index_us_stock_sina(symbol=sym)
            close = df["close"].tolist()
            if len(close) >= 2:
                chg = (close[-1] - close[-2]) / close[-2] * 100
                result[name] = {"close": round(close[-1], 2), "chg": round(chg, 2)}
            else:
                errors.append(f"{name}: insufficient rows")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def _current_day_stage(run_id, expected_day):
    """Seal today's observations independently of Qlib, or reuse that seal."""
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
    if expected_day != today:
        raise SnapshotDeferredError("expected_trading_day_rolled_over", target=expected_day)
    _supervised_soft_deadline("current market stage")
    write_run_checkpoint(run_id, target=expected_day, activePhase="market_stage")
    stage = load_akshare_market_stage(expected_day)
    retry_identity = {
        field: os.environ.get("RESEARCH_DATA_RETRY_STAGE_" + suffix)
        for field, suffix in (("tradeDate", "TRADE_DATE"),
                              ("payloadSha256", "PAYLOAD_SHA256"),
                              ("recordSha256", "RECORD_SHA256"))
    }
    if any(value is not None for value in retry_identity.values()):
        if stage is None or any(stage.get(key) != value for key, value in retry_identity.items()):
            raise SnapshotDeferredError("same_day_retry_stage_missing_or_changed", target=expected_day)
    if stage is None:
        try:
            stage = capture_current_akshare_market(expected_day=expected_day)
        except SnapshotDeferredError as exc:
            exc.target = expected_day
            raise
    else:
        stage = {**stage, "status": "reused"}
    # Historical seals remain readable, but only an observation actually made
    # on this trading day can feed a live full assembly or same-day retry.
    for value in stage["collectionWindow"].values():
        observed = datetime.datetime.fromisoformat(value).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        if observed != expected_day:
            raise SnapshotValidationError("current_stage_observation_day_mismatch")
    validate_daily_source_dates(expected_day, stage["akshare"])
    write_run_checkpoint(run_id, activePhase="qlib_readiness",
                         stageCandidateTradeDate=expected_day,
                         stageCandidatePayloadSha256=stage["payloadSha256"],
                         stageCandidateRecordSha256=stage["recordSha256"])
    return stage


def run_snapshot(run_id, started_monotonic, expected_day=None):
    date = requested_trade_date(sys.argv[1:])
    # Live entry supplies an independently proven current trading day. Keep
    # the immutable market evidence before any Qlib read/coverage/date gate.
    current_stage = (_current_day_stage(run_id, expected_day)
                     if expected_day is not None else None)
    q = qlib_market(date)
    qlib_tag = (q.get("generation") or {}).get("tag")
    if expected_day is not None and q["date"] != expected_day:
        raise SnapshotDeferredError("qlib_not_current_for_expected_trading_day", target=expected_day,
                                    qlib_tag=qlib_tag, akshare_stage={
                                        key: current_stage[key] for key in
                                        ("status", "tradeDate", "payloadSha256", "recordSha256")})
    prior = existing_snapshot(q["date"])
    write_run_checkpoint(run_id, target=q["date"], qlibTag=qlib_tag,
                         preexistingRowSha256=prior["payloadSha256"] if prior else None)
    target_stage = current_stage
    if prior is None and target_stage is None:
        target_stage = load_akshare_market_stage(q["date"])

    staging_warning = None
    staging_error = None
    if current_stage is None:
        try:
            current_stage = capture_current_akshare_market()
        except (SnapshotDeferredError, SnapshotValidationError) as exc:
            staging_error = exc
            staging_warning = f"{type(exc).__name__}: {exc}"
    if current_stage is None:
        current_stage_summary = {
            "status": "unavailable",
            "reason": staging_warning,
        }
        if isinstance(staging_error, SnapshotDeferredError) and staging_error.source_failure:
            current_stage_summary["sourceFailure"] = staging_error.source_failure
    else:
        current_stage_summary = {
            "status": current_stage["status"],
            "tradeDate": current_stage["tradeDate"],
            "payloadSha256": current_stage["payloadSha256"],
        }

    if prior is not None:
        if current_stage is None:
            raise SnapshotDeferredError("current_market_stage_unavailable_even_with_existing_snapshot",
                                        target=q["date"], qlib_tag=qlib_tag,
                                        source_failure=getattr(staging_error, "source_failure", None))
        captured_date = None
        if current_stage is not None:
            try:
                captured_date = datetime.date.fromisoformat(current_stage["tradeDate"])
            except (KeyError, TypeError, ValueError):
                captured_date = None
        try:
            qlib_day = datetime.date.fromisoformat(q["date"])
        except (KeyError, TypeError, ValueError):
            qlib_day = None
        if captured_date is not None and qlib_day is not None and captured_date > qlib_day:
            raise SnapshotDeferredError(
                "qlib calendar behind observed market stage: "
                f"qlib={q['date']}, captured={current_stage['tradeDate']}",
                target=q["date"], qlib_tag=qlib_tag,
            )
        status_record = {
            "runId": run_id,
            "status": "skipped_existing",
            "target": q["date"],
            "qlibTag": q["generation"]["tag"],
            "coverage": q["coverage"],
            "generatedAt": prior["generatedAt"],
            "payloadSha256": prior["payloadSha256"],
            "akshareStage": current_stage_summary,
            "durationSeconds": round(time.monotonic() - started_monotonic, 3),
        }
        if staging_warning:
            status_record["warnings"] = [staging_warning]
        emit_status(status_record)
        return 0

    if target_stage is None and current_stage is not None:
        target_stage = load_akshare_market_stage(q["date"])
    if target_stage is None:
        captured = (
            current_stage["tradeDate"] if current_stage is not None else "unavailable"
        )
        warning_suffix = (
            f"; current staging unavailable: {staging_warning}"
            if staging_warning else ""
        )
        raise SnapshotDeferredError(
            "waiting for immutable AkShare market stage: "
            f"qlib={q['date']}, captured={captured}{warning_suffix}",
            target=q["date"], qlib_tag=qlib_tag,
            source_failure=(staging_error.source_failure
                            if isinstance(staging_error, SnapshotDeferredError) else None),
        )
    a = target_stage["akshare"]
    activity_date = validate_daily_source_dates(q["date"], a)
    collection_started_at = datetime.datetime.now().astimezone().isoformat()
    try:
        lhb = collect_source(akshare_lhb, q["date"])
    except Exception as e:
        lhb = {"date": q["date"], "count": 0, "rows": [], "error": str(e)}
    try:
        spot = collect_source(spot_market)
    except Exception as e:
        spot = {"date": "current", "error": str(e)}
    # 资金流: watchlist + 涨停股
    try:
        with open(
            os.path.expanduser("~/research-data/config/stocks.json"), encoding="utf-8"
        ) as config_file:
            cfg = json.load(config_file)
        watch_codes = [s["code"] for s in cfg.get("corePool", [])]
    except Exception as exc:
        raise SnapshotValidationError(f"stock config unavailable: {exc}") from exc
    if not watch_codes:
        raise SnapshotValidationError("stock config corePool is empty")
    limit_codes = [x["sym"][2:] for x in spot.get("limitUp", [])[:10]]
    ff_codes = list(dict.fromkeys(watch_codes + limit_codes))
    try:
        ff = collect_source(fund_flow_sina, ff_codes, days=5)
    except Exception as e:
        ff = {"error": str(e)}
    try:
        lhb_off = collect_source(lhb_official, q["date"])
    except Exception as e:
        lhb_off = {"date": q["date"], "error": str(e)}
    try:
        mq = collect_source(mootdx_quotes, watch_codes)
    except Exception as e:
        mq = {"error": str(e)}
    try:
        tq = collect_source(tencent_quote, watch_codes)
    except Exception as e:
        tq = {"error": str(e)}
    try:
        bv = collect_source(baostock_valuation, watch_codes, days=30)
    except Exception as e:
        bv = {"error": str(e)}
    anns = collect_source(collect_cninfo_announcements, watch_codes, _allow_error_payload=True)
    try:
        news = collect_source(cls_telegraph, 20)
    except Exception as e:
        news = {"error": str(e)}
    try:
        nb = collect_source(northbound_flow)
    except Exception as e:
        nb = {"error": str(e)}
    concepts = collect_source(collect_concepts, watch_codes, _allow_error_payload=True)
    insiders = collect_source(collect_shareholders, watch_codes, _allow_error_payload=True)
    try:
        hot = collect_source(hot_stocks)
    except Exception as e:
        hot = {"error": str(e)}
    try:
        global_mkts = collect_source(global_markets)
    except Exception as e:
        global_mkts = {"error": str(e)}
    sources = {
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
        "fundFlow": ff,
        "lhbOfficial": lhb_off,
        "mootdx": mq,
        "tencent": tq,
        "baostock": bv,
        "announcements": anns,
        "news": news,
        "northbound": nb,
        "concepts": concepts,
        "insiders": insiders,
        "hotStocks": hot,
        "globalMarkets": global_mkts,
    }
    try:
        validate_source_completeness(sources, watch_codes, ff_codes)
    except SnapshotValidationError as exc:
        exc.details.update({"target": q["date"], "qlibTag": q["generation"]["tag"]})
        raise
    collection_completed_at = datetime.datetime.now().astimezone().isoformat()
    observation_day = datetime.datetime.fromisoformat(collection_completed_at).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
    assembly_kind = "same_day_observation" if q["date"] == observation_day else "delayed_observation"
    snapshot = {
        "date": q["date"],
        "generatedAt": collection_completed_at,
        "assemblyKind": assembly_kind,
        "sourceAsOf": {
            "collectionWindow": {
                "startedAt": collection_started_at,
                "completedAt": collection_completed_at,
            },
            "qlib": {"status": "ok", "kind": "trade_date", "value": q["date"]},
            "akshareActivity": {
                "status": "ok",
                "kind": "trade_date",
                "value": activity_date,
                "provenance": "immutable_stage",
                "payloadSha256": target_stage["payloadSha256"],
                "window": target_stage["collectionWindow"],
            },
            "akshareStage": {
                "status": "ok",
                "kind": "immutable_stage",
                "tradeDate": activity_date,
                "payloadSha256": target_stage["payloadSha256"],
                "recordSha256": target_stage["recordSha256"],
                "window": target_stage["collectionWindow"],
                "sources": ["akshareActivity", "akshareSectors"],
            },
            "lhb": {"status": "ok", "kind": "trade_date", "value": q["date"]},
            "mootdx": {
                "status": "degraded" if (isinstance(mq, dict) and mq.get("error")) else "ok",
                "kind": "observed_at",
                "optional": True,
                "note": "TDX quote servers block this host's IDC egress IP; failure does not reject the snapshot",
            },
            "lhbOfficial": {
                "status": "ok", "kind": "trade_date", "value": q["date"],
            },
            "announcements": {
                "status": "ok",
                "dateKind": "source_disclosure_date",
                "observedAtLocation": "announcements[code][].observedAt",
                "dateIsFirstObservedTime": False,
                "nextDayPolicy": "one_calendar_day_with_verified_midnight_label_and_attachment_identity",
            },
            "concepts": {"provider": "ths_f10", "kind": "observed_at",
                         "evidenceLocation": "concepts[code].evidence",
                         "taxonomy": "ths_f10_current_concepts",
                         "replacesProvider": "baidu_related_blocks"},
            "insiders": {"provider": "ths_f10", "kind": "observed_at",
                         "evidenceLocation": "insiders[code].evidence",
                         "scope": "current_shareholder_excerpt_not_insider_trades",
                         "replacesProvider": "mootdx_F10"},
            "liveAndRollingSources": {
                "status": "ok",
                "kind": "observed_at",
                "window": {
                    "startedAt": collection_started_at,
                    "completedAt": collection_completed_at,
                },
                "sources": [
                    "akshareSectors", "spot", "fundFlow", "mootdx", "tencent",
                    "baostock", "announcements", "news", "northbound", "concepts",
                    "insiders", "hotStocks", "globalMarkets",
                ],
            },
        },
        "qlib": q,
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
        "fundFlow": ff,
        "lhbOfficial": lhb_off,
        "mootdx": mq,
        "tencent": tq,
        "baostock": bv,
        "announcements": anns,
        "news": news,
        "northbound": nb,
        "concepts": concepts,
        "insiders": insiders,
        "hotStocks": hot,
        "globalMarkets": global_mkts,
    }
    write_run_checkpoint(run_id, snapshotCandidateSha256=_payload_sha256(_snapshot_payload(snapshot)),
                         snapshotCandidateTradeDate=q["date"])
    stored = (store_snapshot(snapshot, expected_day=expected_day)
              if expected_day is not None else store_snapshot(snapshot))
    status_record = {
        "runId": run_id,
        "status": stored["status"],
        "target": q["date"],
        "qlibTag": q["generation"]["tag"],
        "generatedAt": snapshot["generatedAt"],
        "assemblyKind": assembly_kind,
        "sourceDates": {
            "qlib": q["date"],
            "akshareActivity": activity_date,
        },
        "coverage": q["coverage"],
        "payloadSha256": stored["payloadSha256"],
        "quickCheck": stored.get("quickCheck"),
        "akshareStage": {
            **current_stage_summary,
            "usedTradeDate": target_stage["tradeDate"],
            "usedPayloadSha256": target_stage["payloadSha256"],
        },
        "durationSeconds": round(time.monotonic() - started_monotonic, 3),
    }
    if staging_warning:
        status_record["warnings"] = [staging_warning]
    emit_status(status_record)
    return 0


def run_current_day(run_id, started_monotonic):
    """Mandatory independent calendar gate for every live writer entry."""
    requested_trade_date(sys.argv[1:])
    import snapshot_contract
    policy_path = os.environ.get("RESEARCH_DATA_CORE_POLICY_PATH",
        os.path.join(os.path.dirname(__file__), "config", "snapshot-contract.json"))
    policy = snapshot_contract.read_policy(policy_path)
    core_db_path = os.environ.get("RESEARCH_DATA_CORE_DB_PATH",
        os.path.join(os.path.dirname(__file__), "core-market.db"))
    snapshot_contract.assert_core_history(policy, core_db_path)
    if snapshot_contract.select(policy, datetime.datetime.now(snapshot_contract.CST)) != snapshot_contract.LEGACY:
        raise SnapshotDeferredError("full_contract_not_active")
    from calendar_evidence import load_calendar_evidence
    from core_contract import validate_calendar_evidence
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    root = os.environ.get("RESEARCH_DATA_CORE_CALENDAR_PROJECT_ROOT", "/home/ubuntu/AStock_AI_System")
    try:
        evidence = load_calendar_evidence(root, now)
        calendar = validate_calendar_evidence(evidence, now)
    except (OSError, ValueError, TypeError, KeyError, SnapshotDeferredError):
        raise SnapshotDeferredError("independent_expected_day_unavailable") from None
    if calendar["status"] == "closed":
        emit_status({"runId": run_id, "status": "skipped_non_trading", "target": calendar["previousTradingDate"],
                     "calendarEvidence": evidence, "durationSeconds": round(time.monotonic() - started_monotonic, 3)})
        return 0
    return run_snapshot(run_id, started_monotonic, expected_day=calendar["targetDate"])


def main(run_id=None, started_monotonic=None):
    run_id = run_id or uuid.uuid4().hex
    started_monotonic = started_monotonic or time.monotonic()
    if _supervised_run_id() is not None:
        _verify_supervisor_lock()
        return run_current_day(run_id, started_monotonic)
    with snapshot_run_lock():
        return run_current_day(run_id, started_monotonic)


def emit_deferred_status(run_id, started_monotonic, error, stream=None):
    """Persist only a target established by independent calendar or Qlib evidence."""
    record = {
        "runId": run_id,
        "status": "deferred",
        "reason": str(error),
        "durationSeconds": round(time.monotonic() - started_monotonic, 3),
    }
    if error.target is not None:
        record["target"] = error.target
    if error.qlib_tag is not None:
        record["qlibTag"] = error.qlib_tag
    if error.source_failure is not None:
        record["sourceFailure"] = error.source_failure
    if error.akshare_stage is not None:
        record["akshareStage"] = error.akshare_stage
    emit_status(record, stream=stream or sys.stderr)


if __name__ == "__main__":
    if _supervised_run_id() is None:
        # All CLI invocations use the bounded entry. No opt-out flag is offered.
        from snapshot_supervisor import run_supervised
        _record, _code = run_supervised(worker_args=sys.argv[1:])
        print(json.dumps(_record, ensure_ascii=False))
        raise SystemExit(_code)
    _run_id = _supervised_run_id()
    _started_monotonic = time.monotonic()
    try:
        _await_supervisor_gate()
        write_run_checkpoint(_run_id, timestamp=datetime.datetime.now().astimezone().isoformat())
        raise SystemExit(main(_run_id, _started_monotonic))
    except SnapshotUsageError as exc:
        emit_status({
            "runId": _run_id,
            "status": "usage_error",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(64)
    except SnapshotTemporaryError as exc:
        emit_status({
            "runId": _run_id,
            "status": "temporary_failure",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(75)
    except SnapshotDeferredError as exc:
        emit_deferred_status(_run_id, _started_monotonic, exc)
        raise SystemExit(SNAPSHOT_DEFERRED_EXIT)
    except (QlibDateError, SnapshotValidationError) as exc:
        emit_status({
            "runId": _run_id,
            "status": "rejected",
            "reason": str(exc),
            **getattr(exc, "details", {}),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        emit_status({
            "runId": _run_id,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(1)
