"""Read-only 15:50 EOD daily report. Not a profitability certificate.

Reporting scope only. The qualified table mirrors the effectiveness card's
admission rule, so decisions that the card excludes are listed as context and
never mixed into the headline numbers. No database, state-machine or alert
threshold is changed here.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from datetime import time as daytime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
DAILY_REPORT_CUTOFF = daytime(15, 50)
# Same horizon set and admission rule as the effectiveness card.
HORIZONS = (1, 3, 5, 10, 20)
DECISION_TIME = daytime(15, 30)
DECISION_WINDOW_EARLY_MINUTES = 5
OOS_SPLITS = frozenset({"live_oos", "walk_forward_oos"})
RUN_STATUSES = frozenset({"running", "success", "partial", "failed", "skipped"})
EXCLUSIONS = ("run_not_success", "outside_decision_window", "non_oos", "invalid_record")
STATES = ("no_trigger", "allowed", "blocked", "data_failed", "missing", "unknown")
FACTORS = ("total_amount", "margin_balance", "margin_growth_pct", "limit_ratio")


def is_daily_report_slot(now: datetime) -> bool:
    local = now if now.tzinfo else now.replace(tzinfo=BEIJING)
    local = local.astimezone(BEIJING)
    return local.hour * 60 + local.minute >= DAILY_REPORT_CUTOFF.hour * 60 + DAILY_REPORT_CUTOFF.minute


def notification_plan(result: Dict[str, Any], now: datetime) -> str:
    """15:50 sends the single daily report; earlier slots only page unexpected failures."""
    if is_daily_report_slot(now):
        return "daily"
    if result.get("ok"):
        return "none"
    return "failure"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _connect_readonly(store_file: str) -> sqlite3.Connection:
    path = Path(store_file).expanduser().resolve()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _counts_from_run(run: Optional[Dict[str, Any]], rows: List[sqlite3.Row]) -> Dict[str, int]:
    counts = {key: 0 for key in STATES}
    counts.update({"evaluated": 0, "failed": 0, "targets": 0, "signals": 0, "allowed_signals": 0, "blocked_signals": 0})
    if run:
        counts["targets"] = int(run.get("target_count") or 0)
        counts["evaluated"] = int(run.get("evaluated_count") or 0)
        counts["failed"] = int(run.get("failed_count") or 0)
        counts["signals"] = int(run.get("signal_count") or 0)
        counts["allowed_signals"] = int(run.get("allowed_signal_count") or 0)
        counts["blocked_signals"] = int(run.get("blocked_signal_count") or 0)
    for row in rows:
        state = str(row["decision_state"] or "unknown")
        if state not in counts:
            state = "unknown"
        counts[state] = int(row["n"] or 0)
    return counts


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


# --------------------------------------------------------------------------- #
# Qualified-sample partition (mirrors dashboard effectiveness admission rules)
# --------------------------------------------------------------------------- #
def _parse_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING)


def _parse_day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def decision_window_eligible(
    decision_date: Any, observed_at: Any, decision_time: daytime = DECISION_TIME
) -> tuple[bool, str]:
    """Mirror the card's scheduled-window rule. Read-only classification."""
    decision_day = _parse_day(decision_date)
    observed = _parse_timestamp(observed_at)
    if decision_day is None or observed is None:
        return False, "invalid_time"
    observed = observed.astimezone(BEIJING)
    if observed.date() != decision_day:
        return False, "date_mismatch"
    scheduled = datetime.combine(decision_day, decision_time, tzinfo=BEIJING)
    if observed < scheduled - timedelta(minutes=DECISION_WINDOW_EARLY_MINUTES):
        return False, "before_decision_window"
    return True, "scheduled_window"


