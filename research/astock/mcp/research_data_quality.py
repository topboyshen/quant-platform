"""Separate coverage/availability from source confidence for new inputs only.

No historical input is upgraded by this module. Completeness refers to the
declared morning input scope, not a complete financial or semantic study.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Mapping


QUALITY_VERSION = "research-data-quality-v1"


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def official_window_assessment(payload: Any, cutoff: datetime,
                               previous_day: date | None) -> dict[str, Any]:
    """Pagination completeness as actually observed, never a future absence claim."""
    payload = payload if isinstance(payload, Mapping) else {}
    coverage = payload.get("queryCoverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    observed = _instant(payload.get("queriedAt"))
    start = _instant(coverage.get("windowStartAt"))
    end = _instant(coverage.get("cutoffAt"))
    recorded = _instant(coverage.get("queriedAt"))
    through = _instant(coverage.get("observedThroughAt"))
    pages, total, returned = (coverage.get(k) for k in ("pagesFetched", "total", "returned"))
    page_size, max_pages = coverage.get("pageSize"), coverage.get("maxPages")
    required_start = datetime.combine(previous_day, time.min, cutoff.tzinfo) if previous_day else None
    counts_valid = (
        all(type(x) is int for x in (pages, total, returned, page_size, max_pages))
        and 1 <= page_size <= 100 and 1 <= max_pages <= 8
        and page_size * max_pages <= 120
        and 1 <= pages <= max_pages and 0 <= returned == total <= pages * page_size
    )
    valid = (
        payload.get("status") == "available" and coverage.get("kind") == "window"
        and coverage.get("complete") is True
        and coverage.get("reason") == "all_query_pages_collected"
        and required_start is not None and start is not None and start <= required_start
        and end == cutoff and observed is not None and start <= observed <= cutoff
        and recorded == observed and through is not None and start <= through <= observed and counts_valid
    )
    return {
        "status": "complete_as_observed" if valid else "incomplete" if payload.get("status") == "available" else "unavailable",
        "completeAsObserved": bool(valid),
        "queriedAt": payload.get("queriedAt"),
        "observedThroughAt": coverage.get("observedThroughAt"),
        "requestedCutoffAt": cutoff.isoformat(),
        "finalCutoffCovered": bool(valid and through == cutoff),
        "reason": coverage.get("reason") or "window_proof_missing",
        "scope": "paginated_query_not_atomic_snapshot_or_future_filings",
    }


def build_quality(*, cutoff: datetime, previous_day: date | None,
                  modules: Mapping[str, Any], indices: list[Any],
                  official_results: list[Any], stock_codes: list[str],
                  market_score: int, financial_score: int,
                  section_scores: Mapping[str, int]) -> dict[str, Any]:
    """Versioned diagnostics; trust limitations never silently disappear."""
    source_states = {name: result.status for name, result in modules.items()}
    index_by_code = {item.get("code"): item for item in indices if isinstance(item, Mapping)}
    missing = []
    for code in ("sh000001", "sz399001", "sz399006"):
        item = index_by_code.get(code)
        if item is None:
            missing.append(f"indices.{code}")
        elif item.get("turnover") is None:
            missing.append(f"indices.{code}.turnover")
    us = []
    for name, result in modules.items():
        if not name.startswith("us_index_"):
            continue
        item = result.data if isinstance(result.data, Mapping) else {}
        us.append({"symbol": item.get("symbol") or name.removeprefix("us_index_"),
                   "sessionDate": item.get("sessionDate"),
                   "expectedSessionDate": item.get("expectedSessionDate"),
                   "status": item.get("freshnessStatus") or "unverified"})
    official = [{"code": code, **official_window_assessment(result.data, cutoff, previous_day)}
                for code, result in zip(stock_codes, official_results)]
    stale = [item["symbol"] for item in us if item["status"] != "current"]
    return {
        "schemaVersion": QUALITY_VERSION,
        "requestedCutoffAt": cutoff.isoformat(),
        "scope": "declared_morning_inputs_not_full_research",
        "scoreMeaning": "validated_content_and_source_state_not_confidence",
        "sourceAvailability": source_states,
        "requiredFields": {"marketScore": market_score, "basicFinancialScore": financial_score,
                           "missing": missing},
        "freshness": {"status": "current" if us and not stale else "unverified_or_stale",
                      "items": us},
        "officialWindowCoverage": official,
        "policyUnavailable": [{"field": "northbound_realtime", "status": "not_disclosed",
                               "required": False,
                               "reason": "实时净流入不公开披露；不得以成交额或估算替代"}],
        "sourceCredibility": {
            "mediaNews": {"kind": "media_not_official", "confidenceCap": 75,
                          "affectsCoverageScore": False},
            "financial": {"kind": "aggregated_basic_fields_not_full_filing_verification",
                          "affectsCoverageScore": False},
        },
        "limitations": [
            "live/100仅指声明的晨报必需输入可用，不表示完整研究或语义已审",
            "公告分页完整仅截至真实查询时刻；查询后至固定截止的新增披露仍未知",
            "基本财务字段齐全不等于现金流、应收、存货、减值与官方口径全部核验",
        ],
        "declaredInputsComplete": bool(section_scores and all(v == 100 for v in section_scores.values())
                                       and market_score == 100 and financial_score == 100 and not missing
                                       and us and not stale and official and all(v["completeAsObserved"] for v in official)
                                       and all(v == "live" for v in source_states.values())),
    }
