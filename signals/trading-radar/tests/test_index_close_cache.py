from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts import index_close_backend_probe as backend_probe
from scripts import research_digest
from scripts import update_index_close_cache as updater

TZ = ZoneInfo("Asia/Shanghai")
EXPECTED = "2026-08-21"


def _probe_payload(*, fallback_symbol: str | None = None) -> dict:
    closes = []
    for index, symbol in enumerate(updater.REQUIRED_SYMBOLS, start=1):
        closes.append(
            {
                "symbol": symbol,
                "close": 3900.0 + index,
                "trade_date": EXPECTED,
                "source": "sina" if symbol == fallback_symbol else "tencent",
            }
        )
    return {
        "expected_as_of": EXPECTED,
        "source": "tencent+sina" if fallback_symbol else "tencent",
        "closes": closes,
    }


def test_backend_probe_uses_sina_only_when_exact_tencent_row_is_absent():
    epoch = int(datetime(2026, 8, 21, 15, 0, tzinfo=TZ).timestamp())
    item = backend_probe.fetch_exact_close(
        "sh000001",
        EXPECTED,
        fetch_tencent=lambda _symbol: [["2026-08-20", "1", "3900", "1", "1", "1"]],
        fetch_sina=lambda _symbol: [{"time": epoch, "close": "3905.20"}],
    )
    assert item == {
        "symbol": "sh000001",
        "close": 3905.2,
        "trade_date": EXPECTED,
        "source": "sina",
    }


def test_backend_probe_rejects_when_both_sources_lack_exact_date():
    with pytest.raises(RuntimeError, match="unavailable"):
        backend_probe.fetch_exact_close(
            "sh000001",
            EXPECTED,
            fetch_tencent=lambda _symbol: [],
            fetch_sina=lambda _symbol: [],
        )


def test_validate_requires_exact_unique_four_symbol_set():
    payload = _probe_payload()
    payload["closes"][3] = dict(payload["closes"][0])
    with pytest.raises(ValueError, match="missing, unexpected, or duplicated"):
        updater.validate_probe_payload(payload, EXPECTED)


def test_validate_rejects_mixed_trade_dates():
    payload = _probe_payload()
    payload["closes"][2]["trade_date"] = "2026-08-20"
    with pytest.raises(ValueError, match="trade_date mismatch"):
        updater.validate_probe_payload(payload, EXPECTED)


