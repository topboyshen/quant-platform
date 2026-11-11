"""SwingStore - 每日宏观情绪切片持久化（sqlite）。

每天收盘后由 ``MacroSnapshotCollector`` 把「大盘环境过滤器」需要的三因子打满：

- ``total_amount``        : 全市场每日成交额（亿元）
- ``margin_balance``      : 两融余额（亿元）
- ``margin_growth_pct``   : 两融余额环比增速（%）
- ``limit_up``/``limit_down``: 涨停/跌停家数
- ``limit_ratio``         : 涨跌停家数比 = limit_up / (limit_down + 1)

存量切片用于历史分位数计算，判断「情绪冰点 / 沸点」。

风格参照 ``radar/data/price_history_store.py`` 的 sqlite 后端：单文件、线程锁、
``CREATE TABLE IF NOT EXISTS`` 幂等建表。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from radar.swing.dispatch_store import NotificationDispatchMixin
from radar.swing.evidence_store import SwingEvidenceStoreMixin

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MacroSlice:
    """单日宏观情绪切片。``date`` 为 ``YYYY-MM-DD`` 字符串。"""

    date: str
    total_amount: Optional[float] = None      # 全市场成交额（亿元）
    margin_balance: Optional[float] = None     # 两融余额（亿元）
    margin_growth_pct: Optional[float] = None  # 两融余额环比增速（%）
    limit_up: Optional[int] = None             # 涨停家数
    limit_down: Optional[int] = None           # 跌停家数
    limit_ratio: Optional[float] = None        # 涨跌停家数比
    source: str = ""                           # 数据来源标记（akshare / mx / mixed）
    extra: Dict = field(default_factory=dict)

    def to_row(self) -> tuple:
        import json

        return (
            self.date,
            self.total_amount,
            self.margin_balance,
            self.margin_growth_pct,
            self.limit_up,
            self.limit_down,
            self.limit_ratio,
            self.source,
            json.dumps(self.extra or {}, ensure_ascii=False),
        )


_COLUMNS = (
    "date",
    "total_amount",
    "margin_balance",
    "margin_growth_pct",
    "limit_up",
    "limit_down",
    "limit_ratio",
    "source",
    "extra",
)

# 可做分位数极值判定的数值字段
NUMERIC_FACTORS = (
    "total_amount",
    "margin_growth_pct",
    "limit_ratio",
)

_RUN_STATUSES = {"running", "success", "partial", "failed", "skipped"}
_RUN_OUTCOMES = {
    "not_evaluated",
    "emitted",
    "no_signal",
    "blocked",
    "data_unavailable",
    "skipped",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SwingStore(SwingEvidenceStoreMixin, NotificationDispatchMixin):
    """宏观情绪切片 sqlite 存储。"""

    def __init__(
        self,
        db_path: str = "data/swing_macro.db",
        *,
        initialize_schema: bool = True,
    ):
        self.db_path = db_path
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if initialize_schema:
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        from radar.swing.migrations import migrate

        with self._lock, self._connect() as conn:
            migrate(conn)
        # integration-api drops all caps and cannot read owner-only 0600 files.
        try:
            os.chmod(self.db_path, 0o644)
        except OSError:
            pass

    def upsert(self, slice_: MacroSlice) -> None:
        """写入/更新当日切片（按 date 主键幂等）。"""
        placeholders = ",".join("?" for _ in _COLUMNS)
        updates = ",".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "date")
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO macro_slice ({','.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(date) DO UPDATE SET {updates}",
                slice_.to_row(),
            )
        logger.debug("SwingStore 写入切片 %s (source=%s)", slice_.date, slice_.source)

    def upsert_partial(self, slice_: MacroSlice) -> MacroSlice:
        """合并写入一次可能不完整的实时采集结果。

        同日重复采集时，单个上游失败不能把当天较早一次已成功保存的因子
        覆盖成 ``NULL``。失败因子的旧值及其观测元数据会保留；本次失败
        状态单独写入 ``last_collect_status``，便于排障且不冒充旧值元数据。

        常规 ``upsert`` 仍保留完整覆盖语义，供显式构造完整切片的调用方使用。
        """
        factor_columns = {
            "total_amount": ("total_amount",),
            "margin_balance": ("margin_balance",),
            "margin_growth_pct": ("margin_growth_pct",),
            "limit_ratio": ("limit_up", "limit_down", "limit_ratio"),
        }

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM macro_slice WHERE date = ?", (slice_.date,)
            ).fetchone()
            if row is None:
                merged = slice_
            else:
                existing = self._row_to_slice(row)
                incoming_extra = slice_.extra if isinstance(slice_.extra, dict) else {}
                existing_extra = existing.extra if isinstance(existing.extra, dict) else {}
                merged_extra = dict(existing_extra)
                for key, value in incoming_extra.items():
                    if key not in {"collect_status", "last_collect_status"}:
                        merged_extra[key] = value

                old_status = existing_extra.get("collect_status", {})
                if not isinstance(old_status, dict):
                    old_status = {}
                new_status = incoming_extra.get("collect_status", {})
                if not isinstance(new_status, dict):
                    new_status = {}
                merged_status = dict(old_status)
                last_attempt = {}
                for factor, status in new_status.items():
                    columns = factor_columns.get(factor, (factor,))
                    has_complete_new_value = all(
                        hasattr(slice_, column) and getattr(slice_, column) is not None
                        for column in columns
                    )
                    has_old_value = any(
                        hasattr(existing, column) and getattr(existing, column) is not None
                        for column in columns
                    )
                    if has_complete_new_value or not has_old_value:
                        merged_status[factor] = status
                    else:
                        last_attempt[factor] = status
                if merged_status:
                    merged_extra["collect_status"] = merged_status
                else:
                    merged_extra.pop("collect_status", None)
                if last_attempt:
                    merged_extra["last_collect_status"] = last_attempt
                else:
                    merged_extra.pop("last_collect_status", None)

                def _source_tokens(value: str) -> List[str]:
                    return [
                        token
                        for token in str(value or "").split("+")
                        if token and token != "none"
                    ]

                source = "+".join(
                    dict.fromkeys(
                        _source_tokens(existing.source) + _source_tokens(slice_.source)
                    )
                ) or "none"
                merged = MacroSlice(
                    date=slice_.date,
                    total_amount=(
                        slice_.total_amount
                        if slice_.total_amount is not None
                        else existing.total_amount
                    ),
                    margin_balance=(
                        slice_.margin_balance
                        if slice_.margin_balance is not None
                        else existing.margin_balance
                    ),
                    margin_growth_pct=(
                        slice_.margin_growth_pct
                        if slice_.margin_growth_pct is not None
                        else existing.margin_growth_pct
                    ),
                    limit_up=(
                        slice_.limit_up if slice_.limit_up is not None else existing.limit_up
                    ),
                    limit_down=(
                        slice_.limit_down
                        if slice_.limit_down is not None
                        else existing.limit_down
                    ),
                    limit_ratio=(
                        slice_.limit_ratio
                        if slice_.limit_ratio is not None
                        else existing.limit_ratio
                    ),
                    source=source,
                    extra=merged_extra,
                )

            placeholders = ",".join("?" for _ in _COLUMNS)
            updates = ",".join(
                f"{column}=excluded.{column}" for column in _COLUMNS if column != "date"
            )
            conn.execute(
                f"INSERT INTO macro_slice ({','.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(date) DO UPDATE SET {updates}",
                merged.to_row(),
            )
        logger.debug(
            "SwingStore 合并写入实时切片 %s (source=%s)", merged.date, merged.source
        )
        return merged

    def get_latest(self) -> Optional[MacroSlice]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM macro_slice ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return self._row_to_slice(row) if row else None

    def get_for_date(self, date: str) -> Optional[MacroSlice]:
        """Return the exact dated slice without falling back to older data."""
        date = self._validate_run_date(date, "date")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM macro_slice WHERE date = ?", (date,)
            ).fetchone()
        return self._row_to_slice(row) if row else None

    def get_recent(self, n: int = 250) -> List[MacroSlice]:
        """返回最近 n 个交易日切片，按日期升序。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM macro_slice ORDER BY date DESC LIMIT ?", (int(n),)
            ).fetchall()
        return [self._row_to_slice(r) for r in reversed(rows)]

    def get_series(
        self, factor: str, n: int = 250, exclude_date: Optional[str] = None
    ) -> List[float]:
        """取某个数值因子的历史序列（升序，剔除 None）。

        ``exclude_date`` 用于排除某一天（通常是当前切片自身），避免分位数
        计算时把当前值算进历史样本造成偏置。
        """
        if factor not in NUMERIC_FACTORS:
            raise ValueError(f"未知因子: {factor}（可选 {NUMERIC_FACTORS}）")
        out: List[float] = []
        for s in self.get_recent(n):
            if exclude_date is not None and s.date == exclude_date:
                continue
            v = getattr(s, factor)
            if v is not None:
                out.append(float(v))
        return out

    def get_latest_factor_value(self, factor: str, n: int = 250) -> Optional[tuple[str, float]]:
        """取某个数值因子最近一次非空值，返回 ``(date, value)``。"""
        if factor not in NUMERIC_FACTORS:
            raise ValueError(f"未知因子: {factor}（可选 {NUMERIC_FACTORS}）")
        limit = max(1, int(n))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT date,{factor} FROM ("
                f"SELECT date,{factor} FROM macro_slice ORDER BY date DESC LIMIT ?"
                f") WHERE {factor} IS NOT NULL ORDER BY date DESC LIMIT 1",
                (limit,),
            ).fetchone()
        if row is None:
            return None
        return row["date"], float(row[factor])

    # ---------------------------------------------------------- EOD run audit
    @staticmethod
    def _validate_run_date(value: str, field_name: str) -> str:
        text = str(value or "")
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{field_name} 必须是 YYYY-MM-DD") from exc
        if parsed.strftime("%Y-%m-%d") != text:
            raise ValueError(f"{field_name} 必须是 YYYY-MM-DD")
        return text

    @staticmethod
    def _bounded_run_count(value: int, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} 必须是非负整数")
        number = int(value)
        if number < 0 or number > 10_000 or number != value:
            raise ValueError(f"{field_name} 必须是非负整数")
        return number

    def begin_eod_run(self, decision_date: str, *, target_count: int) -> str:
        """Append a running EOD attempt and return its immutable run id."""
        decision_date = self._validate_run_date(decision_date, "decision_date")
        target_count = self._bounded_run_count(target_count, "target_count")
        run_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO swing_eod_run "
                "(run_id, decision_date, status, started_at, target_count) "
                "VALUES (?, ?, 'running', ?, ?)",
                (run_id, decision_date, _utc_now_iso(), target_count),
            )
        return run_id

    def finish_eod_run(
        self,
        run_id: str,
        *,
        status: str,
        decision_outcome: str,
        data_date: Optional[str] = None,
        evaluated_count: int = 0,
        failed_count: int = 0,
        signal_count: int = 0,
        allowed_signal_count: int = 0,
        blocked_signal_count: int = 0,
        reason_codes: Optional[List[str]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Finalize exactly one running attempt; completed rows are immutable."""
        import json as _json

        status = str(status or "")
        decision_outcome = str(decision_outcome or "")
        if status not in _RUN_STATUSES - {"running"}:
            raise ValueError(f"非法 EOD 状态: {status}")
        if decision_outcome not in _RUN_OUTCOMES - {"not_evaluated"}:
            raise ValueError(f"非法 EOD 决策结果: {decision_outcome}")
        if data_date:
            data_date = self._validate_run_date(data_date, "data_date")
        evaluated_count = self._bounded_run_count(evaluated_count, "evaluated_count")
        failed_count = self._bounded_run_count(failed_count, "failed_count")
        signal_count = self._bounded_run_count(signal_count, "signal_count")
        allowed_signal_count = self._bounded_run_count(
            allowed_signal_count, "allowed_signal_count"
        )
        blocked_signal_count = self._bounded_run_count(
            blocked_signal_count, "blocked_signal_count"
        )
        allowed_outcomes = {
            "success": {"emitted", "no_signal", "blocked"},
            "partial": {"emitted", "blocked", "data_unavailable"},
            "failed": {"data_unavailable"},
            "skipped": {"skipped"},
        }
        if decision_outcome not in allowed_outcomes[status]:
            raise ValueError("EOD 状态与决策结果不一致")
        if allowed_signal_count + blocked_signal_count != signal_count:
            raise ValueError("EOD 信号总数与放行/拦截数量不一致")
        if decision_outcome in {"no_signal", "data_unavailable", "skipped"}:
            if signal_count != 0:
                raise ValueError("该 EOD 决策结果不允许包含信号")
        elif decision_outcome == "emitted":
            if signal_count == 0 or allowed_signal_count == 0:
                raise ValueError("emitted 必须至少包含一条放行信号")
        elif decision_outcome == "blocked":
            if signal_count == 0 or allowed_signal_count != 0:
                raise ValueError("blocked 必须全部为风控拦截信号")
        cleaned_reasons = []
        for value in reason_codes or []:
            text = str(value or "")[:64]
            if text and text.replace("_", "").isalnum() and text not in cleaned_reasons:
                cleaned_reasons.append(text)
        encoded_reasons = _json.dumps(cleaned_reasons[:8], ensure_ascii=False)
        encoded_details = _json.dumps(details or {}, ensure_ascii=False, default=str)
        if len(encoded_details) > 16_384:
            # Keep only the existing, run-bound evidence pair when general
            # details exceed the historical character limit. The unchanged
            # reader admits at most 512000 UTF-8 bytes; never crop targets or
            # infer evidence from live config to squeeze it into that budget.
            compact_details: Dict[str, Any] = {"truncated": True}
            if isinstance(details, dict) and "rule_attribution" in details:
                attribution = details["rule_attribution"]
                if isinstance(attribution, dict) and attribution:
                    attributed = {**compact_details, "rule_attribution": attribution}
                    bounded = _json.dumps(attributed, ensure_ascii=False, default=str)
                    if len(bounded) <= 16_384:
                        compact_details = attributed
                        diagnostics = details.get("trigger_diagnostics")
                        if isinstance(diagnostics, dict) and diagnostics:
                            evidence = {**attributed, "trigger_diagnostics": diagnostics}
                            encoded_evidence = _json.dumps(evidence, ensure_ascii=False, default=str)
                            if len(encoded_evidence.encode("utf-8")) <= 512_000:
                                compact_details = evidence
                            else:
                                compact_details["trigger_diagnostics_unavailable"] = "exceeds_reader_byte_limit"
                        else:
                            compact_details["trigger_diagnostics_unavailable"] = "invalid_or_missing"
                    else:
                        compact_details["rule_attribution_unavailable"] = "exceeds_details_limit"
                else:
                    compact_details["rule_attribution_unavailable"] = "invalid_or_empty"
            encoded_details = _json.dumps(compact_details, ensure_ascii=False, default=str)

        with self._lock, self._connect() as conn:
            run = conn.execute(
                "SELECT decision_date, target_count, status "
                "FROM swing_eod_run WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("EOD run 不存在或已完成")
            if data_date and data_date > run["decision_date"]:
                raise ValueError("data_date 不能晚于 decision_date")
            if evaluated_count + failed_count > int(run["target_count"]):
                raise ValueError("EOD 已检查与失败数量超过目标数")
            if status == "success" and (
                failed_count != 0 or evaluated_count != int(run["target_count"])
            ):
                raise ValueError("success 必须完整检查全部目标")
            cursor = conn.execute(
                "UPDATE swing_eod_run SET data_date=?, status=?, completed_at=?, "
                "evaluated_count=?, failed_count=?, signal_count=?, "
                "allowed_signal_count=?, blocked_signal_count=?, "
                "decision_outcome=?, reason_codes=?, details=? "
                "WHERE run_id=? AND status='running'",
                (
                    data_date,
                    status,
                    _utc_now_iso(),
                    evaluated_count,
                    failed_count,
                    signal_count,
                    allowed_signal_count,
                    blocked_signal_count,
                    decision_outcome,
                    encoded_reasons,
                    encoded_details,
                    str(run_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("EOD run 不存在或已完成")

    def get_latest_eod_run(self) -> Optional[Dict[str, Any]]:
        """Return the latest append-only EOD attempt for diagnostics/tests."""
        import json as _json

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM swing_eod_run ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key, fallback in (("reason_codes", []), ("details", {})):
            try:
                result[key] = _json.loads(result.get(key) or "")
            except (TypeError, ValueError):
                result[key] = fallback
        return result

    # ------------------------------------------------------------ trend signals
    @staticmethod
    def signal_key(signal: Dict) -> Optional[Tuple[str, str, str, str]]:
        """Return the immutable market-data identity for a generated signal."""
        detail = signal.get("detail", {})
        data_date = detail.get("data_date") if isinstance(detail, dict) else None
        if not data_date:
            data_date = signal.get("data_date")
        if not data_date:
            return None
        return (
            str(signal.get("code", "")),
            str(signal.get("signal_type", "")),
            str(signal.get("period", "")),
            str(data_date),
        )

    def get_signal_keys(
        self,
        *,
        exclude_date: Optional[str] = None,
        notified_only: bool = False,
    ) -> Set[Tuple[str, str, str, str]]:
        """Return persisted signal identities, optionally excluding one EOD run."""
        clauses = ["data_date IS NOT NULL", "data_date <> ''"]
        params: List[str] = []
        if exclude_date is not None:
            clauses.append("date <> ?")
            params.append(str(exclude_date))
        if notified_only:
            clauses.append("notified_at IS NOT NULL")
        sql = (
            "SELECT code, signal_type, period, data_date FROM trend_signal WHERE "
            + " AND ".join(clauses)
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {
            (r["code"], r["signal_type"], r["period"], r["data_date"])
            for r in rows
        }

    def mark_signals_notified(self, keys: List[Tuple[str, str, str, str]]) -> None:
        """Mark only successfully delivered trend notifications as sent."""
        if not keys:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "UPDATE trend_signal SET notified_at=COALESCE(notified_at, datetime('now')) "
                "WHERE code=? AND signal_type=? AND period=? AND data_date=?",
                keys,
            )

    def replace_signals_for_date(self, date: str, signals: List[Dict]) -> None:
        """覆盖某日的趋势信号（先删后插，使 EOD 重跑幂等）。

        每个 signal dict 字段：code/name/signal_type/period/title/detail/confidence。
        ``detail`` 为可 JSON 序列化的 dict。
        """
        import json as _json

        with self._lock, self._connect() as conn:
            # A same-day rerun may delete/reinsert the row. Preserve successful
            # notification state so it cannot emit the same data bar twice.
            notified_rows = conn.execute(
                "SELECT code,signal_type,period,data_date,notified_at FROM trend_signal "
                "WHERE notified_at IS NOT NULL AND data_date IS NOT NULL"
            ).fetchall()
            notified_by_key = {
                (r["code"], r["signal_type"], r["period"], r["data_date"]): r["notified_at"]
                for r in notified_rows
            }
            # 已成功发送的行是通知去重身份和审计记录，空结果或数据源失败的
            # 同日重跑都不得删除；只清理尚未通知、可安全重算的行。
            conn.execute(
                "DELETE FROM trend_signal WHERE date = ? AND notified_at IS NULL",
                (date,),
            )
            for s in signals:
                detail = s.get("detail", {}) if isinstance(s.get("detail", {}), dict) else {}
                key = self.signal_key(s)
                if key is not None and isinstance(detail.get("risk_gate"), dict):
                    # 同一行情 bar 若此前因风控拦截/发送失败而尚未通知，迁移到
                    # 当前决策日再写入最新 gate 审计；已通知行绝不删除。
                    conn.execute(
                        "DELETE FROM trend_signal WHERE code=? AND signal_type=? "
                        "AND period=? AND data_date=? AND notified_at IS NULL",
                        key,
                    )
                notified_row = notified_by_key.get(key) if key is not None else None
                if notified_row is not None:
                    # 同日风险状态切换时刷新展示字段，但保留 notified_at；
                    # 跨日已通知身份保持原审计日期，不迁移也不复制。
                    current = conn.execute(
                        "SELECT date FROM trend_signal WHERE code=? AND signal_type=? "
                        "AND period=? AND data_date=? AND notified_at IS NOT NULL",
                        key,
                    ).fetchone()
                    if current is not None and current["date"] == date:
                        conn.execute(
                            "UPDATE trend_signal SET name=?,title=?,detail=?,confidence=? "
                            "WHERE code=? AND signal_type=? AND period=? AND data_date=? "
                            "AND notified_at IS NOT NULL",
                            (
                                s.get("name", ""),
                                s.get("title", ""),
                                _json.dumps(detail, ensure_ascii=False),
                                float(s.get("confidence", 0) or 0),
                                *key,
                            ),
                        )
                        continue
                conn.execute(
                    "INSERT OR IGNORE INTO trend_signal "
                    "(date, code, name, signal_type, period, data_date, title, detail, confidence, notified_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        date,
                        str(s.get("code", "")),
                        s.get("name", ""),
                        s.get("signal_type", ""),
                        s.get("period", ""),
                        detail.get("data_date"),
                        s.get("title", ""),
                        _json.dumps(detail, ensure_ascii=False),
                        float(s.get("confidence", 0) or 0),
                        notified_by_key.get(key) if key is not None else None,
                    ),
                )
        logger.debug("SwingStore 写入 %d 条趋势信号 (date=%s)", len(signals), date)

    def get_recent_signals(self, n: int = 50) -> List[Dict]:
        """返回最近 n 条趋势信号，按时间倒序。``detail`` 解析回 dict。"""
        import json as _json

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trend_signal ORDER BY date DESC, id DESC LIMIT ?",
                (int(n),),
            ).fetchall()
        out: List[Dict] = []
        for r in rows:
            try:
                detail = _json.loads(r["detail"]) if r["detail"] else {}
            except (ValueError, TypeError):
                detail = {}
            out.append(
                {
                    "date": r["date"],
                    "code": r["code"],
                    "name": r["name"],
                    "signal_type": r["signal_type"],
                    "period": r["period"],
                    "data_date": r["data_date"] if "data_date" in r.keys() else detail.get("data_date"),
                    "title": r["title"],
                    "detail": detail,
                    "confidence": r["confidence"],
                    "notified": bool(r["notified_at"]) if "notified_at" in r.keys() else False,
                }
            )
        return out

    @staticmethod
    def _row_to_slice(row: sqlite3.Row) -> MacroSlice:
        import json as _json

        try:
            extra = _json.loads(row["extra"]) if "extra" in row.keys() and row["extra"] else {}
        except (ValueError, TypeError):
            extra = {}
        return MacroSlice(
            date=row["date"],
            total_amount=row["total_amount"],
            margin_balance=row["margin_balance"],
            margin_growth_pct=row["margin_growth_pct"],
            limit_up=row["limit_up"],
            limit_down=row["limit_down"],
            limit_ratio=row["limit_ratio"],
            source=row["source"] or "",
            extra=extra,
        )

    def as_dict(self, slice_: MacroSlice) -> Dict:
        return asdict(slice_)

    @staticmethod
    def today_str() -> str:
        """北京时间的当日日期（UTC 容器里 naive now() 会提前/滞后换日）。"""
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
