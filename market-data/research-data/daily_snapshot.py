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
import sqlite3
import stat
import sys
import syslog
import time
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from qlib_io import load_qlib_features, read_qlib_day_bin
from cninfo_source import CninfoClient, CninfoError, safe_diagnostic
from concept_source import ConceptError, safe_concept_diagnostic
from ths_concept_source import ThsConceptClient as ConceptClient
from ths_source import SourceError, safe_source_diagnostic
from shareholder_source import ShareholderClient
from source_contract import validate_current_source_payloads
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
QLIB_MIN_COVERAGE = 0.999
SQLITE_TIMEOUT = 30


class QlibDateError(ValueError):
    """The requested trade date is not present in the local Qlib calendar."""


class SnapshotValidationError(RuntimeError):
    """The candidate snapshot is unsafe to persist."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


class SnapshotUsageError(RuntimeError):
    """The command-line invocation is unsupported."""


class SnapshotTemporaryError(RuntimeError):
    """A transient lock or database-busy condition blocked the run."""


class SnapshotDeferredError(RuntimeError):
    """Required same-trade-date source material has not arrived yet."""


def _snapshot_payload(snapshot):
    try:
        return json.dumps(
            snapshot,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"payload is not strict JSON: {exc}") from exc


def _payload_sha256(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def write_last_run(record, state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "last-run.json")
    temporary_path = f"{path}.{os.getpid()}.tmp"
    payload = _snapshot_payload(record) + "\n"
    with open(temporary_path, "w", encoding="utf-8") as state_file:
        state_file.write(payload)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temporary_path, path)


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


@contextmanager
def snapshot_run_lock(state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    lock_path = os.path.join(state_dir, "snapshot.lock")
    lock_file = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise SnapshotTemporaryError("another snapshot run holds the lock") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


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
        payload = json.loads(row[1], parse_constant=_reject_json_constant)
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
    errors = source_error_paths(payload)
    if errors:
        raise SnapshotValidationError(
            f"existing snapshot {date} contains source errors: {', '.join(errors[:5])}"
        )
    return {
        "generatedAt": row[0],
        "payloadSha256": _payload_sha256(row[1]),
    }


def store_snapshot(snapshot, db_path=None):
    """Persist one immutable snapshot, allowing only an exact-payload retry."""
    db_path = db_path or DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    payload = _snapshot_payload(snapshot)
    payload_sha256 = _payload_sha256(payload)
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT) as conn:
            conn.execute("BEGIN IMMEDIATE")
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


def source_error_paths(value, path=""):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (key == "error" or key.endswith("_error")) and child:
                errors.append(child_path)
            else:
                errors.extend(source_error_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            errors.extend(source_error_paths(child, child_path))
    return errors


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


def collect_source(func, *args, attempts=2, **kwargs):
    """Retry read-only source collection on exceptions or explicit error payloads."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = func(*args, **kwargs)
            errors = source_error_paths(result)
            if errors:
                raise RuntimeError(f"error payloads: {', '.join(errors[:5])}")
            return result
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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
        document = json.loads(raw, parse_constant=_reject_json_constant)
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
    akshare_payload, collection_started_at, collection_completed_at, stage_dir=None
):
    trade_date = validate_akshare_market_payload(akshare_payload)
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


def capture_current_akshare_market(stage_dir=None):
    collection_started_at = datetime.datetime.now().astimezone().isoformat()
    try:
        akshare_payload = collect_source(akshare_market)
    except Exception as exc:
        raise SnapshotDeferredError(
            f"current AkShare market staging failed: {type(exc).__name__}: {exc}"
        ) from exc
    collection_completed_at = datetime.datetime.now().astimezone().isoformat()
    return seal_akshare_market_stage(
        akshare_payload,
        collection_started_at,
        collection_completed_at,
        stage_dir,
    )


def validate_source_completeness(sources, watch_codes, fund_flow_codes):
    errors = source_error_paths(sources)
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
        "mootdx": watch_codes,
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
        "mootdx": watch_codes,
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


def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith("30") or code.startswith("68"):
        return 0.20
    return 0.10


