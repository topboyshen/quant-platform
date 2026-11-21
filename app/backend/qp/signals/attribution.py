"""Read-only performance attribution for the ETF record chain.

The report joins the latest chain outcome revision to the current eligibility
and state, then groups matured filled outcomes by eligibility, sector, signal
type, origin, signal state or risk state.  It never writes, never sends
notifications and does not change the chain.

Historical ``migrated_v1`` decisions usually have no ``raw_signals`` detail.
Their signal type is reported as ``unknown`` instead of being inferred; the
coverage block and warnings make that limitation explicit.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from qp.signals.chain import ELIGIBILITY_POLICY, HORIZONS, SAMPLE_THRESHOLD

CST = dt.timezone(dt.timedelta(hours=8))
DIMENSIONS = ("eligibility", "sector", "signal_type", "origin", "signal_state", "risk_state")
DEFAULT_GROUP_BY = ("eligibility", "sector", "signal_type")
DIMENSION_LABELS = {
    "eligibility": "资格",
    "sector": "板块",
    "signal_type": "信号类型",
    "origin": "数据来源",
    "signal_state": "信号状态",
    "risk_state": "风控状态",
}
STATUS_LABELS = {
    "sufficient": "样本达标",
    "insufficient": "样本不足",
    "baseline_only": "仅基线",
    "empty": "无样本",
}


def _now() -> dt.datetime:
    return dt.datetime.now(CST)


def _normalise_group_by(group_by: str | Iterable[str] | None) -> tuple[str, ...]:
    if group_by is None:
        return DEFAULT_GROUP_BY
    if isinstance(group_by, str):
        values = [item.strip() for item in group_by.split(",")]
    else:
        values = [str(item).strip() for item in group_by]
    values = [item for item in values if item]
    if not values:
        return DEFAULT_GROUP_BY
    unknown = sorted(set(values) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown group_by: {', '.join(unknown)}; allowed: {', '.join(DIMENSIONS)}")
    return tuple(dict.fromkeys(values))


def _is_matured(row: dict[str, Any]) -> bool:
    return (
        row.get("result_state") == "matured"
        and row.get("execution_state") == "filled"
        and row.get("net_return") is not None
    )


def _stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["net_return"]) for row in rows if row.get("net_return") is not None]
    excess = [float(row["excess_return"]) for row in rows if row.get("excess_return") is not None]
    benchmark = [float(row["benchmark_return"]) for row in rows if row.get("benchmark_return") is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    eligible_n = sum(1 for row in rows if row.get("verdict") == "eligible")
    if not returns:
        status = "empty"
    elif eligible_n == 0:
        status = "baseline_only"
    elif eligible_n < SAMPLE_THRESHOLD:
        status = "insufficient"
    else:
        status = "sufficient"
    return {
        "n": len(returns),
        "eligible_n": eligible_n,
        "win_rate": (len(wins) / len(returns)) if returns else None,
        "avg_net_return": statistics.mean(returns) if returns else None,
        "median_net_return": statistics.median(returns) if returns else None,
        "avg_excess_return": statistics.mean(excess) if excess else None,
        "median_excess_return": statistics.median(excess) if excess else None,
        "avg_benchmark_return": statistics.mean(benchmark) if benchmark else None,
        "avg_win_return": statistics.mean(wins) if wins else None,
        "avg_loss_return": statistics.mean(losses) if losses else None,
        "best_net_return": max(returns) if returns else None,
        "worst_net_return": min(returns) if returns else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "sample_status": status,
        "sample_status_label": STATUS_LABELS[status],
    }


def _bucket(row: dict[str, Any], dimension: str) -> str:
    if dimension == "eligibility":
        return str(row.get("reason_code") or row.get("verdict") or "unknown")
    if dimension == "signal_type":
        # signal types are exploded by _group_rows; this is only a fallback.
        return "unknown"
    return str(row.get(dimension) or "unknown")


def _group_rows(rows: Sequence[dict[str, Any]], dimension: str) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    overlap = False
    for row in rows:
        if dimension == "signal_type":
            types = row.get("signal_types") or ["unknown"]
            if len(types) > 1:
                overlap = True
            for signal_type in types:
                buckets[str(signal_type)].append(row)
        else:
            buckets[_bucket(row, dimension)].append(row)
    groups = []
    for bucket, bucket_rows in buckets.items():
        stat = _stats(bucket_rows)
        groups.append({"bucket": bucket, **stat})
    groups.sort(key=lambda item: (-int(item["n"]), str(item["bucket"])))
    return groups, overlap


def _signal_types(row: dict[str, Any], trend_types: dict[tuple[dt.date, str], set[str]]) -> list[str]:
    types: set[str] = set()
    raw = row.get("raw_signals") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                value = item.get("signal_type")
                if value:
                    types.add(str(value))
    types.update(trend_types.get((row["trade_date"], str(row["code"])), set()))
    return sorted(types) or ["unknown"]


def _fetch_rows(session: Session, *, horizon: int, start: dt.date | None, end: dt.date | None) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"policy": ELIGIBILITY_POLICY, "horizon": horizon}
    if start is not None:
        where.append("d.trade_date >= :start")
        params["start"] = start
    if end is not None:
        where.append("d.trade_date <= :end")
        params["end"] = end
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = text(
        f"""
        SELECT d.decision_uid, d.trade_date, d.code, d.name, d.origin,
               d.universe_version, d.batch_key,
               e.verdict, e.reason_code,
               COALESCE(ss.state, CASE WHEN td.raw_trigger_count > 0 THEN 'triggered'
                                       WHEN td.raw_trigger_count = 0 THEN 'not_triggered' END) AS signal_state,
               COALESCE(sr.state, CASE WHEN td.decision_state = 'blocked' OR td.risk_action = 'blocked' THEN 'blocked'
                                       WHEN td.risk_action = 'not_applicable' THEN 'not_applicable'
                                       WHEN td.decision_state IS NOT NULL THEN 'allowed' END) AS risk_state,
               CASE WHEN ss.state IS NOT NULL THEN 'chain'
                    WHEN td.raw_trigger_count IS NOT NULL THEN 'legacy_target_decision'
                    ELSE 'unknown' END AS signal_state_source,
               CASE WHEN sr.state IS NOT NULL THEN 'chain'
                    WHEN td.decision_state IS NOT NULL THEN 'legacy_target_decision'
                    ELSE 'unknown' END AS risk_state_source,
               o.horizon_sessions, o.result_state, o.execution_state,
               o.net_return, o.excess_return, o.benchmark_return,
               o.expected_maturity_date, o.evaluation_as_of,
               COALESCE(um.sector, td.sector, 'unknown') AS sector,
               td.raw_signals,
               c.run_id
        FROM signals.chain_decision d
        LEFT JOIN signals.chain_decision_current_eligibility e
          ON e.decision_uid = d.decision_uid AND e.policy_version = :policy
        LEFT JOIN signals.chain_decision_current_state ss
          ON ss.decision_uid = d.decision_uid AND ss.aspect = 'signal'
        LEFT JOIN signals.chain_decision_current_state sr
          ON sr.decision_uid = d.decision_uid AND sr.aspect = 'risk'
        LEFT JOIN signals.chain_decision_current_outcome o
          ON o.decision_uid = d.decision_uid AND o.horizon_sessions = :horizon
        LEFT JOIN signals.chain_decision_canonical_attempt c
          ON c.decision_uid = d.decision_uid
        LEFT JOIN LATERAL (
            SELECT td.sector, td.raw_signals, td.raw_trigger_count, td.decision_state, td.risk_action
            FROM signals.target_decision td
            WHERE td.run_id = c.run_id AND td.code = d.code
            ORDER BY td.id DESC
            LIMIT 1
        ) td ON TRUE
        LEFT JOIN signals.chain_universe_member um
          ON um.universe_version = d.universe_version AND um.code = d.code
        {clause}
        """
    )
    return [dict(row) for row in session.execute(sql, params).mappings().all()]


def _trend_types(
    session: Session, *, start: dt.date | None, end: dt.date | None
) -> dict[tuple[dt.date, str], set[str]]:
    where = []
    params: dict[str, Any] = {}
    if start is not None:
        where.append("decision_date >= :start")
        params["start"] = start
    if end is not None:
        where.append("decision_date <= :end")
        params["end"] = end
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    result: dict[tuple[dt.date, str], set[str]] = defaultdict(set)
    for row in session.execute(
        text(f"SELECT decision_date, code, signal_type FROM signals.trend_signal {clause}"), params
    ).mappings():
        if row["signal_type"]:
            result[(row["decision_date"], str(row["code"]))].add(str(row["signal_type"]))
    return result


def _warnings(report: dict[str, Any]) -> list[dict[str, str]]:
    sample = report["sample"]
    warnings: list[dict[str, str]] = []
    if sample["matured_filled"] == 0:
        warnings.append({"code": "no_matured_outcomes", "message": "没有成熟且成交的结果，暂不能做绩效归因。"})
    if sample["eligible_matured"] < SAMPLE_THRESHOLD:
        warnings.append(
            {
                "code": "eligible_sample_below_threshold",
                "message": f"合格样本 {sample['eligible_matured']} < {SAMPLE_THRESHOLD}，只能作描述性参考。",
            }
        )
    if sample["matured_filled"] and sample["signal_type_known"] == 0:
        warnings.append(
            {
                "code": "signal_type_detail_missing",
                "message": "历史决策缺少 raw_signals 明细，signal_type 全部归为 unknown。",
            }
        )
    elif sample["matured_filled"] and sample["signal_type_known"] < sample["matured_filled"]:
        warnings.append(
            {
                "code": "signal_type_partial",
                "message": (
                    f"仅 {sample['signal_type_known']}/{sample['matured_filled']} 条成熟结果有信号类型明细，"
                    "signal_type 分组不完整。"
                ),
            }
        )
    if sample["native_matured"] == 0:
        warnings.append(
            {
                "code": "no_native_matured_outcomes",
                "message": "native 记录链尚无成熟结果；当前成熟样本全部来自 migrated_v1。",
            }
        )
    if sample.get("state_fallback_legacy"):
        warnings.append(
            {
                "code": "state_fallback_legacy",
                "message": (
                    f"{sample['state_fallback_legacy']} 条 migrated_v1 结果没有链内 state，"
                    "signal_state/risk_state 回退 target_decision（来源字段已标记）。"
                ),
            }
        )
    return warnings


def build_report(
    rows: Sequence[dict[str, Any]],
    *,
    horizon: int = 20,
    group_by: str | Iterable[str] | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Aggregate already-fetched rows; kept separate for deterministic tests."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(map(str, HORIZONS))}")
    dimensions = _normalise_group_by(group_by)
    enriched = []
    for row in rows:
        item = dict(row)
        if "signal_types" not in item:
            item["signal_types"] = ["unknown"]
        enriched.append(item)
    matured = [row for row in enriched if _is_matured(row)]
    eligible = [row for row in matured if row.get("verdict") == "eligible"]
    triggered = [row for row in matured if row.get("signal_state") == "triggered"]
    signal_type_known = sum(1 for row in matured if row.get("signal_types") != ["unknown"])
    native_matured = sum(1 for row in matured if row.get("origin") == "native")
    state_fallback_legacy = sum(
        1
        for row in matured
        if row.get("signal_state_source") == "legacy_target_decision"
        or row.get("risk_state_source") == "legacy_target_decision"
    )
    groups: dict[str, Any] = {}
    overlap_flags: dict[str, bool] = {}
    for dimension in dimensions:
        group_rows, overlap = _group_rows(matured, dimension)
        groups[dimension] = group_rows
        overlap_flags[dimension] = overlap
    report: dict[str, Any] = {
        "as_of": (as_of or _now().date()).isoformat(),
        "horizon": horizon,
        "threshold": SAMPLE_THRESHOLD,
        "group_by": list(dimensions),
        "dimension_labels": {dimension: DIMENSION_LABELS[dimension] for dimension in dimensions},
        "definitions": {
            "overall": "所有成熟且成交的结果，含未触发基线",
            "eligible": "当前资格判定为 eligible 的成熟结果",
            "triggered": "当前信号状态为 triggered 的成熟结果",
            "sample_status": "eligible_n 达到阈值才为 sufficient",
        },
        "overall": _stats(matured),
        "eligible": _stats(eligible),
        "triggered": _stats(triggered),
        "sample": {
            "decisions": len(enriched),
            "triggered": sum(1 for row in enriched if row.get("signal_state") == "triggered"),
            "matured_filled": len(matured),
            "eligible_matured": len(eligible),
            "native_matured": native_matured,
            "legacy_matured": len(matured) - native_matured,
            "signal_type_known": signal_type_known,
            "signal_type_unknown": len(matured) - signal_type_known,
            "signal_state_chain": sum(1 for row in matured if row.get("signal_state_source") == "chain"),
            "signal_state_legacy": sum(
                1 for row in matured if row.get("signal_state_source") == "legacy_target_decision"
            ),
            "risk_state_chain": sum(1 for row in matured if row.get("risk_state_source") == "chain"),
            "risk_state_legacy": sum(1 for row in matured if row.get("risk_state_source") == "legacy_target_decision"),
            "state_fallback_legacy": state_fallback_legacy,
        },
        "groups": groups,
        "overlap": overlap_flags,
        "warnings": [],
    }
    report["warnings"] = _warnings(report)
    return report


def report(
    session: Session,
    *,
    horizon: int = 20,
    start: dt.date | None = None,
    end: dt.date | None = None,
    group_by: str | Iterable[str] | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Compute the read-only attribution report for one horizon."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(map(str, HORIZONS))}")
    dimensions = _normalise_group_by(group_by)
    rows = _fetch_rows(session, horizon=horizon, start=start, end=end)
    trend_types = _trend_types(session, start=start, end=end)
    for row in rows:
        row["signal_types"] = _signal_types(row, trend_types)
    result = build_report(rows, horizon=horizon, group_by=dimensions, as_of=as_of)
    result["start"] = start.isoformat() if start else None
    result["end"] = end.isoformat() if end else None
    return result


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def markdown(data: dict[str, Any]) -> str:
    """Compact operator-facing rendering of an attribution report."""
    lines = [
        f"# 信号链绩效归因（{data['horizon']} 日）",
        "",
        f"- 截至：{data['as_of']}；范围：{data.get('start') or '全部'} → {data.get('end') or '全部'}",
        f"- 成熟且成交：{data['sample']['matured_filled']}；合格：{data['sample']['eligible_matured']}"
        f"；阈值：{data['threshold']}",
        f"- 总体：胜率 {_pct(data['overall']['win_rate'])}，平均净收益 {_pct(data['overall']['avg_net_return'])}，"
        f"平均超额 {_pct(data['overall']['avg_excess_return'])}",
        f"- 合格样本：胜率 {_pct(data['eligible']['win_rate'])}，平均净收益 {_pct(data['eligible']['avg_net_return'])}，"
        f"平均超额 {_pct(data['eligible']['avg_excess_return'])}",
        "",
    ]
    for warning in data["warnings"]:
        lines.append(f"> 警告：{warning['message']}")
    if data["warnings"]:
        lines.append("")
    for dimension in data["group_by"]:
        lines.extend(
            [
                f"## {data['dimension_labels'][dimension]}",
                "",
                "| 分组 | 样本 | 合格 | 胜率 | 平均净收益 | 平均超额 | 状态 |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for group in data["groups"].get(dimension, []):
            lines.append(
                f"| {group['bucket']} | {group['n']} | {group['eligible_n']} | {_pct(group['win_rate'])} | "
                f"{_pct(group['avg_net_return'])} | {_pct(group['avg_excess_return'])} | "
                f"{group['sample_status_label']} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