def classify_candidate(row: sqlite3.Row, today: date) -> Optional[str]:
    """Return an exclusion reason, or None when the record is eligible."""
    decision_day = _parse_day(row["decision_date"])
    if decision_day is None:
        return "invalid_record"
    decision_id = str(row["decision_id"] or "")
    run_id = str(row["run_id"] or "")
    if (
        not decision_id
        or not run_id
        or str(row["joined_run_id"] or "") != run_id
        or not str(row["code"] or "")
        or type(row["raw_trigger_count"]) is not int
        or int(row["raw_trigger_count"]) <= 0
        or decision_day > today
        or str(row["run_status"] or "") not in RUN_STATUSES
    ):
        return "invalid_record"
    observed = row["decision_at"] or row["run_started_at"] or ""
    eligible, reason = decision_window_eligible(row["decision_date"], observed)
    if reason == "invalid_time":
        return "invalid_record"
    if str(row["run_status"]) != "success":
        return "run_not_success"
    if not eligible:
        return "outside_decision_window"
    if str(row["validation_split"] or "") not in OOS_SPLITS:
        return "non_oos"
    return None


def _mature_outcome(row: sqlite3.Row, decision_date: date, today: date) -> bool:
    """Mirror the card's mature rule: filled, finite net, dated in the window."""
    evaluated = _parse_day(row["evaluation_as_of"])
    if evaluated is None or not decision_date <= evaluated <= today:
        return False
    if str(row["fill_status"] or "") != "filled":
        return False
    net = row["net_return"]
    if _finite(net) is None:
        return False
    for key in ("excess_return", "mfe", "mae"):
        value = row[key]
        if value is not None and _finite(value) is None:
            return False
    return True


