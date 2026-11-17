"""17:40 ETF research shadow report: read-only payload, renderer and timer."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs import JOBS
from qp.jobs.registry import JobContext
from qp.jobs.research import (
    build_combined_report,
    build_etf_shadow_report,
    etf_shadow_report_job,
)
from qp.models.market import IndexClose
from qp.models.notify import Outbox
from qp.models.signals import MacroSlice, RiskState
from qp.notify.render import render_outbox
from qp.signals.eod import now_cst
from qp.signals.watchdog import build_daily_report

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def payload() -> dict:
    return {
        "schema_version": 1,
        "report_type": "etf_shadow_report",
        "trade_date": "2026-11-16",
        "generated_at": "2026-11-16T17:40:00+08:00",
        "read_only": True,
        "is_trade_signal": False,
        "scores": {
            "20": {
                "run_id": 2,
                "model_version": "qp-etf-lgbm20-v1-test",
                "data_cutoff": "2026-11-16",
                "stale": False,
                "eligible": 40,
                "top8": [{"code": "515050", "name": "5G通信ETF", "rank": 1, "score": 0.0686}],
            },
            "5": {
                "run_id": 3,
                "model_version": "qp-etf-lgbm5-v1-test",
                "data_cutoff": "2026-11-16",
                "stale": False,
                "eligible": 40,
                "top8": [{"code": "515050", "name": "5G通信ETF", "rank": 1, "score": 0.0393}],
            },
        },
        "rotation": {
            "rule_version": "qp-rotation.v1",
            "trade_date": "2026-11-16",
            "signal_date": "2026-11-13",
            "execute_date": "2026-11-16",
            "nav": 994561.87,
            "nav_return": -0.0054,
            "benchmark_return": -0.0042,
            "ew_return": -0.0122,
            "positions": [{"code": "159941", "name": "纳指ETF"}, {"code": "512480", "name": "半导体ETF"}],
        },
        "data_quality": {"missing": [], "stale": [], "note": "5d/20d 均为当日快照，40/40 可评分"},
        "conclusion": "研究影子，不构成推荐；5d 仍不晋级。",
    }


def test_render_etf_shadow_report():
    row = Outbox(
        channel="telegram",
        recipient="signals",
        dedup_key="etf-shadow:render",
        subject="ETF 研究影子 2026-11-16",
        body=json.dumps(payload(), ensure_ascii=False),
        mode="live",
    )
    text = render_outbox(row)
    assert "ETF 研究影子 2026-11-16" in text
    assert "ETF 研究观察（不是买卖建议）" in text
    assert "5日/20日模型都选中这1只：5G通信ETF" in text
    assert "纸面轮动：今天 -0.54%；沪深300 -0.42%；40只等权 -1.22%" in text
    assert "持有：纳指ETF、半导体ETF" in text
    assert "只观察，不用于实盘" in text
    assert '"report_type"' not in text


def test_render_daily_includes_market_risk_and_funnel():
    body = json.dumps(
        {
            "report_type": "eod_daily_report",
            "trade_date": "2026-11-16",
            "terminal_ok": True,
            "terminal_reason": "success",
            "market": {"text": "上证 3,602.57（-0.87%） | 创业板指 2,320.02（-4.37%）"},
            "macro_text": "宏观：成交额 14,845 亿；涨停 32 / 跌停 62",
            "risk_text": "风险闸门：critical（VIX=19.30 > 17.5）；VIX 19.30",
            "funnel": "信号漏斗：评估 40/40 · 触发 0 · allowed 0 · 数据失败 0",
            "reading": {"market_data": "标的行情：40/40 合格", "research_eligibility": "研究资格：批次成功"},
            "today_trend_signals": 0,
            "conclusion": "今日无 allowed 信号；描述性结果，不支持推荐。",
        },
        ensure_ascii=False,
    )
    row = Outbox(
        channel="telegram",
        recipient="signals",
        dedup_key="eod:render",
        subject="EOD 日报 2026-11-16",
        body=body,
        mode="live",
    )
    text = render_outbox(row)
    assert "市场：上证 3,602.57" in text
    assert "宏观：成交额 14,845 亿" in text
    assert "风险闸门：critical" in text
    assert "信号漏斗：评估 40/40" in text
    assert "研究资格：批次成功" in text


def test_daily_builder_exposes_market_risk_and_funnel(engine):
    day = dt.date(2031, 4, 7)
    try:
        with Session(engine) as s:
            s.add_all(
                [
                    IndexClose(origin="qp", symbol="sh000001", trade_date=day, close=3602.57, source="test"),
                    IndexClose(
                        origin="qp",
                        symbol="sh000001",
                        trade_date=day - dt.timedelta(days=3),
                        close=3634.35,
                        source="test",
                    ),
                    MacroSlice(
                        decision_date=day,
                        total_amount=14844.9,
                        margin_balance=22791.12,
                        margin_growth_pct=-0.708,
                        limit_up=32,
                        limit_down=62,
                        limit_ratio=0.508,
                        source="test",
                        collect_status={},
                    ),
                    RiskState(
                        observed_at=dt.datetime(2031, 4, 7, 15, 30, tzinfo=dt.UTC),
                        level="critical",
                        message="VIX=19.30 > 17.5",
                        values={"vix": 19.3, "us_10y": 5.154},
                        raw={},
                    ),
                ]
            )
            s.commit()
            out = build_daily_report(
                s,
                result={
                    "ok": True,
                    "reason": "success",
                    "run": {
                        "run_id": 999,
                        "status": "success",
                        "target_count": 40,
                        "evaluated_count": 40,
                        "failed_count": 0,
                        "signal_count": 0,
                        "allowed_signal_count": 0,
                        "blocked_signal_count": 0,
                    },
                },
                now=dt.datetime(2031, 4, 7, 15, 50, tzinfo=dt.UTC),
            )
    finally:
        with Session(engine) as s:
            s.execute(delete(IndexClose).where(IndexClose.source == "test"))
            s.execute(delete(MacroSlice).where(MacroSlice.decision_date == day))
            s.execute(delete(RiskState).where(RiskState.observed_at == dt.datetime(2031, 4, 7, 15, 30, tzinfo=dt.UTC)))
            s.commit()
    assert out["market"]["text"].startswith("上证 3,602.57")
    assert out["market"]["indices"][0]["change"] < 0
    assert out["risk"]["level"] == "critical"
    assert "VIX 19.30" in out["risk_text"]
    assert "成交额 14,845 亿" in out["macro_text"]
    assert "评估 40/40" in out["funnel"]
    assert "今日无 allowed 信号" in out["conclusion"]


def test_combined_renderer_contains_both_sections():
    body = {"report_type": "daily_combined_report", "trade_date": "2026-11-16", "etf_shadow": payload()}
    body["terminal_ok"] = True
    body["market"] = {"text": "上证 3,602.57（-0.87%）"}
    body["macro_text"] = "宏观：成交额 14,845 亿"
    body["risk_text"] = "风险闸门：critical；VIX 19.30"
    body["funnel"] = "信号漏斗：评估 40/40 · 触发 0 · allowed 0"
    body["reading"] = {"market_data": "标的行情：40/40 合格"}
    body["conclusion"] = "今日无 allowed 信号；不支持推荐。"
    row = Outbox(
        channel="telegram",
        recipient="signals",
        dedup_key="combined:render",
        subject="每日报告 2026-11-16",
        body=json.dumps(body, ensure_ascii=False),
        mode="live",
    )
    text = render_outbox(row)
    assert "市场：上证 3,602.57" in text
    assert "风险闸门：critical" in text
    assert "ETF 研究观察（不是买卖建议）" in text
    assert "纸面轮动" in text
    assert "不支持推荐" in text


def test_job_is_shadow_only_when_combined_disabled(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_COMBINED_REPORT", "0")
    key = f"etf-shadow:{now_cst().date().isoformat()}"
    with Session(engine) as s:
        s.execute(delete(Outbox).where(Outbox.dedup_key == key))
        s.commit()
    out = etf_shadow_report_job(JobContext(engine=engine, run_id=0, name="research.etf_shadow_report"))
    assert out["combined"] is False
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == key))
        assert row is not None and row.subject.startswith("ETF 研究观察")
        assert json.loads(row.body)["report_type"] == "etf_shadow_report"
        s.execute(delete(Outbox).where(Outbox.dedup_key == key))
        s.commit()


def test_combined_builder_has_shadow(engine):
    with Session(engine) as s:
        out = build_combined_report(s, today=dt.date(2030, 1, 2))
    assert out["report_type"] == "daily_combined_report"
    assert out["read_only"] is True and out["is_trade_signal"] is False
    assert "etf_shadow" in out
    assert "missing" in out["etf_shadow"]["data_quality"]


def test_empty_builder_is_explicit(engine):
    with Session(engine) as s:
        out = build_etf_shadow_report(s, today=dt.date(2030, 1, 2))
    assert out["report_type"] == "etf_shadow_report"
    assert out["read_only"] is True and out["is_trade_signal"] is False
    assert out["scores"]["20"] is None and out["scores"]["5"] is None
    assert "20d 快照缺失" in out["data_quality"]["missing"]
    assert "5d 快照缺失" in out["data_quality"]["missing"]
    assert "不构成推荐" in out["conclusion"]


def test_job_and_timer_registered():
    assert "research.etf_shadow_report" in JOBS
    assert JOBS["research.etf_shadow_report"].schedule_hint.startswith("Mon-Fri 17:40")
    text = (DEPLOY / "systemd" / "qp-research-etf-shadow.timer").read_text(encoding="utf-8")
    assert "17:40:00 Asia/Shanghai" in text
    assert "Unit=qp-job@research.etf_shadow_report.service" in text
