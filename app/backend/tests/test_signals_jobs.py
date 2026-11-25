"""Signal job wrappers: calendar skips, gate blocks and result shaping."""

from __future__ import annotations

import datetime as dt

import pytest

from qp.jobs import signals
from qp.jobs.registry import JobContext


class FakeSession:
    scalar_value = 1

    def __init__(self):
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def commit(self):
        self.committed = True

    def scalar(self, *_args, **_kwargs):
        return self.scalar_value


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(signals, "Session", lambda _engine: FakeSession())
    return JobContext(engine=object(), run_id=1, name="test")


def test_eod_job_skips_closed_day_and_runs_open_day(monkeypatch, ctx):
    monkeypatch.setattr(signals, "trading_day_status", lambda _s, _d: (False, "calendar"))
    monkeypatch.setattr(signals, "record_skipped_run", lambda *_a, **_k: {"status": "skipped"})
    out = signals.eod_job(ctx)
    assert out["ok"] is True and out["status"] == "skipped"

    monkeypatch.setattr(signals, "trading_day_status", lambda _s, _d: (True, "calendar"))
    monkeypatch.setattr(signals, "previous_open_day", lambda _s, _d: dt.date(2026, 11, 21))
    monkeypatch.setattr(signals, "run_eod", lambda *_a, **_k: {"status": "success"})
    out = signals.eod_job(ctx)
    assert out["ok"] is True and out["previous_session"] == "2026-11-21"


def test_eod_retry_job_paths(monkeypatch, ctx):
    monkeypatch.setattr(signals, "trading_day_status", lambda _s, _d: (False, "calendar"))
    assert signals.eod_retry_job(ctx)["retried"] is False

    monkeypatch.setattr(signals, "trading_day_status", lambda _s, _d: (True, "calendar"))
    monkeypatch.setattr(signals, "latest_retryable_run", lambda _s, _d: (None, False))
    out = signals.eod_retry_job(ctx)
    assert out["retried"] is False and out["reason"] == "not_retryable"

    monkeypatch.setattr(signals, "latest_retryable_run", lambda _s, _d: (None, True))
    monkeypatch.setattr(signals, "previous_open_day", lambda _s, _d: None)
    monkeypatch.setattr(signals, "run_eod", lambda *_a, **_k: {"status": "success"})
    out = signals.eod_retry_job(ctx)
    assert out["retried"] is True and out["ok"] is True

    monkeypatch.setattr(signals, "run_eod", lambda *_a, **_k: {"status": "failed"})
    assert signals.eod_for_date(ctx, dt.date(2026, 11, 24))["ok"] is False


def test_simple_jobs_shape_results(monkeypatch, ctx):
    monkeypatch.setattr(signals, "run_watchdog", lambda *_a, **_k: {"ok": False, "reason": "x"})
    out = signals.watchdog_job(ctx)
    assert out == {"ok": True, "inspection_ok": False, "reason": "x"}

    monkeypatch.setattr(signals, "run_repair", lambda *_a, **_k: {"ok": 1})
    assert signals.total_amount_repair_job(ctx)["ok"] is True

    monkeypatch.setattr(signals, "evaluate_pending", lambda *_a, **_k: {"benchmark_ok": False})
    assert signals.outcomes_job(ctx)["ok"] is False

    monkeypatch.setattr(signals, "build_previous_day_digest", lambda *_a, **_k: {"ok": 0})
    assert signals.execution_digest_job(ctx)["ok"] is False


