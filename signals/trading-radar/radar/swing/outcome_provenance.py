"""Opt-in, forward-only evidence of inputs actually consumed by outcomes.

This is a sidecar, not a price provider, performance fix, backup, or replay API.
It never opens the business DB or obtains quotes. Storage failure cannot change
the evaluator's business result. Cross-horizon price changes are evidence only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
HORIZONS = (1, 3, 5, 10, 20)
MAX_RECORD = 65_536
MAX_FILES = 50_000
MAX_DIRECTORY = 256 * 1024 * 1024
MIN_FREE = 64 * 1024 * 1024
SOURCE_PATHS = (
    "radar/swing/outcome_evaluator.py", "radar/swing/outcome_provenance.py",
    "radar/swing/store.py", "scripts/evaluate_swing_outcomes.py",
    "radar/swing/evidence_store.py", "radar/data/unified_history.py",
    "radar/data/market_data_v2.py", "radar/data/kline_sources.py",
    "radar/data/ohlcv_parquet_store.py",
)
OUTCOME_FIELDS = (
    "decision_id", "horizon_sessions", "evaluation_as_of", "entry_date",
    "entry_open", "entry_fill", "exit_date", "exit_close", "exit_fill",
    "benchmark_code", "benchmark_return", "gap_return", "gross_return",
    "net_return", "excess_return", "mfe", "mae", "fees_return",
    "slippage_return", "fill_status", "fill_reason",
)


class EvidenceError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise EvidenceError("timestamp_missing")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise EvidenceError("timezone_missing")
    return result.astimezone(timezone.utc)


def iso_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise EvidenceError("invalid_date")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise EvidenceError("invalid_identifier")
    return value


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise EvidenceError("invalid_number")
    return value


def source_lineage(frame):
    raw = getattr(frame, "attrs", {}).get("radar_lineage", {}) if frame is not None else {}
    if not isinstance(raw, dict):
        raw = {}
    result = {}
    # No arbitrary attrs, headers, URLs, errors, provider credentials or titles.
    for key in ("source", "pipeline", "adjustment", "schema_version"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
            result[key] = value
    result["normalization_error_present"] = bool(raw.get("normalize_error"))
    result["validation_errors_present"] = bool(raw.get("validate_errors"))
    result["status"] = "declared_unverified" if all(result.get(k) not in (None, "unknown") for k in ("source", "adjustment")) and not result["normalization_error_present"] and not result["validation_errors_present"] else "unknown"
    return result


def bars(frame, start, end):
    if frame is None or getattr(frame, "empty", True):
        return []
    result, seen = [], set()
    for row in frame.itertuples(index=False):
        day = iso_day(row.date)
        if not start <= day <= end:
            continue
        if day in seen:
            raise EvidenceError("duplicate_bar")
        seen.add(day)
        record = {"date": day}
        for field in ("open", "high", "low", "close"):
            record[field] = finite(float(getattr(row, field)))
        if min(record[k] for k in ("open", "high", "low", "close")) <= 0 or not record["low"] <= min(record["open"], record["close"]) <= max(record["open"], record["close"]) <= record["high"]:
            raise EvidenceError("invalid_ohlc")
        result.append(record)
    if len(result) > 20:
        raise EvidenceError("path_budget")
    return sorted(result, key=lambda r: r["date"])


def file_hash(path):
    if path.is_symlink():
        raise EvidenceError("source_symlink")
    with path.open("rb") as handle:
        data = handle.read(2_000_001)
    if len(data) > 2_000_000:
        raise EvidenceError("source_budget")
    return hashlib.sha256(data).hexdigest()


def observed_manifest(repo, config_path):
    return {"source_sha256": {p: file_hash(repo / p) for p in SOURCE_PATHS},
            "config_file_sha256": file_hash(config_path),
            "python_version": sys.version.split()[0],
            "scope": "outcome_process_observed_source_files; not historical decision-code attestation"}


class UnavailableRecorder:
    def __init__(self, cutover=None):
        self.cutover = cutover

    def capture(self, **kwargs):
        if self.cutover is not None:
            try:
                if timestamp(kwargs["decision"]["decision_at"]) < self.cutover:
                    return {"status": "skipped_pre_cutover", "basis_diff": False, "provenance_unknown": False}
            except (KeyError, TypeError, ValueError):
                pass
        return {"status": "unavailable", "basis_diff": False, "provenance_unknown": True}


class OutcomeRecorder:
    def __init__(self, directory, *, cutover, manifest, clock=None, validate_sources=None):
        self.directory = Path(directory)
        self.cutover = timestamp(cutover)
        self.manifest = json.loads(canonical(manifest))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.validate_sources = validate_sources
        # No mkdir/open in construction or before the forward-only gate.

    def _open_directory(self):
        # Fixed repository data path in production; refuse symlink components.
        absolute = self.directory.absolute()
        for part in (absolute, *absolute.parents):
            if part.is_symlink():
                raise EvidenceError("evidence_path_symlink")
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(absolute, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _read(self, directory_fd, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise EvidenceError("non_regular_evidence")
            raw = handle.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise EvidenceError("record_budget")
        record = json.loads(raw)
        if record.get("schema_version") != "swing-outcome-input-v1" or record.get("payload_sha256") != digest(record["payload"]):
            raise EvidenceError("corrupt_existing_evidence")
        if record.get("record_sha256") != digest({k: v for k, v in record.items() if k != "record_sha256"}):
            raise EvidenceError("corrupt_evidence_envelope")
        return record

    def _basis_differences(self, fd, payload):
        changes = []
        for h in HORIZONS:
            if h == payload["outcome"]["horizon_sessions"]:
                continue
            old = self._read(fd, f"{payload['decision']['decision_id']}-{h}.json")
            if old is None:
                continue
            old = old["payload"]
            fields = []
            for name in ("target", "benchmark"):
                before = {r["date"]: r for r in old["paths"][name]}
                after = {r["date"]: r for r in payload["paths"][name]}
                shared_end = min(old["session_window"]["exit"], payload["session_window"]["exit"])
                if {d for d in before if d <= shared_end} != {d for d in after if d <= shared_end}:
                    fields.append(name + "_shared_dates")
                if any(before[d] != after[d] for d in before.keys() & after.keys()):
                    fields.append(name + "_overlap_ohlc")
                if old["lineage"][name] != payload["lineage"][name]:
                    fields.append(name + "_lineage")
            for field in ("entry_date", "entry_open", "entry_fill", "benchmark_code"):
                if old["outcome"].get(field) != payload["outcome"].get(field):
                    fields.append(field)
            if old["effective_settings"] != payload["effective_settings"]:
                fields.append("effective_settings")
            if old["reference_input"] != payload["reference_input"]:
                fields.append("reference_input")
            if old["process_version"] != payload["process_version"]:
                fields.append("process_version")
            if fields:
                changes.append({"against_horizon": h, "fields": fields})
        return changes

    def capture(self, *, decision, outcome, target_frame, benchmark, settings, as_of):
        now = self.clock()
        if now.tzinfo is None:
            raise EvidenceError("clock_timezone")
        decision_at = timestamp(decision.get("decision_at"))
        if decision_at < self.cutover:
            return {"status": "skipped_pre_cutover", "basis_diff": False, "provenance_unknown": False}
        decision_day = iso_day(decision["decision_date"])
        if decision_at > now or decision_at.astimezone(CST).date().isoformat() != decision_day or iso_day(as_of) != now.astimezone(CST).date().isoformat():
            raise EvidenceError("not_current_natural_day")
        if decision.get("validation_split") != "live_oos":
            raise EvidenceError("not_live_oos")
        identity = identifier(decision["decision_id"])
        horizon = outcome["horizon_sessions"]
        if type(horizon) is not int or horizon not in HORIZONS or outcome["decision_id"] != identity or outcome["evaluation_as_of"] != as_of:
            raise EvidenceError("outcome_identity_mismatch")
        start = iso_day(outcome["entry_date"])
        if start <= decision_day:
            raise EvidenceError("entry_before_decision")
        sessions = [iso_day(d) for d in benchmark["date"] if iso_day(d) > decision_day]
        if len(sessions) < horizon or sessions[0] != start:
            raise EvidenceError("session_mismatch")
        end = sessions[horizon - 1]
        if end > as_of or outcome.get("exit_date", end) != end:
            raise EvidenceError("exit_mismatch")
        if self.validate_sources is not None and self.validate_sources() != self.manifest:
            raise EvidenceError("source_changed_during_process")
        raw = decision.get("raw_signals_json")
        raw = json.loads(raw) if isinstance(raw, str) else decision.get("raw_signals", [])
        keys, identity_unknown = [], False
        if not isinstance(raw, list) or len(raw) != decision.get("raw_trigger_count"):
            keys, identity_unknown = [], True
        else:
            for item in raw:
                try:
                    detail = item.get("detail") or {}
                    key = [identifier(decision["code"]), identifier(item["signal_type"]),
                           identifier(item["period"]), iso_day(detail.get("data_date") or item.get("data_date"))]
                    if item.get("code", decision["code"]) != decision["code"]:
                        raise EvidenceError("signal_code")
                    keys.append(key)
                except (KeyError, TypeError, ValueError, AttributeError):
                    identity_unknown = True
        paths = {"target": bars(target_frame, start, end), "benchmark": bars(benchmark, start, end)}
        lineage = {"target": source_lineage(target_frame), "benchmark": source_lineage(benchmark)}
        path_complete = [r["date"] for r in paths["target"]] == sessions[:horizon]
        prior_bar = None
        if target_frame is not None and not getattr(target_frame, "empty", True):
            prior = target_frame[target_frame["date"].map(iso_day) <= decision_day]
            if not prior.empty:
                prior_day = iso_day(prior.iloc[-1]["date"])
                prior_bar = bars(prior.tail(1), prior_day, prior_day)[0]
        reference = decision.get("reference_close")
        reference = finite(float(reference)) if reference is not None else (prior_bar["close"] if prior_bar else None)
        provenance_unknown = identity_unknown or not path_complete or any(v["status"] == "unknown" for v in lineage.values())
        payload = {
            "coverage": "new_post_cutover_decisions_only; outcome_inputs_only; no historical decision-version claim",
            "cutover": self.cutover.isoformat(),
            "decision": {k: decision.get(k) for k in ("decision_id", "run_id", "decision_at", "decision_date", "code", "data_date", "decision_state", "risk_action", "data_status", "reference_close", "raw_trigger_count", "validation_split")},
            "signal_keys": keys, "signal_identity_status": "unknown" if identity_unknown else "known",
            "canonical_selection": "not performed here; deduplicate against full decision history before research",
            "decision_process_version": None,
            "outcome": {k: outcome.get(k) for k in OUTCOME_FIELDS},
            "representation": "actual_normalised_inputs; not raw provider response",
            "session_window": {"entry": start, "exit": end, "dates": sessions[:horizon], "calendar_verification": "observed_benchmark_only"},
            "reference_input": {"effective_prior_close": reference, "prior_bar": prior_bar, "source": "decision_reference_close" if decision.get("reference_close") is not None else "last_prior_target_bar"},
            "effective_settings": asdict(settings), "process_version": self.manifest,
            "effective_settings_sha256": digest(asdict(settings)),
            "paths": paths, "paths_sha256": digest(paths), "lineage": lineage,
            "target_session_coverage": "complete" if path_complete else "incomplete",
            "database_link": "business insert returned true; sidecar+DB not atomic; independently join by decision/horizon and outcome values",
            "execution_validation": "unchanged legacy hypothesis; not fills, not independently T+1/liquidity/adjustment verified",
        }
        payload_hash = digest(payload)
        fd = self._open_directory()
        name, temporary = f"{identity}-{horizon}.json", None
        try:
            existing = self._read(fd, name)
            if existing is not None:
                return {"status": "existing" if existing["payload_sha256"] == payload_hash else "conflict",
                        "basis_diff": bool(existing.get("cross_horizon_differences")), "provenance_unknown": provenance_unknown}
            changes = self._basis_differences(fd, payload)
            record = {"schema_version": "swing-outcome-input-v1", "observed_at": now.isoformat(),
                      "payload": payload, "payload_sha256": payload_hash,
                      "cross_horizon_differences": changes}
            invocation = os.environ.get("INVOCATION_ID", "")
            record["invocation"] = {"systemd_id": invocation if re.fullmatch(r"[0-9a-fA-F]{32}", invocation) else None,
                                    "scheduler_origin": "not_proven_by_environment; manual same-day execution cannot be distinguished here"}
            record["record_sha256"] = digest(record)
            data = canonical(record) + b"\n"
            if len(data) > MAX_RECORD:
                raise EvidenceError("record_budget")
            used, count = 0, 0
            with os.scandir(fd) as entries:
                for entry in entries:
                    count += 1
                    if not entry.is_file(follow_symlinks=False):
                        raise EvidenceError("foreign_evidence_entry")
                    used += entry.stat(follow_symlinks=False).st_size
                    if count >= MAX_FILES or used + len(data) > MAX_DIRECTORY:
                        raise EvidenceError("directory_budget")
            space = os.fstatvfs(fd)
            if space.f_bavail * space.f_frsize < MIN_FREE + len(data):
                raise EvidenceError("disk_headroom")
            temporary = f".pending-{uuid.uuid4().hex}"
            handle_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic no-replace publish. A racing writer may only win first.
            try:
                os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                winner = self._read(fd, name)
                return {"status": "existing" if winner["payload_sha256"] == payload_hash else "conflict", "basis_diff": bool(winner.get("cross_horizon_differences")), "provenance_unknown": provenance_unknown}
            os.fsync(fd)
            return {"status": "written", "basis_diff": bool(changes),
                    "provenance_unknown": provenance_unknown}
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
            os.close(fd)


def recorder_from_config(swing, *, config_path, repo, logger):
    """No default activation and no inferred or rolling cutover timestamp."""
    section = swing.get("outcome_evaluation", {}).get("provenance", {})
    if section == {} or (isinstance(section, dict) and section.get("enabled") is False):
        return None
    cutover = None
    try:
        if not isinstance(section, dict) or section.get("enabled") is not True:
            raise EvidenceError("invalid_enable_flag")
        cutover = timestamp(section["decision_at_or_after"])
        repo, config_path = Path(repo), Path(config_path)
        manifest = observed_manifest(repo, config_path)
        return OutcomeRecorder(repo / "data/swing_outcome_evidence_v1",
            cutover=section["decision_at_or_after"], manifest=manifest,
            validate_sources=lambda: observed_manifest(repo, config_path))
    except Exception as exc:
        # Never echo config values, paths, raw exceptions, or credentials.
        logger.error("outcome evidence unavailable error_type=%s", type(exc).__name__)
        return UnavailableRecorder(cutover=cutover)