def qlib_generation():
    tag_path = os.path.join(os.path.dirname(QLIB), ".tag")
    try:
        with open(tag_path, encoding="utf-8") as tag_file:
            tag = tag_file.read().strip()
        inode = os.stat(QLIB).st_ino
    except (FileNotFoundError, OSError) as exc:
        raise SnapshotValidationError(f"Qlib generation marker unavailable: {exc}") from exc
    if not tag:
        raise SnapshotValidationError("Qlib generation marker is empty")
    return {"tag": tag, "inode": inode}


def reference_complete_date(calendar, symbol):
    try:
        start, features = load_qlib_features(
            QLIB, symbol, ("close", "high", "low", "amount")
        )
    except (FileNotFoundError, ValueError) as exc:
        raise SnapshotValidationError(
            f"Qlib reference features unavailable for {symbol}: {exc}"
        ) from exc
    usable = min(len(values) for values in features.values())
    close = features["close"]
    for index in range(usable - 1, 0, -1):
        values = (
            close[index - 1],
            close[index],
            features["high"][index],
            features["low"][index],
            features["amount"][index],
        )
        calendar_index = start + index
        if (
            0 <= calendar_index < len(calendar)
            and all(math.isfinite(float(value)) for value in values)
        ):
            return calendar[calendar_index]
    raise SnapshotValidationError(
        f"Qlib reference has no complete finite date: {symbol}"
    )


def qlib_market(date):
    generation_before = qlib_generation()
    with open(os.path.join(QLIB, "calendars/day.txt"), encoding="utf-8") as calendar_file:
        cal = [line.strip() for line in calendar_file if line.strip()]
    if date is None:
        # 日历可能领先特征数据；四只参考股的四字段必须共同对齐。
        ref_syms = ["sh600000", "sz000001", "sh600519", "sz300750"]
        reference_dates = {
            symbol: reference_complete_date(cal, symbol) for symbol in ref_syms
        }
        unique_dates = sorted(set(reference_dates.values()))
        if len(unique_dates) != 1:
            raise SnapshotValidationError(
                f"Qlib reference dates disagree: {reference_dates}"
            )
        date = unique_dates[0]
    elif date not in cal:
        raise QlibDateError(f"requested date is not in Qlib calendar: {date}")
    di = cal.index(date)
    insts = []
    with open(os.path.join(QLIB, "instruments/all.txt"), encoding="utf-8") as instruments_file:
        for line in instruments_file:
            p = line.split()
            if len(p) >= 3:
                insts.append((p[0], p[1], p[2]))
    up = down = flat = invalid = eligible = 0
    limit_up, limit_down, rows = [], [], []
    for sym, sd, ed in insts:
        # A prior close is required to calculate the daily change, so first-day
        # listings are intentionally excluded from the eligible population.
        if not (sd < date <= ed):
            continue
        eligible += 1
        sym = sym.lower()  # all.txt 大写, feature 目录小写
        try:
            s, features = load_qlib_features(QLIB, sym, ("close", "high", "low", "amount"))
        except (FileNotFoundError, ValueError) as exc:
            raise SnapshotValidationError(
                f"Qlib feature structure unavailable for {sym}: {exc}"
            ) from exc
        close = features["close"]
        high = features["high"]
        low = features["low"]
        amount = features["amount"]
        li = di - s
        if li < 1 or any(li >= len(values) for values in features.values()):
            raise SnapshotValidationError(
                f"Qlib feature index is out of range for {sym} on {date}"
            )
        required_values = (close[li - 1], close[li], high[li], low[li], amount[li])
        if not all(math.isfinite(float(value)) for value in required_values):
            invalid += 1
            continue
        chg = close[li] / close[li - 1] - 1
        if chg > 1e-4:
            up += 1
        elif chg < -1e-4:
            down += 1
        else:
            flat += 1
        lim = limit_pct(sym)
        lp = round(close[li - 1] * (1 + lim), 2)
        if abs(close[li] - lp) < 0.005 and abs(high[li] - close[li]) < 0.005:
            streak, j = 0, li
            while j >= 1:
                lpj = round(close[j - 1] * (1 + lim), 2)
                if abs(close[j] - lpj) < 0.005 and abs(high[j] - close[j]) < 0.005:
                    streak += 1
                    j -= 1
                else:
                    break
            limit_up.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "streak": streak,
                "close": round(float(close[li]), 2),
            })
        lpd = round(close[li - 1] * (1 - lim), 2)
        if abs(close[li] - lpd) < 0.005 and abs(low[li] - close[li]) < 0.005:
            limit_down.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "close": round(float(close[li]), 2),
            })
        rows.append((sym, float(chg), float(amount[li]), float(close[li])))
    coverage = (len(rows) / eligible) if eligible else 0.0
    if eligible < QLIB_MIN_ELIGIBLE or coverage < QLIB_MIN_COVERAGE:
        raise SnapshotValidationError(
            "Qlib market coverage is incomplete: "
            f"date={date}, valid={len(rows)}, eligible={eligible}, "
            f"invalid={invalid}, coverage={coverage:.6f}"
        )
    generation_after = qlib_generation()
    if generation_before != generation_after:
        raise SnapshotValidationError(
            "Qlib generation changed during snapshot scan: "
            f"before={generation_before}, after={generation_after}"
        )
    rows.sort(key=lambda r: -r[1])
    return {
        "date": date,
        "generation": generation_after,
        "coverage": {
            "valid": len(rows),
            "eligible": eligible,
            "invalid": invalid,
            "ratio": round(coverage, 6),
        },
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitDownCount": len(limit_down),
        "limitUp": sorted(limit_up, key=lambda x: -x["streak"])[:30],
        "limitDown": limit_down[:10],
        "topGainers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "amount": round(r[2], 0)} for r in sorted(rows, key=lambda r: -r[2])[:10]],
    }


