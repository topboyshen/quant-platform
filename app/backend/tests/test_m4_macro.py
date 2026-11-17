"""Macro slice and macro-risk parity fixtures (pure parsers + fake HTTP routes)."""
import datetime as dt

import pytest

from qp.signals import macro


class FakeResp:
    def __init__(self, payload=None, text: str = ""):
        self._payload = payload
        self.body = text.encode("utf-8")

    def json(self):
        return self._payload

    def text(self, enc: str = "utf-8") -> str:
        return self.body.decode(enc, errors="replace")


def tencent_quote(code: str, observed: str, amount_wan: float) -> dict:
    quote = [""] * 38
    quote[30] = observed.replace("-", "") + "150000"
    quote[37] = str(amount_wan)
    return {"data": {code: {"qt": {code: quote},
                            "qfqday": [[observed, "1", "1", "1", "1", "1"]]}}}


def sina_text(sh_yuan: float, sz_yuan: float, observed: str) -> str:
    def fields(amount: float) -> str:
        values = [""] * 33
        values[0] = "index"
        values[9] = f"{amount:.2f}"
        values[30] = observed
        return ",".join(values)

    return (f'var hq_str_sh000001="{fields(sh_yuan)}";\n'
            f'var hq_str_sz399001="{fields(sz_yuan)}";\n')


def test_parse_tencent_total_requires_matching_quote_and_bar():
    payload = tencent_quote("sh000001", "2026-11-12", 60_000_000.0)
    payload["data"]["sz399001"] = tencent_quote("sz399001", "2026-11-12", 50_000_000.0)["data"]["sz399001"]
    assert macro.parse_tencent_total(payload) == (11000.0, "2026-11-12")

    payload["data"]["sz399001"]["qt"]["sz399001"][30] = "20261111150000"
    with pytest.raises(ValueError):
        macro.parse_tencent_total(payload)


def test_parse_sina_total_units_and_component_rules():
    text = sina_text(600_000_000_000.0, 500_000_000_000.0, "2026-11-12")
    assert macro.parse_sina_total(text) == (11000.0, "2026-11-12")
    with pytest.raises(ValueError):
        macro.parse_sina_total('\n'.join(text.splitlines()[:1]))
    with pytest.raises(ValueError):
        macro.parse_sina_total(text.replace("2026-11-12", "2026-11-11", 1))


def test_parse_margin_and_limit_payloads():
    sse = {"result": [
        {"opDate": "20261111", "rzrqjyzl": 1_193_012_000_000},
        {"opDate": "20261110", "rzrqjyzl": 1_188_312_000_000},
        {"opDate": "bad", "rzrqjyzl": 1},
    ]}
    assert macro.parse_sse_margin(sse) == [("2026-11-11", 11930.12), ("2026-11-10", 11883.12)]
    assert macro.parse_szse_margin([{"data": [{"jrrzrjye": "10,940.38"}]}]) == 10940.38
    assert macro.parse_limit_pool({"data": {"tc": 54}}) == 54
    assert macro.parse_limit_pool({"data": {"pool": [1, 2, 3]}}) == 3
    with pytest.raises(ValueError):
        macro.parse_limit_pool({"data": {}})


def _macro_fetch(observed_total: str = "2026-11-12"):
    sse = {"result": [
        {"opDate": "20261111", "rzrqjyzl": 1_193_012_000_000},
        {"opDate": "20261110", "rzrqjyzl": 1_188_312_000_000},
    ]}
    szse_by_date = {"2026-11-11": "10,940.38", "2026-11-10": "10,816.88"}

    def fake(url, *, params=None, **kwargs):
        params = params or {}
        if url == macro.TENCENT_QUOTE_URL:
            code = str(params["param"]).split(",")[0]
            amount = 60_000_000.0 if code == "sh000001" else 50_000_000.0
            return FakeResp(tencent_quote(code, observed_total, amount))
        if url == macro.SSE_MARGIN_URL:
            return FakeResp(sse)
        if url == macro.SZSE_MARGIN_URL:
            return FakeResp([{"data": [{"jrrzrjye": szse_by_date[params["txtDate"]]}]}])
        if url == macro.ZT_POOL_URL:
            return FakeResp({"data": {"tc": 54}})
        if url == macro.DT_POOL_URL:
            return FakeResp({"data": {"tc": 1}})
        raise AssertionError(url)

    return fake


def test_collect_macro_slice_matches_legacy_units_and_formulas():
    got = macro.collect_macro_slice("2026-11-12", fetch_fn=_macro_fetch())
    assert got["total_amount"] == 11000.0
    assert got["margin_balance"] == 22870.5
    assert got["margin_growth_pct"] == 0.751
    assert (got["limit_up"], got["limit_down"], got["limit_ratio"]) == (54, 1, 27.0)
    assert got["source"] == "tencent+sse+szse+eastmoney"
    assert all(status["available"] and not status["stale"] and not status["anomalous"]
               for status in got["collect_status"].values())


def test_stale_total_amount_is_kept_and_flagged():
    got = macro.collect_macro_slice("2026-11-12", fetch_fn=_macro_fetch("2026-11-11"))
    assert got["total_amount"] == 11000.0
    status = got["collect_status"]["total_amount"]
    assert status["stale"] is True and status["observed_date"] == "2026-11-11"