def test_chain_jobs_handle_disabled_blocked_no_batch_and_success(monkeypatch, ctx):
    from qp.signals import chain, chain_compare, chain_window

    monkeypatch.setattr(chain, "enabled", lambda: False)
    assert signals.chain_snapshot_job(ctx)["skipped"] == "record_chain_disabled"
    assert signals.chain_import_history_job(ctx)["skipped"] == "record_chain_disabled"

    monkeypatch.setattr(chain, "enabled", lambda: True)
    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": False, "reason": "upstream_missing"})
    assert signals.chain_snapshot_job(ctx)["blocked"] == "upstream_gate"

    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(chain_compare, "compare", lambda *_a, **_k: {"status": "complete"})
    monkeypatch.setattr(chain, "build_snapshot", lambda *_a, **_k: {"ok": True, "reason": "no_batch"})
    out = signals.chain_snapshot_job(ctx)
    assert out["skipped"] == "no_batch" and out["upstream_gate"]["ok"] is True

    monkeypatch.setattr(chain, "build_snapshot", lambda *_a, **_k: {"ok": True})
    out = signals.chain_snapshot_job(ctx)
    assert out["compare"]["status"] == "complete" and out["ok"] is True

    monkeypatch.setattr(chain_window, "report", lambda *_a, **_k: {"verdict": "FAIL"})
    out = signals.chain_window_job(ctx)
    assert out["ok"] is False and out["upstream_gate"]["ok"] is True

    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": False})
    assert signals.chain_window_job(ctx)["blocked"] == "upstream_gate"

    from qp.signals import chain_import

    monkeypatch.setattr(chain_import, "import_history", lambda *_a, **_k: {"rows": 3})
    assert signals.chain_import_history_job(ctx)["ok"] is True


def test_rule_shadow_skips_closed_and_blocked(monkeypatch, ctx):
    fake = FakeSession()
    fake.scalar_value = 0
    monkeypatch.setattr(signals, "Session", lambda _engine: fake)
    assert signals.rule_shadow_job(ctx)["skipped"] == "non_trading_day"

    fake.scalar_value = 1
    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": False})
    assert signals.rule_shadow_job(ctx)["blocked"] == "upstream_gate"


def test_shadow_rule_and_rotation_jobs(monkeypatch, ctx):
    from qp.signals import rotation_shadow, rule_shadow, shadow

    monkeypatch.setattr(rule_shadow, "generate", lambda *_a, **_k: {"inserted": 2})
    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": True})
    out = signals.rule_shadow_job(ctx)
    assert out["ok"] is True and out["inserted"] == 2

    monkeypatch.setattr(rule_shadow, "collect", lambda *_a, **_k: {"benchmark_ok": False})
    assert signals.rule_shadow_outcomes_job(ctx)["ok"] is False

    monkeypatch.setattr(shadow, "record", lambda *_a, **_k: {"inserted": 1})
    assert signals.shadow_record_job(ctx)["ok"] is True
    monkeypatch.setattr(shadow, "collect", lambda *_a, **_k: {"benchmark_ok": True})
    assert signals.shadow_outcomes_job(ctx)["ok"] is True

    monkeypatch.setattr(rotation_shadow, "step", lambda *_a, **_k: {"ok": False})
    fake = FakeSession()
    fake.scalar_value = None
    monkeypatch.setattr(signals, "Session", lambda _engine: fake)
    assert signals.rotation_shadow_job(ctx)["error"] == "calendar_day_unknown"
    fake.scalar_value = 0
    assert signals.rotation_shadow_job(ctx)["skipped"] == "non_trading_day"
    fake.scalar_value = 1
    assert signals.rotation_shadow_job(ctx)["ok"] is False


def test_acceptance_job_gate_and_day_paths(monkeypatch, ctx):
    from qp.signals import acceptance

    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": False})
    assert signals.acceptance_job(ctx)["blocked"] == "upstream_gate"

    monkeypatch.setattr(signals, "_gate", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(acceptance, "latest_decision_date", lambda *_a, **_k: None)
    assert signals.acceptance_job(ctx)["reason"] == "no_runs"

    monkeypatch.setattr(acceptance, "latest_decision_date", lambda *_a, **_k: dt.date(2026, 11, 24))
    monkeypatch.setattr(acceptance, "evaluate", lambda *_a, **_k: {"verdict": "WARN"})
    out = signals.acceptance_job(ctx)
    assert out["ok"] is True and out["upstream_gate"]["ok"] is True


def test_shadow_outcomes_keeps_eastmoney_failure_as_warning(monkeypatch, ctx):
    from qp.signals import shadow

    monkeypatch.setattr(
        shadow,
        "collect",
        lambda *_a, **_k: {
            "benchmark_ok": True,
            "benchmark_attempts": [
                {"source": "eastmoney_push2his", "error": "FetchError"},
                {"source": "tencent_kline", "error": ""},
            ],
        },
    )
    out = signals.shadow_outcomes_job(ctx)
    assert out["ok"] is True
    assert out["benchmark_warnings"] == ["eastmoney_push2his:FetchError"]
