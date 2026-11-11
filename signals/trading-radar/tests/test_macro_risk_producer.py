from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.error import URLError

import pytest

from scripts import run_macro_risk_producer as producer


def _state(**overrides):
    payload = {
        "level": "ok",
        "vix": 15.2,
        "us_10y": 4.1,
        "crude_oil": 77.0,
        "message": "SYSTEM_OK",
        "timestamp": time.time(),
        "observed_at": {},
        "source_timestamps": {},
        "available_at": {},
        "sources": {},
        "cached_fields": [],
        "missing_fields": [],
    }
    payload.update(overrides)
    return payload


def test_load_published_state_accepts_fresh_valid_state(tmp_path: Path):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(_state(timestamp=100.0)), encoding="utf-8")

    payload, age = producer.load_published_state(
        path, max_age_seconds=20, now=110.0
    )

    assert payload["level"] == "ok"
    assert age == 10.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"level": "unknown"},
        {"timestamp": 1.0},
        {"vix": 0},
        {"us_10y": float("nan")},
        {"missing_fields": "vix"},
    ],
)
def test_load_published_state_rejects_invalid_or_stale_state(
    tmp_path: Path, overrides
):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(_state(**overrides)), encoding="utf-8")

    with pytest.raises(ValueError):
        producer.load_published_state(path, max_age_seconds=20, now=time.time())