def test_total_amount_falls_back_to_sina_and_rejects_out_of_range():
    def tencent_down(url, **kwargs):
        raise RuntimeError("proxy")

    def sina_route(url, **kwargs):
        return FakeResp(text=sina_text(600_000_000_000.0, 500_000_000_000.0, "2026-11-12"))

    def route(url, **kwargs):
        return sina_route(url, **kwargs) if "sinajs" in url else tencent_down(url)

    assert macro.collect_total_amount("2026-11-12", route)[0] == 11000.0

    def tiny(url, **kwargs):
        if url == macro.TENCENT_QUOTE_URL:
            code = str(kwargs["params"]["param"]).split(",")[0]
            return FakeResp(tencent_quote(code, "2026-11-12", 1_000.0))  # 0.1 亿
        if "sinajs" in url:
            return FakeResp(text=sina_text(1_000_000.0, 1_000_000.0, "2026-11-12"))
        raise RuntimeError("eastmoney")

    value, status = macro.collect_total_amount("2026-11-12", tiny)
    assert value is None and status.available is False and status.anomalous is True


def test_limit_pool_zero_zero_is_unavailable():
    def zero(url, **kwargs):
        return FakeResp({"data": {"tc": 0}})

    up, down, status = macro.collect_limits("2026-11-12", zero)
    assert (up, down) == (None, None)
    assert status.available is False and status.message == "empty_pool"


def test_evaluate_risk_threshold_order():
    assert macro.evaluate_risk({"vix": 18.0, "us_10y": 5.0, "crude_oil": 95.0})["level"] == "critical"
    assert macro.evaluate_risk({"vix": None, "us_10y": None, "crude_oil": None})["level"] == "warning"
    assert macro.evaluate_risk({"vix": 15.0, "us_10y": 4.55, "crude_oil": 70.0})["level"] == "growth_blocked"
    assert macro.evaluate_risk({"vix": 15.0, "us_10y": 4.0, "crude_oil": 88.6})["level"] == "growth_blocked"
    partial = macro.evaluate_risk({"vix": 15.0, "us_10y": 4.0, "crude_oil": None})
    assert partial["level"] == "warning" and partial["missing_fields"] == ["crude_oil"]
    assert macro.evaluate_risk({"vix": 15.0, "us_10y": 4.0, "crude_oil": 70.0})["level"] == "ok"


def test_evaluate_risk_reuses_fresh_observations_only():
    now = 1_000_000.0
    previous = {
        "vix": 15.0,
        "us_10y": 4.0,
        "crude_oil": 70.0,
        "observed_at": {"vix": now - 100, "us_10y": now - 100, "crude_oil": now - 100},
        "sources": {"vix": "yahoo_chart_regular_market"},
    }
    got = macro.evaluate_risk({}, previous_state=previous, now=now)
    assert got["level"] == "ok"
    assert sorted(got["cached_fields"]) == ["crude_oil", "us_10y", "vix"]
    assert got["missing_fields"] == []

    stale = macro.evaluate_risk(
        {}, previous_state={"vix": 15.0, "observed_at": {"vix": now - 901}}, now=now)
    assert stale["level"] == "warning"
    assert stale["cached_fields"] == []
    assert sorted(stale["missing_fields"]) == ["crude_oil", "us_10y", "vix"]

    future = macro.evaluate_risk(
        {}, previous_state={"vix": 15.0, "observed_at": {"vix": now + 10}}, now=now)
    assert future["vix"] is None


def test_fetch_cboe_and_fred_values():
    ts = 1_700_000_000.0
    stamp = dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    cboe = FakeResp({"timestamp": stamp, "data": {"current_price": 18.2}})
    value, meta = macro.fetch_cboe_vix(lambda url, **kw: cboe, now=ts + 60)
    assert value == 18.2 and meta["source"] == "cboe_vix_delayed"

    day = dt.datetime.fromtimestamp(ts - 86400, tz=dt.UTC).date().isoformat()
    csv_text = f"observation_date,VIXCLS\n{day},15.4\n"
    value, meta = macro.fetch_fred_value("VIXCLS", lambda url, **kw: FakeResp(text=csv_text), now=ts)
    assert value == 15.4 and meta["source"] == "fred_vixcls_daily"


def test_wti_spot_fallback_is_env_gated(monkeypatch):
    calls: list[str] = []

    def yahoo(symbol, fetch_fn):
        raise ValueError("429")

    def cboe(fetch_fn, now=None):
        raise ValueError("down")

    def fred(series, fetch_fn, now=None):
        calls.append(series)
        if series == "DCOILWTICO":
            return 75.0, {"source": "fred_dcoilwtico_spot_daily",
                          "source_timestamp": 1_000_000.0, "available_at": 1_000_000.0}
        raise ValueError("no")

    monkeypatch.setattr(macro, "fetch_yahoo_value", yahoo)
    monkeypatch.setattr(macro, "fetch_cboe_vix", cboe)
    monkeypatch.setattr(macro, "fetch_fred_value", fred)
    monkeypatch.delenv("QP_RISK_ALLOW_WTI_SPOT_FALLBACK", raising=False)
    got = macro.fetch_risk_values(now=1_000_000.0)
    assert got["values"]["crude_oil"] is None and "DCOILWTICO" not in calls
    assert got["values"]["vix"] is None and got["values"]["us_10y"] is None

    monkeypatch.setenv("QP_RISK_ALLOW_WTI_SPOT_FALLBACK", "1")
    got = macro.fetch_risk_values(now=1_000_000.0)
    assert got["values"]["crude_oil"] == 75.0
    assert got["sources"]["crude_oil"] == "fred_dcoilwtico_spot_daily"


def test_risk_state_freshness():
    now = 1_000_000.0
    assert macro.risk_state_is_fresh(now - 10, now=now)
    assert not macro.risk_state_is_fresh(now - 301, now=now)
    assert not macro.risk_state_is_fresh(now + 2, now=now)
    assert not macro.risk_state_is_fresh(None, now=now)