def test_refresh_writes_validated_payload_atomically(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    status = updater.refresh(
        output,
        now=datetime(2026, 8, 23, 10, 0, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(fallback_symbol="sz399006"),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert output.stat().st_mode & 0o777 == 0o644
    assert payload["schema_version"] == updater.SCHEMA_VERSION
    assert payload["trade_date"] == EXPECTED
    assert payload["expected_as_of"] == EXPECTED
    assert payload["stale"] is False
    assert payload["source"] == "tencent+sina"
    assert status["mode"] == "refresh"


def test_source_smoke_never_writes(monkeypatch, tmp_path):
    output = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    status = updater.refresh(
        output,
        now=datetime(2026, 8, 23, 10, 0, tzinfo=TZ),
        source_smoke=True,
        probe=lambda _expected: _probe_payload(),
    )
    assert status["mode"] == "source_smoke"
    assert not output.exists()


def test_refresh_invalid_trade_date_is_not_misclassified_as_future(
    monkeypatch, tmp_path
):
    output = tmp_path / "index_closes_latest.json"
    output.write_text('{"trade_date":"2026-99-99","closes":[]}\n', encoding="utf-8")
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    status = updater.refresh(
        output,
        now=datetime(2026, 8, 21, 15, 35, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(),
    )

    assert status["mode"] == "refresh"
    assert json.loads(output.read_text(encoding="utf-8"))["trade_date"] == EXPECTED


def test_refresh_refuses_canonical_future_cache_before_probe(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    original = b'{"trade_date":"2026-08-24","closes":[]}\n'
    output.write_bytes(original)
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    def must_not_probe(_expected: str):
        raise AssertionError("future cache must fail before probing")

    with pytest.raises(RuntimeError, match="refusing to regress a future cache"):
        updater.refresh(
            output,
            now=datetime(2026, 8, 21, 15, 35, tzinfo=TZ),
            probe=must_not_probe,
        )

    assert output.read_bytes() == original


def test_repair_fresh_cache_is_explicit_noop_without_probe(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    # Only a cache carrying a verified write receipt is trusted as fresh.
    updater.refresh(
        output,
        now=datetime(2026, 8, 21, 15, 35, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(),
    )
    original = output.read_bytes()
    original_mtime = output.stat().st_mtime_ns

    def must_not_probe(_expected: str):
        raise AssertionError("fresh repair must not call the upstream probe")

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
        probe=must_not_probe,
    )

    assert status["status"] == "ok"
    assert status["mode"] == "repair_noop"
    assert status["trade_date"] == EXPECTED
    assert status["expected_as_of"] == EXPECTED
    assert status["stale"] is False
    assert output.read_bytes() == original
    assert output.stat().st_mtime_ns == original_mtime


def test_repair_stale_cache_writes_complete_exact_date(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    output.write_text(
        json.dumps({"trade_date": "2026-08-20", "closes": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(fallback_symbol="sz399006"),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert status["mode"] == "repair"
    assert status["trade_date"] == EXPECTED
    assert payload["schema_version"] == updater.SCHEMA_VERSION
    assert payload["trade_date"] == EXPECTED
    assert payload["expected_as_of"] == EXPECTED
    assert payload["stale"] is False
    assert len(payload["closes"]) == 4


def test_repair_invalid_current_date_cache_is_not_treated_as_fresh(
    monkeypatch, tmp_path
):
    output = tmp_path / "index_closes_latest.json"
    invalid = _probe_payload()
    invalid.update(
        {
            "schema_version": updater.SCHEMA_VERSION,
            "trade_date": EXPECTED,
            "stale": False,
            "source": "tencent",
            "fetched_at": "2026-08-21T15:35:00+08:00",
        }
    )
    invalid["closes"].pop()
    output.write_text(json.dumps(invalid), encoding="utf-8")
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    calls: list[str] = []

    def probe(expected: str):
        calls.append(expected)
        return _probe_payload()

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
        probe=probe,
    )

    assert calls == [EXPECTED]
    assert status["mode"] == "repair"
    assert len(json.loads(output.read_text(encoding="utf-8"))["closes"]) == 4


@pytest.mark.parametrize("invalid_date", ["garbage", "2026-99-99", "20260821"])
def test_repair_invalid_trade_date_is_repairable(monkeypatch, tmp_path, invalid_date):
    output = tmp_path / "index_closes_latest.json"
    output.write_text(
        json.dumps({"trade_date": invalid_date, "closes": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    calls: list[str] = []

    def probe(expected: str):
        calls.append(expected)
        return _probe_payload()

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
        probe=probe,
    )

    assert calls == [EXPECTED]
    assert status["mode"] == "repair"
    assert json.loads(output.read_text(encoding="utf-8"))["trade_date"] == EXPECTED


@pytest.mark.parametrize(
    "invalid_fetched_at",
    [
        "2026-08-20T15:35:00+08:00",
        "2026-08-21T15:29:59+08:00",
        "2026-08-21T15:50:01+08:00",
    ],
)
def test_repair_rejects_noncredible_fresh_timestamp(
    monkeypatch, tmp_path, invalid_fetched_at
):
    output = tmp_path / "index_closes_latest.json"
    payload = _probe_payload()
    payload.update(
        {
            "schema_version": updater.SCHEMA_VERSION,
            "trade_date": EXPECTED,
            "stale": False,
            "source": "tencent",
            "fetched_at": invalid_fetched_at,
        }
    )
    output.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    calls: list[str] = []

    def probe(expected: str):
        calls.append(expected)
        return _probe_payload()

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
        probe=probe,
    )

    assert calls == [EXPECTED]
    assert status["mode"] == "repair"


def test_repair_accepts_delayed_fresh_timestamp_after_expected_close(
    monkeypatch, tmp_path
):
    output = tmp_path / "index_closes_latest.json"
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    # Write through the real publisher so the late fetch carries its receipt.
    updater.refresh(
        output,
        now=datetime(2026, 8, 22, 10, 0, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(),
    )
    original = output.read_bytes()

    def must_not_probe(_expected: str):
        raise AssertionError("valid delayed cache must not probe")

    status = updater.repair_if_stale(
        output,
        now=datetime(2026, 8, 22, 10, 3, tzinfo=TZ),
        probe=must_not_probe,
    )

    assert status["mode"] == "repair_noop"
    assert output.read_bytes() == original


def test_repair_refuses_future_cache_without_probe_or_write(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    original = b'{"trade_date":"2026-08-24","closes":[]}\n'
    output.write_bytes(original)
    original_mtime = output.stat().st_mtime_ns
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    def must_not_probe(_expected: str):
        raise AssertionError("future cache must fail before probing")

    with pytest.raises(RuntimeError, match="refusing to regress a future cache"):
        updater.repair_if_stale(
            output,
            now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
            probe=must_not_probe,
        )

    assert output.read_bytes() == original
    assert output.stat().st_mtime_ns == original_mtime


def test_repair_failed_source_preserves_last_known_good_bytes(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    original = b'{"trade_date":"2026-08-20","closes":[]}\n'
    output.write_bytes(original)
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    def fail(_expected: str):
        raise RuntimeError("upstream unavailable")

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        updater.repair_if_stale(
            output,
            now=datetime(2026, 8, 21, 15, 45, tzinfo=TZ),
            probe=fail,
        )
    assert output.read_bytes() == original


def test_repair_main_returns_nonzero_and_reports_mode_on_failure(
    monkeypatch, tmp_path, capsys
):
    output = tmp_path / "index_closes_latest.json"
    original = b'{"trade_date":"2026-08-20","closes":[]}\n'
    output.write_bytes(original)

    def fail(*_args, **_kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(updater, "repair_if_stale", fail)
    rc = updater.main(["--repair-if-stale", "--output", str(output)])
    status = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert status["status"] == "error"
    assert status["mode"] == "repair"
    assert output.read_bytes() == original


def test_failed_probe_preserves_last_known_good_bytes(monkeypatch, tmp_path):
    output = tmp_path / "index_closes_latest.json"
    original = b'{"trade_date":"2026-08-20","closes":[]}\n'
    output.write_bytes(original)
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)

    def fail(_expected: str):
        raise RuntimeError("upstream unavailable")

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        updater.refresh(
            output,
            now=datetime(2026, 8, 23, 10, 0, tzinfo=TZ),
            probe=fail,
        )
    assert output.read_bytes() == original


def test_weekend_resolves_to_previous_open_day(monkeypatch):
    monkeypatch.setattr(updater.HolidayCalendar, "is_holiday", lambda _day: False)
    assert (
        updater.expected_market_as_of(datetime(2026, 8, 23, 10, 0, tzinfo=TZ))
        == EXPECTED
    )


def test_digest_marks_old_but_valid_cache_stale(monkeypatch, tmp_path):
    cache = tmp_path / "index_closes_latest.json"
    monkeypatch.setattr(updater, "expected_market_as_of", lambda _now: EXPECTED)
    updater.refresh(
        cache,
        now=datetime(2026, 8, 21, 15, 35, tzinfo=TZ),
        probe=lambda _expected: _probe_payload(),
    )
    monkeypatch.setattr(research_digest, "INDEX_CLOSE_CACHE", cache)
    monkeypatch.setattr(research_digest, "expected_market_as_of", lambda _now: "2026-08-24")

    state = research_digest._index_close_cache(datetime(2026, 8, 24, 16, 0, tzinfo=TZ))
    assert state["available"] is True
    assert state["values_present"] is True
    assert state["provenance_verified"] is True
    assert state["quality"] == "stale"
    assert state["trade_date"] == EXPECTED
    assert state["expected_as_of"] == "2026-08-24"
    assert state["stale"] is True
    assert len(state["closes"]) == len(updater.REQUIRED_SYMBOLS)


def test_digest_rejects_partial_cache(monkeypatch, tmp_path):
    cache = tmp_path / "index_closes_latest.json"
    payload = _probe_payload()
    payload["closes"].pop()
    cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(research_digest, "INDEX_CLOSE_CACHE", cache)
    monkeypatch.setattr(research_digest, "expected_market_as_of", lambda _now: EXPECTED)
    assert research_digest._index_close_cache()["available"] is False