def _horizon_stats(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    values = lambda key: [float(row[key]) for row in rows if _finite(row[key]) is not None]  # noqa: E731
    nets = values("net_return")
    excess = values("excess_return")
    fees = values("fees_return")
    slippage = values("slippage_return")
    mfe = values("mfe")
    mae = values("mae")
    mean = lambda items: (sum(items) / len(items)) if items else None  # noqa: E731
    return {
        "n": len(rows),
        "wins": sum(1 for value in nets if value > 0),
        "win_rate": (sum(1 for value in nets if value > 0) / len(nets)) if nets else None,
        "avg_net": mean(nets),
        "avg_excess": mean(excess),
        "avg_fees": mean(fees),
        "avg_slippage": mean(slippage),
        "avg_mfe": mean(mfe),
        "avg_mae": mean(mae),
    }


def research_partition(connection: sqlite3.Connection, *, now: datetime) -> Dict[str, Any]:
    """Split records into qualified samples and context-only categories."""
    today = now.astimezone(BEIJING).date()
    candidates = connection.execute(
        "SELECT substr(d.decision_id,1,129) AS decision_id, substr(d.run_id,1,129) AS run_id, "
        "substr(r.run_id,1,129) AS joined_run_id, substr(d.code,1,129) AS code, "
        "substr(d.decision_date,1,129) AS decision_date, substr(d.decision_at,1,129) AS decision_at, "
        "substr(d.validation_split,1,129) AS validation_split, d.raw_trigger_count, "
        "substr(r.status,1,129) AS run_status, substr(r.started_at,1,129) AS run_started_at "
        "FROM eod_target_decision d LEFT JOIN swing_eod_run r ON r.run_id=d.run_id "
        "WHERE d.raw_trigger_count > 0 AND d.decision_state='allowed'"
    ).fetchall()

    excluded = {key: 0 for key in EXCLUSIONS}
    partial_excluded = 0
    eligible: List[str] = []
    for row in candidates:
        reason = classify_candidate(row, today)
        if reason is None:
            eligible.append(str(row["decision_id"]))
            continue
        excluded[reason] += 1
        if reason == "run_not_success" and str(row["run_status"] or "") == "partial":
            partial_excluded += 1

    horizons = {str(horizon): {"n": 0, "wins": 0, "win_rate": None, "avg_net": None,
                               "avg_excess": None, "avg_fees": None, "avg_slippage": None,
                               "avg_mfe": None, "avg_mae": None} for horizon in HORIZONS}
    codes: set[str] = set()
    mature_decisions = 0
    if eligible:
        placeholders = ",".join("?" * len(eligible))
        decisions = {
            str(row["decision_id"]): row
            for row in connection.execute(
                "SELECT decision_id, code, decision_date FROM eod_target_decision "
                f"WHERE decision_id IN ({placeholders})",
                eligible,
            )
        }
        rows_by_horizon: Dict[int, List[sqlite3.Row]] = {horizon: [] for horizon in HORIZONS}
        seen_decisions: Dict[str, set] = {}
        for row in connection.execute(
            "SELECT decision_id, horizon_sessions, evaluation_as_of, fill_status, "
            "net_return, excess_return, fees_return, slippage_return, mfe, mae "
            "FROM eod_decision_outcome "
            f"WHERE decision_id IN ({placeholders}) AND horizon_sessions IN "
            f"({','.join('?' * len(HORIZONS))}) "
            "ORDER BY decision_id, horizon_sessions",
            (*eligible, *HORIZONS),
        ):
            decision_id = str(row["decision_id"])
            decision = decisions.get(decision_id)
            decision_day = _parse_day(decision["decision_date"]) if decision else None
            if decision_day is None or not _mature_outcome(row, decision_day, today):
                continue
            horizon = int(row["horizon_sessions"])
            rows_by_horizon[horizon].append(row)
            seen_decisions.setdefault(decision_id, set()).add(horizon)
        mature_decisions = len(seen_decisions)
        for horizon in HORIZONS:
            horizons[str(horizon)] = _horizon_stats(rows_by_horizon[horizon])
        codes = {str(decisions[decision_id]["code"]) for decision_id in seen_decisions}

    context = {}
    for state, label in (("no_trigger", "control_no_trigger"), ("data_failed", "data_failed")):
        row = connection.execute(
            "SELECT COUNT(DISTINCT d.decision_id) AS decisions, COUNT(o.outcome_id) AS horizon_rows "
            "FROM eod_target_decision d LEFT JOIN eod_decision_outcome o ON o.decision_id=d.decision_id "
            "WHERE d.decision_state=?",
            (state,),
        ).fetchone()
        context[label] = {"decisions": int(row["decisions"] or 0), "horizon_rows": int(row["horizon_rows"] or 0)}

    return {
        "schema": "quant.eod-daily-qualified.v1",
        "rule": "allowed + raw_trigger_count>0 + run_status=success + scheduled window + OOS split",
        "qualified": {
            "records": len(eligible),
            "mature_records": mature_decisions,
            "codes": sorted(codes),
            "horizons": horizons,
        },
        "not_counted": {
            "excluded_records": {key: excluded[key] for key in EXCLUSIONS},
            "macro_partial_excluded": partial_excluded,
            **context,
        },
        "note": "同一决策的多个持有期不是多笔交易；n 为该持有期已成熟的合格决策数。"
                "对照/数据失败仅作上下文，不计入有效性样本。",
    }


# --------------------------------------------------------------------------- #
# Run reading (separates market data, macro factors, signals, research grade)
# --------------------------------------------------------------------------- #
def _first_line(value: Any, limit: int) -> str:
    """First non-blank line of a free-text message; "" when there is none."""
    for line in str(value if value is not None else "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _macro_missing(connection: sqlite3.Connection, trade_date: str) -> List[str]:
    row = connection.execute(
        "SELECT extra FROM macro_slice WHERE date=?", (trade_date,)
    ).fetchone()
    if row is None:
        return ["当日宏观切片缺失"]
    try:
        extra = json.loads(row["extra"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    collect = extra.get("collect_status") if isinstance(extra, dict) else {}
    if not isinstance(collect, dict):
        return []
    missing = []
    for factor in FACTORS:
        status = collect.get(factor)
        if not isinstance(status, dict):
            continue
        if status.get("available") is True and not status.get("stale") and not status.get("anomalous"):
            continue
        reason = _first_line(status.get("message") or "", 60)
        if not reason:
            # An empty collector message must still be reported as a visible gap,
            # never crash the report (2026-10-08: "".splitlines()[0] -> IndexError).
            if status.get("available") is not True:
                reason = "不可用"
            elif status.get("stale"):
                observed = str(status.get("observed_date") or "").strip()[:10]
                reason = f"数据过期（观测日 {observed}）" if observed else "数据过期"
            elif status.get("anomalous"):
                reason = "数据异常"
        source = str(status.get("source") or "unknown")[:40]
        detail = f"（{source}{'：' + reason if reason else ''}）"
        missing.append(f"{factor}{detail}")
    return missing


def _decode_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value or "null") if value.strip() else default
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def _signal_reading(raw: int, dedup: int, retained: int) -> str:
    if raw <= 0:
        return "信号：无触发"
    if retained <= 0:
        return f"信号：已有触发，但去重后无新增（原始检测{raw} / 去重{dedup} / 新增留存0）"
    return f"信号：新增留存{retained} 条（原始检测{raw} / 去重{dedup}）"


def run_reading(
    connection: sqlite3.Connection, run: Dict[str, Any], counts: Dict[str, int], trade_date: str
) -> Dict[str, Any]:
    targets = int(counts.get("targets") or 0)
    evaluated = int(counts.get("evaluated") or 0)
    failed = int(counts.get("failed") or 0)
    missing = _macro_missing(connection, trade_date)

    # The audit row is authoritative; the caller's dict may omit details.
    run_id = str(run.get("run_id") or "")
    row = None
    if run_id:
        row = connection.execute(
            "SELECT status, reason_codes, details, signal_count FROM swing_eod_run WHERE run_id=?",
            (run_id,),
        ).fetchone()
    status = str((row["status"] if row is not None else run.get("status")) or "")
    reasons = _decode_json(row["reason_codes"] if row is not None else run.get("reason_codes"), [])
    details = _decode_json(row["details"] if row is not None else run.get("details"), {})
    retained = int((row["signal_count"] if row is not None else run.get("signal_count")) or 0)
    if not isinstance(reasons, list):
        reasons = [reasons] if reasons else []
    reasons = [str(item) for item in reasons if item]
    if not isinstance(details, dict):
        details = {}
    raw = int(details.get("raw_detected_signal_count") or 0)
    dedup = int(details.get("deduplicated_signal_count") or 0)

    market = f"标的行情：{evaluated}/{targets} 合格" + (f"，失败 {failed} 只" if failed else "")
    macro = "宏观数据：完整" if not missing else "宏观数据：部分缺失 " + "；".join(missing)
    if status == "success":
        research = "研究资格：批次成功；符合准入条件的记录才计入有效样本"
    else:
        detail = "、".join(reasons) if reasons else status or "unknown"
        research = f"研究资格：因批次完整性要求（{status or 'unknown'}：{detail}），未计入有效样本"
    return {
        "market_data": market,
        "macro": macro,
        "macro_missing": missing,
        "signals": _signal_reading(raw, dedup, retained),
        "research_eligibility": research,
        "run_status": status,
        "reason_codes": reasons,
    }


def _conclusion(
    result: Dict[str, Any], qualified: Dict[str, Any], reading: Dict[str, Any]
) -> str:
    if not result.get("ok"):
        return "当日未完成终态，不支持推荐。"
    horizons = qualified.get("horizons") or {}
    nets = [item["avg_net"] for item in horizons.values() if item.get("n") and item.get("avg_net") is not None]
    if not nets:
        return "暂无可评价样本（合格成熟记录 0 条）；不支持盈利或推荐结论。"
    if all(value < 0 for value in nets):
        return "合格样本平均净收益为负，不支持盈利结论；单批描述性结果不构成实盘证明。"
    return "合格样本为描述性结果，不代表盈利证明；尚不足以支持策略晋升。"


# --------------------------------------------------------------------------- #
def load_daily_report_payload(store_file: str, result: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    trade_date = now.astimezone(BEIJING).date().isoformat()
    attempts = result.get("attempts") or {}
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    payload: Dict[str, Any] = {
        "schema_version": 2,
        "report_type": "eod_daily_report",
        "trade_date": trade_date,
        "generated_at": now.astimezone(BEIJING).isoformat(timespec="seconds"),
        "terminal_ok": bool(result.get("ok")),
        "terminal_reason": str(result.get("reason") or ""),
        "message": str(result.get("message") or ""),
        "attempts": attempts,
        "counts": _counts_from_run(run, []),
        "today_trend_signals": 0,
        "macro": None,
        "reading": None,
        "research": None,
        "execution_digest": {
            "status": "stub",
            "fills": 0,
            "pnl": None,
            "note": "order routing stub; fills/pnl 未接线，不能当作 0 收益",
        },
        "fee_basis": "eod_decision_outcome.net_return 已含 fees_return 与 slippage_return；基准 510300；非实盘账户",
        "conclusion": "当日证据不完整，不支持推荐。",
    }
    try:
        connection = _connect_readonly(store_file)
    except sqlite3.Error:
        payload["conclusion"] = "证据库不可读，不支持推荐。"
        return payload
    try:
        run_id = str(run.get("run_id") or "")
        state_rows = []
        if run_id:
            state_rows = connection.execute(
                "SELECT decision_state, COUNT(*) AS n FROM eod_target_decision "
                "WHERE run_id=? GROUP BY 1",
                (run_id,),
            ).fetchall()
        payload["counts"] = _counts_from_run(run, state_rows)
        payload["today_trend_signals"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM trend_signal WHERE date=?", (trade_date,)
            ).fetchone()[0]
        )
        macro = connection.execute(
            "SELECT date, total_amount, limit_up, limit_down, source, extra "
            "FROM macro_slice WHERE date=?",
            (trade_date,),
        ).fetchone()
        if macro is not None:
            extra = {}
            try:
                extra = json.loads(macro["extra"] or "{}")
            except (TypeError, ValueError):
                extra = {}
            collect = extra.get("collect_status") if isinstance(extra, dict) else {}
            total_status = collect.get("total_amount") if isinstance(collect, dict) else {}
            payload["macro"] = {
                "date": macro["date"],
                "total_amount": _finite(macro["total_amount"]),
                "limit_up": macro["limit_up"],
                "limit_down": macro["limit_down"],
                "source": macro["source"],
                "total_amount_source": (
                    total_status.get("source") if isinstance(total_status, dict) else None
                ),
            }
        payload["research"] = research_partition(connection, now=now)
        payload["reading"] = run_reading(connection, run, payload["counts"], trade_date)
        payload["conclusion"] = _conclusion(result, payload["research"]["qualified"], payload["reading"])
        return payload
    except sqlite3.Error:
        payload["conclusion"] = "证据库查询失败，不支持推荐。"
        return payload
    finally:
        connection.close()


def format_daily_report(payload: Dict[str, Any]) -> str:
    attempts = payload.get("attempts") or {}
    first = attempts.get("first") or {}
    latest = attempts.get("latest") or {}
    counts = payload.get("counts") or {}
    macro = payload.get("macro")
    reading = payload.get("reading") or {}
    research = payload.get("research") or {}
    qualified = research.get("qualified") or {}
    not_counted = research.get("not_counted") or {}
    excluded = not_counted.get("excluded_records") or {}
    lines = [
        f"EOD 日报 {payload.get('trade_date')} 15:50",
        f"终态：{'成功' if payload.get('terminal_ok') else '未完成'} / {payload.get('terminal_reason')}",
        (
            f"首次：{first.get('status')}/{first.get('decision_outcome')} @ {first.get('completed_at')}"
            if first else "首次：无审计"
        ),
        (
            f"最终：{latest.get('status')}/{latest.get('decision_outcome')} @ {latest.get('completed_at')}"
            if latest else "最终：无审计"
        ),
        "运行解读",
    ]
    for key in ("market_data", "macro", "signals", "research_eligibility"):
        if reading.get(key):
            lines.append(f"- {reading[key]}")
    lines.append(
        f"逐标的：未触发{counts.get('no_trigger', 0)} 含放行{counts.get('allowed', 0)} "
        f"全拦截{counts.get('blocked', 0)} 数据失败{counts.get('data_failed', 0)}"
    )
    lines.append("合格记录（与有效性卡片同口径：allowed+有触发+批次成功+窗口/OOS）")
    if int(qualified.get("mature_records") or 0) == 0:
        lines.append("- 暂无可评价样本")
    else:
        for horizon in HORIZONS:
            item = (qualified.get("horizons") or {}).get(str(horizon)) or {}
            if item.get("n"):
                lines.append(
                    f"- T+{horizon}：n={item['n']} 胜率{_pct(item.get('win_rate'))} "
                    f"净{_pct(item.get('avg_net'))} 超额{_pct(item.get('avg_excess'))}"
                )
            else:
                lines.append(f"- T+{horizon}：暂无成熟记录")
        lines.append(
            f"- 合格{qualified.get('records', 0)} 条 / 已成熟{qualified.get('mature_records', 0)} 条"
            f"（标的 {len(qualified.get('codes') or [])} 只）"
        )
    if research.get("note"):
        lines.append(f"- {research['note']}")
    lines.append("不计入有效性样本（仅作上下文）")
    lines.append(
        f"- 未触发对照：{not_counted.get('control_no_trigger', {}).get('decisions', 0)} 条决策"
        f"（horizon 行 {not_counted.get('control_no_trigger', {}).get('horizon_rows', 0)}）"
    )
    lines.append(f"- 宏观缺失导致批次不完整：{not_counted.get('macro_partial_excluded', 0)} 条")
    lines.append(
        f"- 数据失败：{not_counted.get('data_failed', {}).get('decisions', 0)} 条决策"
        f"（horizon 行 {not_counted.get('data_failed', {}).get('horizon_rows', 0)}）"
    )
    lines.append(
        f"- 其他排除：窗口 {excluded.get('outside_decision_window', 0)} / "
        f"非OOS {excluded.get('non_oos', 0)} / 无效 {excluded.get('invalid_record', 0)} / "
        f"批次失败 {max(0, excluded.get('run_not_success', 0) - int(not_counted.get('macro_partial_excluded') or 0))}"
    )
    if macro:
        source = macro.get("total_amount_source") or macro.get("source") or "unknown"
        lines.append(
            f"宏观切片：涨停{macro.get('limit_up')} 跌停{macro.get('limit_down')} "
            f"成交额{macro.get('total_amount')} 来源 {source}"
        )
    else:
        lines.append("宏观切片：当日缺失")
    digest = payload.get("execution_digest") or {}
    lines.append(f"口径：{payload.get('fee_basis')}")
    lines.append(
        f"成交接线：{digest.get('status')}；fills={digest.get('fills')}；pnl={digest.get('pnl')}；{digest.get('note')}"
    )
    lines.append(f"结论：{payload.get('conclusion')}")
    return "\n".join(lines)


def persist_daily_report(store_file: str, payload: Dict[str, Any]) -> Path:
    path = Path(store_file).expanduser().resolve().parent / "eod_daily_report.json"
    raw = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".eod_daily_report.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def build_daily_report(store_file: str, result: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    payload = load_daily_report_payload(store_file, result, now)
    payload["text"] = format_daily_report(payload)
    return payload
