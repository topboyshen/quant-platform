"""Weekly attribution summary job and Telegram renderer."""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs.registry import JobContext
from qp.jobs.signals import _attribution_text, attribution_weekly_job
from qp.models.notify import Outbox
from qp.notify.render import render_outbox


def _clean(engine):
    with Session(engine) as session:
        session.execute(delete(Outbox).where(Outbox.dedup_key.like("attribution-weekly:%")))
        session.commit()


def test_weekly_attribution_enqueues_once_and_renders(engine, monkeypatch):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "shadow")
    _clean(engine)
    try:
        ctx = JobContext(engine=engine, run_id=0, name="test")
        first = attribution_weekly_job(ctx)
        second = attribution_weekly_job(ctx)
        assert first["enqueued"] is True
        assert first["mode"] == "shadow"
        assert second["enqueued"] is False
        assert first["week"].startswith(str(dt.date.today().isocalendar().year))
        with Session(engine) as session:
            rows = list(session.scalars(select(Outbox).where(Outbox.dedup_key.like("attribution-weekly:%"))))
        assert len(rows) == 1
        row = rows[0]
        payload = json.loads(row.body)
        assert payload["report_type"] == "attribution_weekly"
        assert payload["text"].startswith("信号链绩效归因（20 日）")
        assert row.recipient == "shadow:signals-attribution"
        text = render_outbox(row)
        assert "信号链绩效归因" in text
        assert "只读归因，不是交易建议" in text
    finally:
        _clean(engine)


def test_attribution_text_lists_dimensions_and_warnings():
    data = {
        "horizon": 20,
        "as_of": "2026-11-24",
        "threshold": 30,
        "sample": {"matured_filled": 0, "eligible_matured": 0},
        "overall": {"n": 0, "win_rate": None, "avg_net_return": None, "avg_excess_return": None},
        "eligible": {"n": 0, "win_rate": None, "avg_net_return": None, "avg_excess_return": None},
        "warnings": [{"code": "no_matured_outcomes", "message": "没有成熟且成交的结果"}],
        "group_by": ["eligibility", "sector"],
        "dimension_labels": {"eligibility": "资格", "sector": "板块"},
        "groups": {"eligibility": [], "sector": []},
    }
    text = _attribution_text(data)
    assert "没有成熟且成交的结果" in text
    assert "资格：无样本" in text
    assert "板块：无样本" in text


def test_attribution_text_renders_groups():
    data = {
        "horizon": 20,
        "as_of": "2026-11-24",
        "threshold": 30,
        "sample": {"matured_filled": 3, "eligible_matured": 1},
        "overall": {"n": 3, "win_rate": 0.5, "avg_net_return": 0.01, "avg_excess_return": 0.02},
        "eligible": {"n": 1, "win_rate": 1.0, "avg_net_return": 0.03, "avg_excess_return": 0.04},
        "warnings": [],
        "group_by": ["sector"],
        "dimension_labels": {"sector": "板块"},
        "groups": {
            "sector": [
                {"bucket": "宽基", "n": 3, "win_rate": 0.5, "avg_excess_return": 0.02},
            ]
        },
    }
    text = _attribution_text(data)
    assert "板块：宽基 n=3 胜率+50.00% 超额+2.00%" in text
