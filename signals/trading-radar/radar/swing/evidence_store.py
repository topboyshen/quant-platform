"""Evidence, outcome and outbox operations mixed into :class:`SwingStore`."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any, *, limit: int = 32_768) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > limit:
        raise ValueError(f"审计 JSON 超过 {limit} 字节")
    return encoded


def _checksum(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


class FactorImportPreconditionChanged(RuntimeError):
    """Raised before insertion when a same-day repair is no longer eligible."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


class SwingEvidenceStoreMixin:
    """Database methods whose rows form immutable decision evidence."""

    def schema_version(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def get_eod_runs_for_date(self, decision_date: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM swing_eod_run WHERE decision_date=? ORDER BY id DESC",
                (str(decision_date),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reason_codes"] = _load_json(item.get("reason_codes"), [])
            item["details"] = _load_json(item.get("details"), {})
            result.append(item)
        return result

    def get_run_evidence_counts(self, run_id: str) -> Dict[str, int]:
        with self._lock, self._connect() as conn:
            decisions = conn.execute(
                "SELECT COUNT(*) FROM eod_target_decision WHERE run_id=?",
                (str(run_id),),
            ).fetchone()[0]
            risk_snapshots = conn.execute(
                "SELECT COUNT(*) FROM eod_risk_snapshot WHERE run_id=?",
                (str(run_id),),
            ).fetchone()[0]
        return {"target_decisions": int(decisions), "risk_snapshots": int(risk_snapshots)}

    def decision_state_counts(self, decision_date: Optional[str] = None) -> Dict[str, int]:
        clause = " WHERE decision_date=?" if decision_date else ""
        params = (str(decision_date),) if decision_date else ()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT decision_state,COUNT(*) AS count FROM eod_target_decision"
                + clause
                + " GROUP BY decision_state",
                params,
            ).fetchall()
        counts = {state: 0 for state in ("no_trigger", "allowed", "blocked", "data_failed")}
        counts.update({str(row["decision_state"]): int(row["count"]) for row in rows})
        return counts

    # ---------------------------------------------------------- factor imports
    def record_factor_import(
        self,
        *,
        batch: Dict[str, Any],
        observations: Iterable[Dict[str, Any]],
        apply_to_macro_slice: bool = True,
    ) -> Dict[str, int]:
        """Atomically append one verified factor batch and fill empty slices.

        Existing non-null factor values are never overwritten.  The immutable
        observation table retains every imported value so disagreements can be
        audited explicitly instead of silently replacing production data.
        """
        batch_id = str(batch.get("batch_id") or uuid.uuid4().hex)
        factor_name = str(batch.get("factor_name") or "")
        if factor_name != "total_amount":
            raise ValueError("当前可信因子导入仅允许 total_amount")
        source = str(batch.get("source") or "")[:64]
        source_ref = str(batch.get("source_ref") or "")[:512]
        checksum = str(batch.get("checksum") or "")
        status = str(batch.get("validation_status") or "")
        details = batch.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("因子导入批次 details 必须是对象")
        import_kind = str(details.get("import_kind") or "historical")
        if import_kind not in {"historical", "same_day_stale_only_repair"}:
            raise ValueError("因子导入类型非法")
        rows = list(observations)
        if not source or not source_ref or len(checksum) != 64:
            raise ValueError("因子导入批次缺少来源、来源引用或 SHA-256")
        if status not in {"accepted", "rejected"}:
            raise ValueError("历史导入校验状态非法")
        if int(batch.get("record_count", len(rows))) != len(rows):
            raise ValueError("批次记录数与观测数量不一致")
        if _checksum(rows) != checksum:
            raise ValueError("因子导入批次 SHA-256 校验失败")
        dates = [str(item.get("observation_date") or "") for item in rows]
        if len(set(dates)) != len(dates):
            raise ValueError("因子导入批次包含重复观测日")

        repair_date: Optional[str] = None
        qualifying_run_id = ""
        if import_kind == "same_day_stale_only_repair":
            if apply_to_macro_slice is not True:
                raise ValueError("当日 stale-only repair 必须填充 macro_slice")
            if status != "accepted":
                raise ValueError("当日 stale-only repair 只允许 accepted 批次")
            if len(rows) != 1 or len(dates) != 1:
                raise ValueError("当日 stale-only repair 必须且只能包含一条观测")
            repair_date = dates[0]
            try:
                parsed_repair_date = datetime.strptime(repair_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("当日 stale-only repair 日期非法") from exc
            if parsed_repair_date.strftime("%Y-%m-%d") != repair_date:
                raise ValueError("当日 stale-only repair 日期非法")
            if (
                str(batch.get("requested_start") or "") != repair_date
                or str(batch.get("requested_end") or "") != repair_date
                or str(details.get("decision_date") or "") != repair_date
            ):
                raise ValueError("当日 stale-only repair 批次与观测日期不一致")
            qualifying_run_id = str(details.get("qualifying_run_id") or "")
            if not qualifying_run_id:
                raise ValueError("当日 stale-only repair 缺少 qualifying_run_id")

        inserted = 0
        filled = 0
        conflicts = 0
        imported_at = _utc_now_iso()
        with self._lock, self._connect() as conn:
            if import_kind == "same_day_stale_only_repair":
                # Serialize the eligibility check and factor append.  Nothing is
                # inserted before these predicates have been revalidated under
                # the SQLite writer lock.
                conn.execute("BEGIN IMMEDIATE")
                latest = conn.execute(
                    "SELECT run_id,status,reason_codes FROM swing_eod_run "
                    "WHERE decision_date=? ORDER BY id DESC LIMIT 1",
                    (repair_date,),
                ).fetchone()
                if latest is None:
                    raise FactorImportPreconditionChanged("eod_run_missing")
                reasons = _load_json(latest["reason_codes"], [])
                if str(latest["run_id"]) != qualifying_run_id:
                    raise FactorImportPreconditionChanged("latest_run_changed")
                if str(latest["status"]) != "partial":
                    raise FactorImportPreconditionChanged("latest_eod_not_partial")
                if not isinstance(reasons, list) or "macro_data_partial" not in reasons:
                    raise FactorImportPreconditionChanged(
                        "macro_data_partial_absent"
                    )
                slices = conn.execute(
                    "SELECT total_amount FROM macro_slice WHERE date=?",
                    (repair_date,),
                ).fetchall()
                if len(slices) != 1:
                    raise FactorImportPreconditionChanged("macro_slice_changed")
                if slices[0]["total_amount"] is not None:
                    raise FactorImportPreconditionChanged("total_amount_present")
            conn.execute(
                "INSERT INTO factor_import_batch "
                "(batch_id,factor_name,source,source_ref,requested_start,requested_end,"
                "observed_at,record_count,checksum,validation_status,details) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    factor_name,
                    source,
                    source_ref,
                    batch.get("requested_start"),
                    batch.get("requested_end"),
                    str(batch.get("observed_at") or imported_at),
                    len(rows),
                    checksum,
                    status,
                    _json(details),
                ),
            )
            for item in rows:
                value = float(item.get("value"))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("历史因子包含非有限或非正数")
                observation_date = str(item.get("observation_date") or "")
                source_market_time = str(item.get("source_market_time") or "")
                available_at = str(item.get("available_at") or "")
                row_checksum = str(item.get("checksum") or "")
                if (
                    len(observation_date) != 10
                    or not source_market_time
                    or not available_at
                    or len(row_checksum) != 64
                ):
                    raise ValueError("历史因子观测元数据不完整")
                row_core = {
                    "factor_name": factor_name,
                    "observation_date": observation_date,
                    "value": value,
                    "source": source,
                    "source_ref": source_ref,
                    "source_market_time": source_market_time,
                    "available_at": available_at,
                    "components": item.get("components") or {},
                }
                if _checksum(row_core) != row_checksum:
                    raise ValueError(
                        f"历史因子观测 {observation_date} SHA-256 校验失败"
                    )
                conn.execute(
                    "INSERT INTO factor_observation "
                    "(observation_id,batch_id,factor_name,observation_date,value,source,"
                    "source_ref,source_market_time,available_at,checksum,components_json,imported_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        factor_name,
                        observation_date,
                        value,
                        source,
                        source_ref,
                        source_market_time,
                        available_at,
                        row_checksum,
                        _json(item.get("components") or {}),
                        imported_at,
                    ),
                )
                inserted += 1
                if status != "accepted" or not apply_to_macro_slice:
                    continue
                existing = conn.execute(
                    "SELECT total_amount,source,extra FROM macro_slice WHERE date=?",
                    (observation_date,),
                ).fetchone()
                if existing is not None and existing["total_amount"] is not None:
                    old_value = float(existing["total_amount"])
                    if abs(old_value - value) / max(old_value, value) > 0.03:
                        conflicts += 1
                    continue
                extra = _load_json(existing["extra"], {}) if existing else {}
                if not isinstance(extra, dict):
                    extra = {}
                collect_status = extra.get("collect_status")
                if not isinstance(collect_status, dict):
                    collect_status = {}
                collect_status["total_amount"] = {
                    "available": True,
                    "source": source,
                    "observed_date": observation_date,
                    "source_market_time": source_market_time,
                    "available_at": available_at,
                    "provenance_batch_id": batch_id,
                    "checksum": row_checksum,
                    "factor_import": True,
                    "import_kind": import_kind,
                    "historical_import": import_kind == "historical",
                }
                extra["collect_status"] = collect_status
                previous_source = str(existing["source"] or "") if existing else ""
                sources = [part for part in previous_source.split("+") if part]
                if source not in sources:
                    sources.append(source)
                if existing is None:
                    conn.execute(
                        "INSERT INTO macro_slice(date,total_amount,source,extra) VALUES (?,?,?,?)",
                        (observation_date, value, "+".join(sources), _json(extra)),
                    )
                else:
                    conn.execute(
                        "UPDATE macro_slice SET total_amount=?,source=?,extra=? WHERE date=?",
                        (value, "+".join(sources), _json(extra), observation_date),
                    )
                filled += 1
        return {"inserted": inserted, "filled": filled, "conflicts": conflicts}

    # --------------------------------------------------------- risk/decisions
    def record_risk_snapshot(
        self, run_id: str, *, decision_at: str, risk_state: Optional[Dict[str, Any]]
    ) -> str:
        state = risk_state if isinstance(risk_state, dict) else {}
        snapshot_id = uuid.uuid4().hex
        values = {
            name: state.get(name) for name in ("vix", "us_10y", "crude_oil")
        }
        with self._lock, self._connect() as conn:
            run = conn.execute(
                "SELECT status FROM swing_eod_run WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("风险快照只能绑定运行中的 EOD run")
            conn.execute(
                "INSERT INTO eod_risk_snapshot "
                "(snapshot_id,run_id,decision_at,level,values_json,"
                "source_timestamps_json,available_at_json,sources_json,"
                "missing_fields_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    str(run_id),
                    str(decision_at),
                    str(state.get("level") or "unavailable")[:32],
                    _json(values),
                    _json(state.get("source_timestamps") or {}),
                    _json(state.get("available_at") or {}),
                    _json(state.get("sources") or {}),
                    _json(state.get("missing_fields") or []),
                    _utc_now_iso(),
                ),
            )
        return snapshot_id

    def record_target_decisions(
        self,
        run_id: str,
        *,
        decision_at: str,
        snapshot_id: Optional[str],
        decisions: Iterable[Dict[str, Any]],
    ) -> Dict[str, str]:
        rows = list(decisions)
        result: Dict[str, str] = {}
        with self._lock, self._connect() as conn:
            run = conn.execute(
                "SELECT decision_date,target_count,status FROM swing_eod_run WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("逐标的决策只能绑定运行中的 EOD run")
            if len(rows) > int(run["target_count"]):
                raise ValueError("逐标的决策数量超过运行目标数")
            for item in rows:
                code = str(item.get("code") or "")
                data_status = str(item.get("data_status") or "")
                decision_state = str(item.get("decision_state") or "")
                risk_action = str(item.get("risk_action") or "")
                raw_signals = item.get("raw_signals") or []
                raw_trigger_count = int(item.get("raw_trigger_count", len(raw_signals)))
                if not code or raw_trigger_count != len(raw_signals):
                    raise ValueError("逐标的决策身份或原始信号计数非法")
                decision_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO eod_target_decision "
                    "(decision_id,run_id,snapshot_id,decision_date,decision_at,data_date,"
                    "code,name,sector,data_status,decision_state,raw_trigger_count,"
                    "raw_signals_json,risk_action,risk_level,reason_code,reference_close,"
                    "validation_split,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        str(run_id),
                        snapshot_id,
                        run["decision_date"],
                        str(decision_at),
                        item.get("data_date"),
                        code,
                        str(item.get("name") or code)[:128],
                        str(item.get("sector") or "")[:64],
                        data_status,
                        decision_state,
                        raw_trigger_count,
                        _json(raw_signals, limit=16_384),
                        risk_action,
                        str(item.get("risk_level") or "unavailable")[:32],
                        str(item.get("reason_code") or "")[:64],
                        item.get("reference_close"),
                        str(item.get("validation_split") or "live_oos"),
                        _utc_now_iso(),
                    ),
                )
                result[code] = decision_id
        return result

    def list_decisions_for_outcome(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT d.*, GROUP_CONCAT(o.horizon_sessions) AS completed_horizons "
                "FROM eod_target_decision d "
                "LEFT JOIN eod_decision_outcome o ON o.decision_id=d.decision_id "
                "GROUP BY d.decision_id HAVING COUNT(o.outcome_id) < 5 "
                "ORDER BY d.decision_date,d.code LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw_signals"] = _load_json(item.pop("raw_signals_json", ""), [])
            item["completed_horizons"] = {
                int(value)
                for value in str(item.get("completed_horizons") or "").split(",")
                if value
            }
            result.append(item)
        return result

    def record_decision_outcome(self, outcome: Dict[str, Any]) -> bool:
        fields = (
            "decision_id", "horizon_sessions", "evaluation_as_of", "entry_date",
            "entry_open", "entry_fill", "exit_date", "exit_close", "exit_fill",
            "benchmark_code", "benchmark_return", "gap_return", "gross_return",
            "net_return", "excess_return", "mfe", "mae", "fees_return",
            "slippage_return", "fill_status", "fill_reason",
        )
        values = [outcome.get(field) for field in fields]
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO eod_decision_outcome "
                f"(outcome_id,{','.join(fields)},created_at) "
                f"VALUES ({','.join('?' for _ in range(len(fields) + 2))})",
                [uuid.uuid4().hex, *values, _utc_now_iso()],
            )
        return cursor.rowcount == 1

    def get_validity_rows(self, *, horizon_sessions: int = 20) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT d.decision_state,d.raw_trigger_count,d.validation_split,d.code,"
                "o.* FROM eod_decision_outcome o JOIN eod_target_decision d "
                "ON d.decision_id=o.decision_id "
                "WHERE o.horizon_sessions=? AND o.fill_status='filled'",
                (int(horizon_sessions),),
            ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- outbox
    def enqueue_notification(
        self,
        idempotency_key: str,
        *,
        operation: str,
        payload: Dict[str, Any],
        channel: str = "telegram",
    ) -> Dict[str, Any]:
        key = str(idempotency_key or "")
        if not key or len(key) > 256:
            raise ValueError("通知幂等键为空或过长")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notification_outbox "
                "(idempotency_key,channel,operation,payload_json,status,created_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (key, str(channel)[:32], str(operation)[:64], _json(payload), _utc_now_iso()),
            )
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row)

    def begin_notification_delivery(self, idempotency_key: str) -> str:
        """Claim a row once; an existing ``sending`` row is never auto-retried.

        This deliberately treats a crash after the remote send as ambiguous.
        Operators can reconcile it, but an automatic dispatcher cannot emit a
        duplicate Telegram message merely because the local sent marker is absent.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM notification_outbox WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
            if row is None:
                raise RuntimeError("通知 outbox 记录不存在")
            status = str(row["status"])
            if status in {"sent", "sending"}:
                return status
            cursor = conn.execute(
                "UPDATE notification_outbox SET status='sending',attempts=attempts+1,"
                "attempted_at=?,last_error=NULL WHERE idempotency_key=? "
                "AND status IN ('pending','failed')",
                (_utc_now_iso(), str(idempotency_key)),
            )
            return "claimed" if cursor.rowcount == 1 else "sending"

    def finish_notification_delivery(
        self, idempotency_key: str, *, success: bool, error: str = ""
    ) -> None:
        status = "sent" if success else "failed"
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE notification_outbox SET status=?,sent_at=?,last_error=? "
                "WHERE idempotency_key=? AND status='sending'",
                (
                    status,
                    _utc_now_iso() if success else None,
                    None if success else str(error)[:1000],
                    str(idempotency_key),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("通知 outbox 状态已变化")

    def outbox_counts(self) -> Dict[str, int]:
        """Legacy outbox status counts plus, on a v6 database, ``dispatch_<state>`` counts.

        The dispatch keys are always present on v6 (zeros included) so a missing key can
        never be mistaken for "healthy".
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS count FROM notification_outbox GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in ("pending", "sending", "sent", "failed")}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        ready, _ = self.dispatch_ready()
        if ready:
            counts.update(
                {f"dispatch_{state}": n for state, n in self.dispatch_counts().items()}
            )
        return counts

    def notification_health(self) -> Dict[str, Any]:
        """Derived dispatch health (None when the v6 tables are not available)."""
        ready, reason = self.dispatch_ready()
        if not ready:
            return {"available": False, "reason": reason}
        return {"available": True, **self.dispatch_health()}

    @staticmethod
    def notification_key(operation: str, payload: Dict[str, Any]) -> str:
        canonical = _json({"operation": operation, "payload": payload}, limit=64_000)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{operation}:{digest}"
