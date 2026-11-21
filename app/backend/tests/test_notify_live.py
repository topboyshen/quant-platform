"""M4 live delivery: per-row shadow/live semantics, Telegram sender, kill switch and timer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qp.jobs import JOBS, run_job
from qp.jobs.builtin import enqueue
from qp.models.notify import DeliveryAttempt, Outbox
from qp.notify.render import render_outbox
from qp.notify.telegram import SendResult, TelegramError, TelegramSender

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _clean_outbox(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(DeliveryAttempt))
        s.execute(delete(Outbox))
        s.commit()


@pytest.fixture()
def clean_outbox(engine):
    _clean_outbox(engine)
    yield
    # Leave no live/sent rows behind: M1/M2 tests assert every outbox row is shadow.
    _clean_outbox(engine)


def _live_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    token = tmp_path / "telegram_bot_token"
    chat = tmp_path / "telegram_chat_id"
    token.write_text("123456:test-token\n", encoding="utf-8")
    chat.write_text("6325485529\n", encoding="utf-8")
    monkeypatch.setenv("QP_NOTIFY_MODE", "shadow")
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    monkeypatch.setenv("QP_TELEGRAM_BOT_TOKEN_FILE", str(token))
    monkeypatch.setenv("QP_TELEGRAM_CHAT_ID_FILE", str(chat))
    monkeypatch.setenv("QP_TELEGRAM_TIMEOUT_SEC", "2")
    monkeypatch.setenv("QP_NOTIFY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("QP_NOTIFY_KILL_SWITCH", "0")
    monkeypatch.setenv("QP_NOTIFY_DISABLED_FILE", str(tmp_path / "notify.disabled"))
    return token, chat


def _row(key: str, *, mode: str, body: str = "hello", subject: str | None = "测试") -> Outbox:
    return Outbox(
        channel="telegram", recipient="signals", dedup_key=key, subject=subject, body=body, mode=mode, status="pending"
    )


def _daily_payload() -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "report_type": "eod_daily_report",
            "trade_date": "2026-11-13",
            "terminal_ok": True,
            "terminal_reason": "",
            "counts": {"targets": 40, "evaluated": 40, "failed": 0, "signals": 0},
            "today_trend_signals": 0,
            "reading": {
                "market_data": "标的行情：40/40 合格",
                "macro": "宏观数据：完整",
                "signals": "信号：无触发",
                "research_eligibility": "研究资格：批次成功",
            },
            "conclusion": "暂无可评价样本；描述性结果，不支持推荐。",
        },
        ensure_ascii=False,
    )


def test_enqueue_defaults_to_shadow_and_live_is_explicit(engine, monkeypatch, clean_outbox):
    monkeypatch.setenv("QP_NOTIFY_MODE", "shadow")
    with Session(engine) as s:
        assert enqueue(s, channel="telegram", recipient="ops", dedup_key="m4live:enq:1", body="x")
        assert enqueue(s, channel="telegram", recipient="signals", dedup_key="m4live:enq:2", body="x", mode="live")
        s.commit()
        rows = {r.dedup_key: r for r in s.scalars(select(Outbox)).all()}
    assert rows["m4live:enq:1"].mode == "shadow"
    assert rows["m4live:enq:2"].mode == "live"


def test_live_row_is_sent_and_receipt_recorded(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.setattr(TelegramSender, "send", lambda self, text: SendResult(message_id=777, latency_ms=12))
    with Session(engine) as s:
        s.add(_row("m4live:send:1", mode="live", body=_daily_payload(), subject="EOD 日报 2026-11-13"))
        s.commit()
    run = run_job(engine, "notify.send", trigger="test")
    assert run.status == "succeeded"
    assert run.result["sent"] == 1 and run.result["failed"] == 0
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:send:1"))
        assert row.status == "sent" and row.send_result == "sent" and row.processed_at
        attempt = s.scalar(select(DeliveryAttempt).where(DeliveryAttempt.outbox_id == row.id))
        assert attempt.mode == "live" and attempt.ok is True
        assert attempt.detail["message_id"] == 777


def test_shadow_row_is_never_sent(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)
    called: list[str] = []

    def _boom(self, text):
        called.append(text)
        raise AssertionError("shadow rows must never reach the sender")

    monkeypatch.setattr(TelegramSender, "send", _boom)
    with Session(engine) as s:
        s.add(_row("m4live:shadow:1", mode="shadow"))
        s.commit()
    run = run_job(engine, "notify.send", trigger="test")
    assert run.status == "succeeded"
    assert run.result["shadowed"] == 1 and run.result["sent"] == 0
    assert called == []
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:shadow:1"))
        assert row.status == "shadowed" and row.send_result == "not_sent_shadow"
        attempt = s.scalar(select(DeliveryAttempt).where(DeliveryAttempt.outbox_id == row.id))
        assert attempt.mode == "shadow" and attempt.ok is None


def test_kill_switch_stops_every_live_send(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("QP_NOTIFY_KILL_SWITCH", "1")
    with Session(engine) as s:
        s.add(_row("m4live:kill:1", mode="live"))
        s.commit()
    run = run_job(engine, "notify.send", trigger="test")
    assert run.status == "succeeded"
    assert run.result["skipped"] == "kill_switch" and run.result["sent"] == 0
    with Session(engine) as s:
        assert s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:kill:1")).status == "pending"
        assert s.execute(select(DeliveryAttempt)).scalars().all() == []


def test_missing_credentials_fail_closed(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("QP_TELEGRAM_BOT_TOKEN_FILE", str(tmp_path / "missing-token"))
    with Session(engine) as s:
        s.add(_row("m4live:nocred:1", mode="live"))
        s.commit()
    run = run_job(engine, "notify.send", trigger="test")
    assert run.status == "failed"
    assert run.result["error"] == "telegram_token_unreadable"
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:nocred:1"))
        assert row.status == "pending"
        assert s.execute(select(DeliveryAttempt)).scalars().all() == []


def test_permanent_error_marks_failed_without_retry(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)

    def _bad(self, text):
        raise TelegramError("http_400", status_code=400, retryable=False, detail="chat not found")

    monkeypatch.setattr(TelegramSender, "send", _bad)
    with Session(engine) as s:
        s.add(_row("m4live:perm:1", mode="live"))
        s.commit()
    run = run_job(engine, "notify.send", trigger="test")
    assert run.status == "failed"
    assert run.result["failed"] == 1 and run.result["retry"] == 0
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:perm:1"))
        assert row.status == "failed" and row.send_result == "failed:http_400"


def test_transient_error_retries_then_fails_at_max_attempts(engine, monkeypatch, tmp_path, clean_outbox):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("QP_NOTIFY_MAX_ATTEMPTS", "2")

    def _down(self, text):
        raise TelegramError("network_error", retryable=True)

    monkeypatch.setattr(TelegramSender, "send", _down)
    with Session(engine) as s:
        s.add(_row("m4live:retry:1", mode="live"))
        s.commit()
    first = run_job(engine, "notify.send", trigger="test")
    assert first.result["retry"] == 1 and first.result["failed"] == 0
    with Session(engine) as s:
        assert s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:retry:1")).status == "pending"
    second = run_job(engine, "notify.send", trigger="test")
    assert second.status == "failed"
    assert second.result["failed"] == 1
    with Session(engine) as s:
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "m4live:retry:1"))
        assert row.status == "failed" and row.send_result == "failed:network_error"


def test_render_daily_report_is_human_readable():
    row = Outbox(
        channel="telegram",
        recipient="signals",
        dedup_key="render:1",
        subject="EOD 日报 2026-11-13",
        body=_daily_payload(),
        mode="live",
    )
    text = render_outbox(row)
    assert "EOD 日报 2026-11-13" in text
    assert "终态：正常" in text
    assert "标的行情：40/40 合格" in text
    assert "结论：" in text
    assert '"schema_version"' not in text and '"reading"' not in text


def test_watchdog_enqueue_uses_signals_live_mode(engine, monkeypatch, clean_outbox):
    monkeypatch.setenv("QP_SIGNALS_NOTIFY_MODE", "live")
    from qp.signals.watchdog import _enqueue

    with Session(engine) as s:
        out = _enqueue(
            s, trade_date=date(2026, 11, 13), kind="daily", subject="EOD 日报", payload={"a": 1}, source_job_run_id=None
        )
        s.commit()
        row = s.scalar(select(Outbox).where(Outbox.dedup_key == "watchdog:2026-11-13:daily"))
    assert out["mode"] == "live" and out["enqueued"] is True
    assert row.mode == "live" and row.recipient == "signals"


def test_notify_timer_targets_registered_job_and_never_backfills():
    text = (DEPLOY / "systemd" / "qp-notify.timer").read_text(encoding="utf-8")
    assert "Unit=qp-job@notify.send.service" in text
    assert "Asia/Shanghai" in text and "Persistent=false" in text
    assert "notify.send" in JOBS


def test_live_runbook_documents_switch_and_emergency_stop():
    text = (DEPLOY / "M4-LIVE-RUNBOOK.md").read_text(encoding="utf-8")
    for token in (
        "QP_SIGNALS_NOTIFY_MODE",
        "QP_NOTIFY_KILL_SWITCH",
        "telegram_bot_token",
        "delivery_attempt",
        "回滚",
        "不补发",
    ):
        assert token in text


def _daily_row(payload: dict) -> Outbox:
    return Outbox(
        channel="telegram",
        recipient="signals",
        dedup_key="render:macro",
        subject="EOD 日报 2026-11-16",
        body=json.dumps(payload, ensure_ascii=False),
        mode="live",
    )


def _macro_payload(**macro) -> dict:
    payload = json.loads(_daily_payload())
    payload["macro"] = {
        "date": "2026-11-16",
        "total_amount": 14844.9,
        "margin_balance": 22791.12,
        "margin_growth_pct": -0.708,
        "limit_up": 32,
        "limit_down": 62,
        "limit_ratio": 0.508,
        "source": "tencent+sse+szse+eastmoney",
        "total_amount_source": "tencent",
    }
    payload["macro"].update(macro)
    return payload


def test_render_daily_report_includes_macro_detail():
    text = render_outbox(_daily_row(_macro_payload()))
    assert ("宏观明细：成交额 14,844.9 亿；两融余额 22,791.12 亿（-0.71%）；涨停 32 / 跌停 62") in text
    assert text.index("宏观数据：完整") < text.index("宏观明细：") < text.index("信号：无触发")


def test_render_daily_report_macro_detail_handles_partial_and_missing():
    partial = render_outbox(
        _daily_row(_macro_payload(margin_balance=None, margin_growth_pct=None, limit_up=None, limit_down=None))
    )
    assert "宏观明细：成交额 14,844.9 亿" in partial
    assert "两融" not in partial and "涨停" not in partial
    only_limits = render_outbox(_daily_row(_macro_payload(total_amount=None)))
    assert "涨停 32 / 跌停 62" in only_limits and "成交额" not in only_limits
    empty = render_outbox(
        _daily_row(
            _macro_payload(
                total_amount=None, margin_balance=None, margin_growth_pct=None, limit_up=None, limit_down=None
            )
        )
    )
    assert "宏观明细" not in empty and "宏观数据：完整" in empty
    no_macro = json.loads(_daily_payload())
    assert "宏观明细" not in render_outbox(_daily_row(no_macro))