def akshare_market():
    import akshare as ak
    sectors = ak.stock_board_industry_summary_ths()
    activity = ak.stock_market_activity_legu()
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
    act = {}
    for _, r in activity.iterrows():
        act[str(r["item"])] = json_scalar(r["value"])
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


def mootdx_quotes(codes):
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    df = client.quotes(symbol=codes)
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
        raise RuntimeError("mootdx quotes API returned empty")
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


def run_snapshot(run_id, started_monotonic):
    date = requested_trade_date(sys.argv[1:])
    q = qlib_market(date)
    prior = existing_snapshot(q["date"])
    target_stage = None
    if prior is None:
        target_stage = load_akshare_market_stage(q["date"])

    current_stage = None
    staging_warning = None
    try:
        current_stage = capture_current_akshare_market()
    except (SnapshotDeferredError, SnapshotValidationError) as exc:
        staging_warning = f"{type(exc).__name__}: {exc}"
    if current_stage is None:
        current_stage_summary = {
            "status": "unavailable",
            "reason": staging_warning,
        }
    else:
        current_stage_summary = {
            "status": current_stage["status"],
            "tradeDate": current_stage["tradeDate"],
            "payloadSha256": current_stage["payloadSha256"],
        }

    if prior is not None:
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
                f"qlib={q['date']}, captured={current_stage['tradeDate']}"
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
            f"qlib={q['date']}, captured={captured}{warning_suffix}"
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
    anns = collect_cninfo_announcements(watch_codes)
    try:
        news = collect_source(cls_telegraph, 20)
    except Exception as e:
        news = {"error": str(e)}
    try:
        nb = collect_source(northbound_flow)
    except Exception as e:
        nb = {"error": str(e)}
    concepts = collect_concepts(watch_codes)
    insiders = collect_shareholders(watch_codes)
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
    stored = store_snapshot(snapshot)
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


def main(run_id=None, started_monotonic=None):
    run_id = run_id or uuid.uuid4().hex
    started_monotonic = started_monotonic or time.monotonic()
    with snapshot_run_lock():
        return run_snapshot(run_id, started_monotonic)


if __name__ == "__main__":
    _run_id = uuid.uuid4().hex
    _started_monotonic = time.monotonic()
    try:
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
        emit_status({
            "runId": _run_id,
            "status": "deferred",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
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