def test_prometheus_metrics_do_not_invent_missing_values(tmp_path: Path, monkeypatch):
    path = tmp_path / "risk.json"
    path.write_text(
        json.dumps(
            _state(
                level="warning",
                vix=None,
                missing_fields=["vix"],
                cached_fields=["crude_oil"],
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RADAR_RISK_STATE_FILE", str(path))

    metrics = producer.prometheus_metrics()

    assert "radar_macro_risk_producer_up 1" in metrics
    assert 'radar_macro_risk_level{level="warning"} 1' in metrics
    assert 'radar_macro_risk_value{field="vix"}' not in metrics
    assert "radar_macro_risk_missing_fields 1" in metrics
    assert "radar_macro_risk_cached_fields 1" in metrics


def test_publish_once_accepts_persistent_controller(tmp_path: Path, monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    path = tmp_path / "risk.json"
    override = tmp_path / "override.json"
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", path)
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", override)
    monkeypatch.setattr(MacroRiskControl, "fetch_vix", classmethod(lambda cls: 16.0))
    monkeypatch.setattr(MacroRiskControl, "fetch_us_10y", classmethod(lambda cls: 4.7))
    monkeypatch.setattr(MacroRiskControl, "fetch_crude_oil", classmethod(lambda cls: 80.0))
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})
    controller = MacroRiskControl(producer.LOGGER)

    payload = producer.publish_once(controller=controller)

    assert payload["level"] == "growth_blocked"
    assert controller._state is not None


def test_publish_once_reuses_existing_risk_logic(tmp_path: Path, monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    path = tmp_path / "risk.json"
    override = tmp_path / "override.json"
    monkeypatch.setenv("RADAR_RISK_STATE_FILE", str(path))
    monkeypatch.setenv("RADAR_RISK_OVERRIDE_FILE", str(override))
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", path)
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", override)
    monkeypatch.setattr(MacroRiskControl, "fetch_vix", classmethod(lambda cls: 16.0))
    monkeypatch.setattr(MacroRiskControl, "fetch_us_10y", classmethod(lambda cls: 4.7))
    monkeypatch.setattr(
        MacroRiskControl, "fetch_crude_oil", classmethod(lambda cls: 80.0)
    )
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    payload = producer.publish_once()

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert payload["level"] == "growth_blocked"
    assert persisted["level"] == "growth_blocked"
    assert persisted["us_10y"] == 4.7


def test_publish_once_preserves_fail_closed_missing_state(tmp_path: Path, monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    path = tmp_path / "risk.json"
    override = tmp_path / "override.json"
    monkeypatch.setenv("RADAR_RISK_STATE_FILE", str(path))
    monkeypatch.setenv("RADAR_RISK_OVERRIDE_FILE", str(override))
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", path)
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", override)
    monkeypatch.setattr(MacroRiskControl, "fetch_vix", classmethod(lambda cls: None))
    monkeypatch.setattr(MacroRiskControl, "fetch_us_10y", classmethod(lambda cls: None))
    monkeypatch.setattr(
        MacroRiskControl, "fetch_crude_oil", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    payload = producer.publish_once()

    assert payload["level"] == "warning"
    assert payload["missing_fields"] == ["vix", "us_10y", "crude_oil"]


class _FredResponse:
    def __init__(self, body: str, url: str, content_type: str, status: int = 200):
        self._body = body.encode("utf-8")
        self._url = url
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self._body)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def geturl(self) -> str:
        return self._url


class _FredOpener:
    def __init__(
        self,
        body: str | None = None,
        error: Exception | None = None,
        content_type: str = "application/csv",
        final_url: str | None = None,
        status: int = 200,
    ):
        self.body = body
        self.error = error
        self.content_type = content_type
        self.final_url = final_url
        self.status = status
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return _FredResponse(
            self.body or "",
            self.final_url or request.full_url,
            self.content_type,
            self.status,
        )


def test_fred_fallback_accepts_recent_value_and_records_provenance(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    now = 1_787_788_800.0  # 2026-08-27T00:00:00Z
    opener = _FredOpener("observation_date,VIXCLS\n" "2026-08-24,.\n" "2026-08-25,15.45\n")
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    value = MacroRiskControl._fetch_fred_api("^VIX", "VIXCLS")

    assert value == 15.45
    assert opener.requests[0][1] == 8
    assert "id=VIXCLS" in opener.requests[0][0].full_url
    assert MacroRiskControl._FETCH_METADATA["^VIX"]["source"] == "fred_vixcls_daily"
    assert MacroRiskControl._FETCH_METADATA["^VIX"]["available_at"] == now
    assert MacroRiskControl._FETCH_METADATA["^VIX"]["source_timestamp"] == 1_787_616_000.0


def test_fred_fallback_rejects_stale_or_unreachable_data(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    now = 1_787_788_800.0
    stale = _FredOpener("observation_date,DGS10\n2026-08-01,4.30\n")
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: stale)
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    assert MacroRiskControl._fetch_fred_api("^TNX", "DGS10") is None
    assert "^TNX" not in MacroRiskControl._FETCH_METADATA

    failed = _FredOpener(error=URLError("network down"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: failed)
    assert MacroRiskControl._fetch_fred_api("^TNX", "DGS10") is None


def test_fred_wti_fallback_identifies_spot_contract(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    now = 1_787_788_800.0
    opener = _FredOpener("observation_date,DCOILWTICO\n2026-08-25,83.90\n")
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    assert MacroRiskControl._fetch_fred_api("CL=F", "DCOILWTICO") == 83.90
    assert MacroRiskControl._FETCH_METADATA["CL=F"]["source"] == "fred_dcoilwtico_spot_daily"


@pytest.mark.parametrize(
    ("method_name", "symbol", "series_id"),
    [
        ("fetch_us_10y", "^TNX", "DGS10"),
        ("fetch_crude_oil", "CL=F", "DCOILWTICO"),
    ],
)
def test_market_fetchers_use_fred_only_after_yahoo_failure(monkeypatch, method_name, symbol, series_id):
    from radar.risk.macro_risk_control import MacroRiskControl

    calls = []
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_yahoo_api",
        classmethod(lambda cls, actual: calls.append(("yahoo", actual)) or None),
    )
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_fred_api",
        classmethod(lambda cls, actual, series: calls.append(("fred", actual, series)) or 1.0),
    )
    monkeypatch.setenv("RADAR_RISK_ALLOW_WTI_SPOT_FALLBACK", "1")

    assert getattr(MacroRiskControl, method_name)() == 1.0
    assert calls == [("yahoo", symbol), ("fred", symbol, series_id)]


def test_wti_spot_fallback_is_fail_closed_without_explicit_opt_in(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    calls = []
    monkeypatch.delenv("RADAR_RISK_ALLOW_WTI_SPOT_FALLBACK", raising=False)
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_yahoo_api",
        classmethod(lambda cls, symbol: None),
    )
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_fred_api",
        classmethod(lambda cls, *args: calls.append(args) or 83.9),
    )

    assert MacroRiskControl.fetch_crude_oil() is None
    assert calls == []


def test_vix_fetcher_uses_cboe_before_fred(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    calls = []
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_yahoo_api",
        classmethod(lambda cls, symbol: calls.append(("yahoo", symbol)) or None),
    )
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_cboe_vix",
        classmethod(lambda cls: calls.append(("cboe",)) or 15.2),
    )
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_fred_api",
        classmethod(lambda cls, *_args: pytest.fail("FRED should not be called")),
    )

    assert MacroRiskControl.fetch_vix() == 15.2
    assert calls == [("yahoo", "^VIX"), ("cboe",)]


def test_cboe_vix_fallback_records_quote_timestamp(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    now = 1_787_788_800.0
    opener = _FredOpener(
        json.dumps(
            {
                "timestamp": "2026-08-26 22:05:48",
                "data": {"current_price": 15.21},
            }
        ),
        content_type="application/json",
    )
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})

    assert MacroRiskControl._fetch_cboe_vix() == 15.21
    metadata = MacroRiskControl._FETCH_METADATA["^VIX"]
    assert metadata["source"] == "cboe_vix_delayed"
    assert metadata["source_timestamp"] == 1_787_781_948.0


def test_direct_fallback_rejects_unexpected_content_type(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    opener = _FredOpener("<html>blocked</html>", content_type="text/html")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)

    assert (
        MacroRiskControl._request_direct_bytes(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
            allowed_host="fred.stlouisfed.org",
            allowed_path="/graph/fredgraph.csv",
            accepted_content_types=("application/csv",),
        )
        is None
    )


def test_direct_fallback_rejects_non_https_or_changed_final_path(monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    opener = _FredOpener(
        "observation_date,DGS10\n2026-08-25,4.64\n",
        final_url="https://fred.stlouisfed.org/unexpected.csv",
    )
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)

    common = {
        "allowed_host": "fred.stlouisfed.org",
        "allowed_path": "/graph/fredgraph.csv",
        "accepted_content_types": ("application/csv",),
    }
    assert (
        MacroRiskControl._request_direct_bytes("http://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", **common)
        is None
    )
    assert opener.requests == []
    assert (
        MacroRiskControl._request_direct_bytes("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", **common)
        is None
    )


def test_publish_once_persists_independent_provider_provenance(tmp_path: Path, monkeypatch):
    from radar.risk.macro_risk_control import MacroRiskControl

    now = 1_787_788_800.0
    state_file = tmp_path / "risk.json"
    override_file = tmp_path / "override.json"
    monkeypatch.setenv("RADAR_RISK_STATE_FILE", str(state_file))
    monkeypatch.setenv("RADAR_RISK_OVERRIDE_FILE", str(override_file))
    monkeypatch.setenv("RADAR_RISK_ALLOW_WTI_SPOT_FALLBACK", "1")
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", state_file)
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", override_file)
    monkeypatch.setattr(MacroRiskControl, "_FETCH_METADATA", {})
    monkeypatch.setattr(
        MacroRiskControl,
        "_fetch_yahoo_api",
        classmethod(lambda cls, _symbol: None),
    )

    def fake_direct(url, **_kwargs):
        if url.endswith("_VIX.json"):
            return json.dumps(
                {
                    "timestamp": "2026-08-26 22:05:48",
                    "data": {"current_price": 15.21},
                }
            ).encode()
        if "id=DGS10" in url:
            return b"observation_date,DGS10\n2026-08-25,4.64\n"
        if "id=DCOILWTICO" in url:
            return b"observation_date,DCOILWTICO\n2026-08-25,83.90\n"
        raise AssertionError(url)

    monkeypatch.setattr(MacroRiskControl, "_request_direct_bytes", staticmethod(fake_direct))

    payload = producer.publish_once()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))

    assert payload["missing_fields"] == []
    assert payload["sources"] == {
        "vix": "cboe_vix_delayed",
        "us_10y": "fred_dgs10_daily",
        "crude_oil": "fred_dcoilwtico_spot_daily",
    }
    assert persisted == payload
