"""M4.4 Swing EOD watchdog and 15:50 daily report (SHADOW).

Two read-only inspections per trading day:

* 15:40 - the latest run is missing, still ``running`` beyond 8 minutes, or already in
  a non-success terminal state.  The expected data gap of the 15:30 attempt is deferred
  until the 15:48 retry, exactly like the legacy watchdog; an unexpected failure pages
  immediately.
* 15:50 - the single daily report is produced from the same terminal inspection.  It
  carries the terminal status, first/latest attempts, per-target state counts, the macro
  slice, a conservative validity card and a conclusion that always says "不支持推荐".

Notification mode is controlled by ``QP_SIGNALS_NOTIFY_MODE``.  In ``shadow`` the rows
are inserted into ``notify.outbox`` with ``mode=shadow`` and the legacy namespaces
``watchdog:{date}:failure`` / ``watchdog:{date}:daily``; no Telegram token, chat id or
delivery attempt is used.  In ``live`` the same rows are inserted with ``mode=live``
and are delivered by ``notify.send`` under its kill switch/retry policy.

Contract note: 3.6 literally pages a 15:40 run that has been ``running`` for more than
8 minutes.  The legacy ``defer_until_retry`` keeps both ``running`` and
``running_timeout`` quiet until 15:48; C7 compares qp against the legacy outbox, so this
module follows the legacy deferral and records the divergence rather than inventing an
extra 15:40 page.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.models.market import IndexClose
from qp.models.signals import (DecisionOutcome, EodRun, MacroSlice, RiskSnapshot, RiskState,
                               TargetDecision, TrendSignal)
from qp.signals.eod import CST, now_cst, trading_day_status

RETRY_ALERT_CUTOFF = dt.time(15, 48)
DAILY_REPORT_CUTOFF = dt.time(15, 50)
MAX_RUNNING_MINUTES = 8
VALIDITY_MIN_SAMPLES = 30
VALIDITY_HORIZON = 20
MACRO_FACTORS = ("total_amount", "margin_balance", "margin_growth_pct", "limit_ratio")
DECISION_STATES = ("no_trigger", "allowed", "blocked", "data_failed", "missing", "unknown")


def _normalise_now(now: dt.datetime | None) -> dt.datetime:
    current = now or now_cst()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    return current.astimezone(CST)


def _iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(CST).isoformat() if value is not None else None


def _run_view(run: EodRun) -> dict[str, Any]:
    """Full run view used for deferral decisions."""
    return {
        "run_id": run.run_id,
        "decision_date": run.decision_date.isoformat(),
        "status": run.status,
        "decision_outcome": run.decision_outcome,
        "reason_codes": list(run.reason_codes or []),
        "target_count": run.target_count,
        "evaluated_count": run.evaluated_count,
        "failed_count": run.failed_count,
        "signal_count": run.signal_count,
        "allowed_signal_count": run.allowed_signal_count,
        "blocked_signal_count": run.blocked_signal_count,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.finished_at),
    }


def _attempt_view(run: EodRun) -> dict[str, Any]:
    view = _run_view(run)
    for key in ("reason_codes", "target_count", "started_at", "allowed_signal_count",
                "blocked_signal_count"):
        view.pop(key, None)
    return view


def summarize_attempts(rows: list[EodRun]) -> dict[str, Any]:
    """Describe the first 15:30 attempt vs the latest terminal run; rows are newest-first."""
    if not rows:
        return {"attempts": 0, "first": None, "latest": None,
                "recovered_after_data_gap": False}
    latest, first = rows[0], rows[-1]
    first_view, latest_view = _attempt_view(first), _attempt_view(latest)
    recovered = (
        (str(first.status or "") == "failed"
         or (retryable_data_failure(_run_view(first),
                                    decision_date=latest.decision_date.isoformat())
             and latest.run_id != first.run_id))
        and str(first.decision_outcome or "") == "data_unavailable"
        and str(latest.status or "") == "success"
    )
    return {"attempts": len(rows), "first": first_view, "latest": latest_view,
            "recovered_after_data_gap": recovered}


def retryable_data_failure(run: dict[str, Any], *, decision_date: str | None = None) -> bool:
    """Classify an expected data gap that the same-day retry may still recover."""
    if str(run.get("decision_outcome") or "") != "data_unavailable":
        return False
    status = str(run.get("status") or "")
    if status == "failed":
        return True
    if decision_date is not None and str(run.get("decision_date") or "") != decision_date:
        return False
    if status != "partial" or decision_date is None:
        return False
    return all(type(run.get(key)) is int and run.get(key) == 0
               for key in ("signal_count", "allowed_signal_count", "blocked_signal_count"))


def latest_retryable_run(session: Session, decision_date: dt.date) -> tuple[EodRun | None, bool]:
    """Return the newest run and whether the same-day 15:48 retry may run.

    Mirrors the legacy ``check_swing_eod_retry.py`` ExecCondition: never start a
    first run, and only retry a same-day ``data_unavailable`` gap whose signal
    counts are all zero.
    """
    latest = session.execute(
        select(EodRun).where(EodRun.decision_date == decision_date)
        .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())).scalars().first()
    if latest is None:
        return None, False
    return latest, retryable_data_failure(_run_view(latest),
                                          decision_date=decision_date.isoformat())


def inspect_terminal_audit(session: Session, *, decision_date: dt.date,
                           now: dt.datetime, max_running_minutes: int = MAX_RUNNING_MINUTES) -> dict[str, Any]:
    rows = list(session.execute(
        select(EodRun).where(EodRun.decision_date == decision_date)
        .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())).scalars())
    attempts = summarize_attempts(rows)
    if not rows:
        return {"ok": False, "reason": "missing",
                "message": "当天没有任何 EOD 审计记录", "attempts": attempts, "run": None}
    latest = rows[0]
    status = str(latest.status or "")
    if status == "running":
        try:
            started = latest.started_at
            assert started is not None
            age_minutes = max(0.0, (now - started).total_seconds() / 60)
        except (TypeError, ValueError, AssertionError):
            age_minutes = float(max_running_minutes)
        reason = "running_timeout" if age_minutes >= max_running_minutes else "running"
        message = (f"EOD 仍为 running，已持续 {age_minutes:.1f} 分钟"
                   if reason == "running_timeout"
                   else f"EOD 尚未形成终态，已运行 {age_minutes:.1f} 分钟")
        return {"ok": False, "reason": reason, "message": message,
                "run": _run_view(latest), "attempts": attempts}
    if status != "success":
        return {
            "ok": False,
            "reason": status or "invalid_status",
            "message": (f"EOD 终态为 {status or 'unknown'}，"
                        f"outcome={latest.decision_outcome}, "
                        f"reasons={list(latest.reason_codes or [])}"),
            "run": _run_view(latest),
            "attempts": attempts,
        }
    decisions = session.scalar(
        select(func.count()).select_from(TargetDecision)
        .where(TargetDecision.run_id == latest.run_id)) or 0
    snapshots = session.scalar(
        select(func.count()).select_from(RiskSnapshot)
        .where(RiskSnapshot.run_id == latest.run_id)) or 0
    if decisions != int(latest.target_count or 0) or snapshots != 1:
        return {
            "ok": False,
            "reason": "evidence_incomplete",
            "message": (f"运行虽为 success，但不可变证据不完整：逐标的 "
                        f"{decisions}/{latest.target_count}，风控快照 {snapshots}/1"),
            "run": _run_view(latest),
            "attempts": attempts,
        }
    return {
        "ok": True,
        "reason": "success",
        "message": (f"EOD success，逐标的证据 {decisions}/{latest.target_count}，"
                    f"outcome={latest.decision_outcome}"),
        "run": _run_view(latest),
        "attempts": attempts,
    }


def defer_until_retry(result: dict[str, Any], now: dt.datetime) -> bool:
    """True when 15:40 is looking at the expected 15:30 gap (retry runs at 15:48)."""
    if result.get("ok"):
        return False
    local = _normalise_now(now)
    if local.time() >= RETRY_ALERT_CUTOFF:
        return False
    run = result.get("run") or {}
    if result.get("reason") in {"running", "running_timeout"}:
        return str(run.get("status") or "") == "running"
    if result.get("reason") not in {"failed", "partial"}:
        return False
    if (result.get("reason") != run.get("status")
            or not retryable_data_failure(run, decision_date=local.date().isoformat())):
        return False
    expected = "trend_data_partial" if run.get("status") == "partial" else "trend_data_unavailable"
    return expected in [str(item) for item in (run.get("reason_codes") or [])]


def notification_plan(result: dict[str, Any], now: dt.datetime) -> str:
    """15:50 is the single daily report; earlier slots only page unexpected failures."""
    if _normalise_now(now).time() >= DAILY_REPORT_CUTOFF:
        return "daily"
    return "none" if result.get("ok") else "failure"


def _first_line(value: Any, limit: int) -> str:
    for line in str(value if value is not None else "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _macro_missing(collect_status: Any) -> list[str]:
    if not isinstance(collect_status, dict):
        return []
    missing: list[str] = []
    for factor in MACRO_FACTORS:
        status = collect_status.get(factor)
        if not isinstance(status, dict):
            continue
        if status.get("available") is True and not status.get("stale") and not status.get("anomalous"):
            continue
        reason = _first_line(status.get("message") or "", 60)
        if not reason:
            if status.get("available") is not True:
                reason = "不可用"
            elif status.get("stale"):
                observed = str(status.get("observed_date") or "").strip()[:10]
                reason = f"数据过期（观测日 {observed}）" if observed else "数据过期"
            elif status.get("anomalous"):
                reason = "数据异常"
        source = str(status.get("source") or "unknown")[:40]
        missing.append(f"{factor}（{source}{'：' + reason if reason else ''}）")
    return missing


def _signal_reading(raw: int, dedup: int, retained: int) -> str:
    if raw <= 0:
        return "信号：无触发"
    if retained <= 0:
        return f"信号：已有触发，但去重后无新增（原始检测{raw} / 去重{dedup} / 新增留存0）"
    return f"信号：新增留存{retained} 条（原始检测{raw} / 去重{dedup}）"


def _outcome_rows(session: Session, state: str) -> list[tuple[float, float]]:
    """Allowed/blocked raw-signal decisions with a mature, filled 20-session outcome."""
    rows = session.execute(
        select(DecisionOutcome.net_return, DecisionOutcome.excess_return)
        .join(TargetDecision, DecisionOutcome.decision_id == TargetDecision.id)
        .join(EodRun, TargetDecision.run_id == EodRun.run_id)
        .where(TargetDecision.decision_state == state,
               TargetDecision.raw_trigger_count > 0,
               TargetDecision.validation_split.in_(("live_oos", "walk_forward_oos")),
               EodRun.status == "success",
               DecisionOutcome.horizon_sessions == VALIDITY_HORIZON,
               DecisionOutcome.fill_status == "filled",
               DecisionOutcome.net_return.is_not(None))).all()
    return [(float(net), float(excess) if excess is not None else math.nan)
            for net, excess in rows]


def validity_card(session: Session) -> dict[str, Any]:
    """Conservative effectiveness card; exact metrics stay hidden below 30 samples."""
    allowed = _outcome_rows(session, "allowed")
    blocked = _outcome_rows(session, "blocked")
    sample = len(allowed)
    nets = [net for net, _ in allowed]
    excess = [value for _, value in allowed if not math.isnan(value)]
    if sample < VALIDITY_MIN_SAMPLES:
        label = "样本不足"
    else:
        mean_net = statistics.fmean(nets)
        mean_excess = statistics.fmean(excess) if excess else 0.0
        label = "未见有效" if mean_net <= 0 or mean_excess <= 0 else "有效性不确定"
    metrics = None
    if sample >= VALIDITY_MIN_SAMPLES:
        metrics = {
            "mean_net": round(statistics.fmean(nets), 8),
            "mean_excess": round(statistics.fmean(excess), 8) if excess else None,
            "win_rate": round(sum(1 for value in nets if value > 0) / sample, 6),
        }
    blocked_returns = [net for net, _ in blocked]
    return {
        "label": label,
        "sample_size": sample,
        "minimum_samples": VALIDITY_MIN_SAMPLES,
        "primary_horizon_sessions": VALIDITY_HORIZON,
        "method": "live_oos_or_walk_forward_oos",
        "metrics": metrics,
        "risk_counterfactual": {
            "sample_size": len(blocked_returns),
            "prevented_loss": round(sum(max(-value, 0.0) for value in blocked_returns), 8),
            "missed_upside": round(sum(max(value, 0.0) for value in blocked_returns), 8),
            "mean_blocked_return": (round(statistics.fmean(blocked_returns), 8)
                                    if blocked_returns else None),
        },
        "note": "同一决策的多个持有期不是多笔交易；不足 30 条不展示精确胜率。",
    }


def _counts(session: Session, run: dict[str, Any] | None) -> dict[str, int]:
    counts = {state: 0 for state in DECISION_STATES}
    counts.update({"evaluated": 0, "failed": 0, "targets": 0, "signals": 0,
                   "allowed_signals": 0, "blocked_signals": 0})
    if run:
        counts.update({
            "targets": int(run.get("target_count") or 0),
            "evaluated": int(run.get("evaluated_count") or 0),
            "failed": int(run.get("failed_count") or 0),
            "signals": int(run.get("signal_count") or 0),
            "allowed_signals": int(run.get("allowed_signal_count") or 0),
            "blocked_signals": int(run.get("blocked_signal_count") or 0),
        })
        for state, count in session.execute(
                select(TargetDecision.decision_state, func.count())
                .where(TargetDecision.run_id == int(run["run_id"]))
                .group_by(TargetDecision.decision_state)):
            counts[str(state) if str(state) in counts else "unknown"] = int(count)
    return counts


def _details(run: dict[str, Any] | None) -> dict[str, Any]:
    details = (run or {}).get("details")
    return details if isinstance(details, dict) else {}


INDEX_LABELS = {"sh000001": "上证", "sh000300": "沪深300",
                "sz399001": "深证成指", "sz399006": "创业板指"}


def _market_report(session: Session, trade_date: dt.date) -> dict[str, Any]:
    """Index closes and one-session changes for the human daily report."""
    rows = {row.symbol: row for row in session.scalars(
        select(IndexClose).where(IndexClose.trade_date == trade_date,
                                 IndexClose.origin == "qp",
                                 IndexClose.symbol.in_(tuple(INDEX_LABELS))))}
    indices = []
    for symbol, label in INDEX_LABELS.items():
        row = rows.get(symbol)
        if row is None:
            continue
        previous = session.scalar(
            select(IndexClose.close).where(
                IndexClose.symbol == symbol, IndexClose.origin == "qp",
                IndexClose.trade_date < trade_date)
            .order_by(IndexClose.trade_date.desc()).limit(1))
        close = float(row.close)
        change = (close / float(previous) - 1) if previous else None
        indices.append({"symbol": symbol, "label": label, "close": close,
                        "change": change})
    text = " | ".join(
        f"{item['label']} {item['close']:,.2f}"
        + (f"（{item['change'] * 100:+.2f}%）" if item["change"] is not None else "")
        for item in indices)
    return {"date": trade_date.isoformat(), "indices": indices, "text": text}


def _risk_report(session: Session) -> dict[str, Any] | None:
    row = session.scalar(select(RiskState).order_by(RiskState.observed_at.desc()).limit(1))
    if row is None:
        return None
    values = row.values if isinstance(row.values, dict) else {}
    return {"level": row.level, "message": row.message,
            "vix": values.get("vix"), "us_10y": values.get("us_10y"),
            "crude_oil": values.get("crude_oil"),
            "observed_at": _iso(row.observed_at)}


def _macro_line(macro: dict[str, Any] | None) -> str:
    if not macro:
        return "宏观：当日切片缺失"
    parts = []
    if macro.get("total_amount") is not None:
        parts.append(f"成交额 {macro['total_amount']:,.0f} 亿")
    if macro.get("margin_balance") is not None:
        growth = macro.get("margin_growth_pct")
        parts.append(f"两融 {macro['margin_balance']:,.0f} 亿"
                     + (f"（{growth:+.2f}%）" if growth is not None else ""))
    if macro.get("limit_up") is not None or macro.get("limit_down") is not None:
        parts.append(f"涨停 {macro.get('limit_up', '—')} / 跌停 {macro.get('limit_down', '—')}"
                     + (f"（比 {macro['limit_ratio']:.2f}）"
                        if macro.get("limit_ratio") is not None else ""))
    return "宏观：" + ("；".join(parts) if parts else "完整")


def _funnel_text(counts: dict[str, int], raw: int, dedup: int) -> str:
    return (f"信号漏斗：评估 {counts.get('evaluated', 0)}/{counts.get('targets', 0)}"
            f" · 触发 {raw} · 去重 {dedup} · 留存 {counts.get('signals', 0)}"
            f" · allowed {counts.get('allowed_signals', 0)}"
            f" · blocked {counts.get('blocked_signals', 0)}"
            f" · 数据失败 {counts.get('failed', 0)}")


def build_daily_report(session: Session, *, result: dict[str, Any],
                       now: dt.datetime) -> dict[str, Any]:
    """Structured 15:50 payload; text is rendered only for the shadow outbox body."""
    local = _normalise_now(now)
    trade_date = local.date()
    run = result.get("run") if isinstance(result.get("run"), dict) else None
    counts = _counts(session, run)
    macro_row = session.get(MacroSlice, trade_date)
    macro: dict[str, Any] | None = None
    missing: list[str]
    if macro_row is None:
        missing = ["当日宏观切片缺失"]
    else:
        collect = macro_row.collect_status or {}
        missing = _macro_missing(collect)
        total_status = collect.get("total_amount") if isinstance(collect, dict) else {}
        macro = {
            "date": macro_row.decision_date.isoformat(),
            "total_amount": (float(macro_row.total_amount)
                             if macro_row.total_amount is not None else None),
            "margin_balance": (float(macro_row.margin_balance)
                               if macro_row.margin_balance is not None else None),
            "margin_growth_pct": (float(macro_row.margin_growth_pct)
                                  if macro_row.margin_growth_pct is not None else None),
            "limit_up": macro_row.limit_up,
            "limit_down": macro_row.limit_down,
            "limit_ratio": (float(macro_row.limit_ratio)
                            if macro_row.limit_ratio is not None else None),
            "source": macro_row.source,
            "total_amount_source": (total_status.get("source")
                                    if isinstance(total_status, dict) else None),
        }
    status = str((run or {}).get("status") or "")
    reasons = [str(item) for item in ((run or {}).get("reason_codes") or []) if item]
    details = _details(run)
    raw = int(details.get("raw_detected_signal_count") or 0)
    dedup = int(details.get("deduplicated_signal_count") or 0)
    retained = int(counts.get("signals") or 0)
    market_data_text = (f"标的行情：{counts['evaluated']}/{counts['targets']} 合格"
                        + (f"，失败 {counts['failed']} 只" if counts["failed"] else ""))
    market_snapshot = _market_report(session, trade_date)
    risk = _risk_report(session)
    risk_text = "风险：未取得快照"
    if risk:
        risk_text = f"风险闸门：{risk['level']}"
        if risk.get("message"):
            risk_text += f"（{risk['message']}）"
        if risk.get("vix") is not None:
            risk_text += f"；VIX {float(risk['vix']):.2f}"
        if risk.get("us_10y") is not None:
            risk_text += f"；美债10Y {float(risk['us_10y']):.3f}%"
    macro_text = ("宏观数据：完整" if not missing
                  else "宏观数据：部分缺失 " + "；".join(missing))
    macro_line = _macro_line(macro)
    funnel = _funnel_text(counts, raw, dedup)
    card = validity_card(session)
    if status == "success":
        research_text = (f"研究资格：批次成功；合格成熟 {card['sample_size']} 条"
                         f"（{card['label']}，门槛 {card['minimum_samples']} 条）")
    else:
        research_text = (f"研究资格：因批次完整性要求（{status or 'unknown'}："
                         f"{'、'.join(reasons) if reasons else status or 'unknown'}），未计入有效样本")
    today_signals = session.scalar(
        select(func.count()).select_from(TrendSignal)
        .where(TrendSignal.decision_date == trade_date)) or 0
    terminal_ok = bool(result.get("ok"))
    if not terminal_ok:
        conclusion = "当日未完成终态，不支持推荐。"
    elif not counts.get("allowed_signals"):
        conclusion = ("今日无 allowed 信号"
                      + (f"（风险闸门 {risk['level']}）" if risk else "")
                      + "；描述性结果，不支持推荐。")
    elif not card["sample_size"]:
        conclusion = "暂无可评价样本（合格成熟记录 0 条）；描述性结果，不支持推荐。"
    else:
        conclusion = "合格样本为描述性结果，不支持推荐；尚不足以支持策略晋升。"
    return {
        "schema_version": 2,
        "report_type": "eod_daily_report",
        "trade_date": trade_date.isoformat(),
        "generated_at": local.isoformat(timespec="seconds"),
        "terminal_ok": terminal_ok,
        "terminal_reason": str(result.get("reason") or ""),
        "message": str(result.get("message") or ""),
        "attempts": result.get("attempts") or summarize_attempts([]),
        "counts": counts,
        "today_trend_signals": int(today_signals),
        "macro": macro,
        "market": market_snapshot,
        "risk": risk,
        "risk_text": risk_text,
        "macro_text": macro_line,
        "funnel": funnel,
        "reading": {
            "market_data": market_data_text,
            "macro": macro_text,
            "macro_missing": missing,
            "signals": _signal_reading(raw, dedup, retained),
            "funnel": funnel,
            "risk": risk_text,
            "research_eligibility": research_text,
            "run_status": status,
            "reason_codes": reasons,
        },
        "research": {
            "schema": "quant.eod-daily-qualified.v1",
            "rule": "allowed + raw_trigger_count>0 + run_status=success + OOS split",
            "qualified": card,
            "note": "对照/数据失败仅作上下文，不计入有效性样本。",
        },
        "execution_digest": {
            "status": "stub",
            "fills": 0,
            "pnl": None,
            "note": "order routing stub; fills/pnl 未接线，不能当作 0 收益",
        },
        "fee_basis": ("eod_decision_outcome.net_return 已含 fees_return 与 slippage_return；"
                      "基准 510300；非实盘账户"),
        "conclusion": conclusion,
    }


def _enqueue(session: Session, *, trade_date: dt.date, kind: str,
             subject: str, payload: dict[str, Any],
             source_job_run_id: int | None) -> dict[str, Any]:
    from qp.jobs.builtin import enqueue  # local import avoids jobs <-> signals cycle

    mode = "live" if get_settings().signals_notify_mode.strip().lower() == "live" else "shadow"
    namespace = f"watchdog:{trade_date.isoformat()}:{kind}"
    inserted = enqueue(
        session,
        channel="telegram",
        recipient="signals" if mode == "live" else "shadow:signals-watchdog",
        dedup_key=namespace,
        subject=subject,
        body=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        source_job_run_id=source_job_run_id,
        mode=mode,
    )
    return {"enqueued": inserted, "namespace": namespace, "mode": mode}


def run_watchdog(session: Session, *, now: dt.datetime | None = None,
                 max_running_minutes: int = MAX_RUNNING_MINUTES, send_alert: bool = True,
                 source_job_run_id: int | None = None) -> dict[str, Any]:
    local = _normalise_now(now)
    is_open, calendar_source = trading_day_status(session, local.date())
    if not is_open:
        return {"ok": True, "reason": "non_trading_day", "deferred": False,
                "message": "非交易日无需审计", "calendar_source": calendar_source,
                "notification_plan": "none", "attempts": summarize_attempts([]),
                "run": None}
    result = inspect_terminal_audit(session, decision_date=local.date(), now=local,
                                    max_running_minutes=max_running_minutes)
    if defer_until_retry(result, local):
        result = {
            "ok": True,
            "reason": "awaiting_same_day_retry",
            "deferred": True,
            "message": ("15:30 为 data_unavailable，15:48 重试前不告警；"
                        f"{result['message']}"),
            "run": result.get("run"),
            "attempts": result.get("attempts"),
        }
    else:
        result["deferred"] = False
    plan = notification_plan(result, local) if send_alert else "none"
    result["notification_plan"] = plan
    result["calendar_source"] = calendar_source
    if plan == "daily":
        payload = build_daily_report(session, result=result, now=local)
        result["shadow_outbox"] = _enqueue(
            session, trade_date=local.date(), kind="daily",
            subject=f"EOD 日报 {local.date().isoformat()}", payload=payload,
            source_job_run_id=source_job_run_id)
        result["daily_report"] = {"conclusion": payload["conclusion"],
                                  "terminal_ok": payload["terminal_ok"]}
    elif plan == "failure":
        payload = {
            "schema_version": 1,
            "report_type": "eod_watchdog_failure",
            "trade_date": local.date().isoformat(),
            "generated_at": local.isoformat(timespec="seconds"),
            "terminal_ok": False,
            "terminal_reason": str(result.get("reason") or ""),
            "message": str(result.get("message") or ""),
            "attempts": result.get("attempts") or summarize_attempts([]),
            "run": result.get("run"),
            "follow_up": "15:50 仍未恢复则并入日报",
        }
        result["shadow_outbox"] = _enqueue(
            session, trade_date=local.date(), kind="failure",
            subject=f"Swing EOD 审计异常 {local.date().isoformat()}", payload=payload,
            source_job_run_id=source_job_run_id)
    return result
