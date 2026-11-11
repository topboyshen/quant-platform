"""Synthetic inputs; real fetcher/strategy modules, fake network boundary."""
import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from radar.data.kline_sources import fetch_akshare_kline, is_usable_kline
from radar.data.market_data_v2 import MarketDataFetcher
from radar.swing.trend_strategy import TrendConfirmStrategy

DAY = "2026-09-10"
LOG = logging.getLogger("date-gate-test")


@pytest.fixture(autouse=True)
def disable_stale_retry(monkeypatch, tmp_path):
    import json

    import radar.data.market_data_v2 as market
    from runtime_contracts.calendar import sessions
    from tests.test_eod_contracts import NOW, calendar_payload

    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "0")
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(calendar_payload()))
    monkeypatch.setenv("RADAR_TRADING_CALENDAR_FILE", str(path))
    monkeypatch.setattr(market, "sessions", lambda payload, now: sessions(payload, NOW))

def frame(day=DAY, n=260):
    close = np.full(n, 10.0)
    close[-1] = 12.0
    volume = np.full(n, 1000000)
    volume[-1] = 5000000
    return pd.DataFrame({"date": pd.date_range(end=day, periods=n), "open":close,
        "high":close + .2,"low":close - .2,"close":close,"volume":volume})

@pytest.mark.parametrize("day,expected", [(DAY,True),("2026-09-09",False),("2026-09-11",False),("2026-09-04",False)])
def test_exact_date_not_general_age(day, expected):
    assert is_usable_kline(frame(day), 250, required_date=DAY) is expected

def test_existing_general_age_contract_unchanged():
    assert is_usable_kline(frame("2026-09-09"),250,now=pd.Timestamp(DAY))

@pytest.mark.parametrize("mutation", ["nan_date","too_short","future_row"])
def test_strict_rejects_invalid_frames(mutation):
    f=frame()
    if mutation=="nan_date": f.loc[0,"date"]=pd.NaT
    elif mutation=="too_short": f=f.tail(29)
    else: f.loc[0,"date"]=pd.Timestamp("2026-09-11")
    assert not is_usable_kline(f,250,required_date=DAY)

def fetcher(monkeypatch, *, cache=None, east=None, tencent=None, ak=None):
    f=MarketDataFetcher(LOG,use_akshare=False)
    written=[]
    monkeypatch.setattr(f,"_try_load_parquet",lambda *a:cache)
    monkeypatch.setattr(f,"_fetch_eastmoney_kline",lambda *a:east)
    monkeypatch.setattr(f,"_fetch_tencent_kline",lambda *a:tencent)
    monkeypatch.setattr(f,"_fetch_akshare_kline",lambda *a,**kw:ak)
    def persist(code,data,*,source,**kwargs):
        written.append((code,source,str(data.date.max().date())))
        return data
    monkeypatch.setattr(f,"_normalize_and_persist",persist)
    return f,written

@pytest.mark.parametrize("where",["cache","east","tencent"])
def test_skips_stale_to_fresh_next_source(monkeypatch,where):
    args={"cache":None,"east":None,"tencent":None,"ak":None}
    order=["cache","east","tencent","ak"]
    args[where]=frame("2026-09-09")
    args[order[order.index(where)+1]]=frame()
    f,written=fetcher(monkeypatch,**args)
    result=f.get_historical_data("510300",250,required_date=DAY)
    assert result.date.max().date().isoformat()==DAY
    assert len(written)==1 and written[0][2]==DAY

def test_all_stale_is_empty_never_persisted(monkeypatch):
    stale=frame("2026-09-09")
    f,written=fetcher(monkeypatch,cache=stale,east=stale,tencent=stale,ak=stale)
    assert f.get_historical_data("510300",250,required_date=DAY).empty
    assert written==[]


def test_publication_lag_waits_once_then_accepts_fresh(monkeypatch):
    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "45")
    stale = frame("2026-09-09")
    fresh = frame()
    slept = []
    calls = {"n": 0}
    f, written = fetcher(monkeypatch, east=stale, tencent=stale, ak=stale)

    def east(*_args):
        calls["n"] += 1
        return fresh if calls["n"] > 1 else stale

    monkeypatch.setattr(f, "_fetch_eastmoney_kline", east)
    monkeypatch.setattr(f, "_sleep", lambda seconds: slept.append(seconds))
    result = f.get_historical_data("510300", 250, required_date=DAY)
    assert result.date.max().date().isoformat() == DAY
    assert slept == [45]
    assert written == [("510300", "eastmoney_push2his", DAY)]


def test_total_outage_does_not_sleep(monkeypatch):
    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "45")
    slept = []
    f, written = fetcher(monkeypatch)
    monkeypatch.setattr(f, "_sleep", lambda seconds: slept.append(seconds))
    assert f.get_historical_data("510300", 250, required_date=DAY).empty
    assert slept == []
    assert written == []


def test_second_symbol_does_not_wait_again(monkeypatch):
    monkeypatch.setenv("RADAR_EOD_STALE_RETRY_SECONDS", "45")
    slept = []
    stale = frame("2026-09-09")
    f, written = fetcher(monkeypatch, east=stale, tencent=stale, ak=stale)
    monkeypatch.setattr(f, "_sleep", lambda seconds: slept.append(seconds))
    assert f.get_historical_data("510300", 250, required_date=DAY).empty
    assert slept == [45]
    assert f.get_historical_data("510500", 250, required_date=DAY).empty
    assert slept == [45]
    assert written == []

def test_network_exception_never_revives_stale_cache(monkeypatch):
    f,written=fetcher(monkeypatch,cache=frame("2026-09-09"))
    def broken(*a): raise ConnectionError("fixture")
    monkeypatch.setattr(f,"_fetch_eastmoney_kline",broken)
    assert f.get_historical_data("510300",250,required_date=DAY).empty
    assert written==[]

def test_matching_cache_needs_no_network_or_write(monkeypatch):
    f,written=fetcher(monkeypatch,cache=frame())
    def denied(*a):raise AssertionError("network must not be attempted")
    monkeypatch.setattr(f,"_fetch_eastmoney_kline",denied)
    assert len(f.get_historical_data("510300",250,required_date=DAY))==260
    assert written==[]

@pytest.mark.parametrize("bad",["20260910","2026-9-10","garbage",""])
def test_invalid_requirement_rejected_before_fetch(monkeypatch,bad):
    f,written=fetcher(monkeypatch)
    def denied(*a):raise AssertionError("must fail before IO")
    monkeypatch.setattr(f,"_try_load_parquet",denied)
    with pytest.raises(ValueError):f.get_historical_data("510300",250,required_date=bad)

def test_akshare_internal_fallback_does_not_stop_at_stale(monkeypatch):
    calls=[]
    class AK:
        def fund_etf_hist_em(self,**kw):calls.append("eastmoney");return frame("2026-09-09")
        def fund_etf_hist_sina(self,**kw):calls.append("sina");return frame()
    got=fetch_akshare_kline(LOG,SimpleNamespace(is_available=True,_ak=AK()),"510300",250,required_date=DAY)
    assert calls==["eastmoney","sina"]
    assert got.date.max().date().isoformat()==DAY

@pytest.mark.parametrize("actual",["2026-09-09","2026-09-11"])
def test_strategy_rechecks_even_if_fetcher_ignores_requirement(actual):
    f=SimpleNamespace(get_historical_data=lambda *a,**k:frame(actual))
    s=TrendConfirmStrategy(f,required_data_date=DAY)
    assert s.evaluate("510300")==[] and not s.last_data_ok
    assert s.last_data_date==actual

def test_normalisation_cannot_remove_today_then_accept_yesterday():
    data=frame();data.loc[len(data)-1,"close"]=float("nan")
    s=TrendConfirmStrategy(SimpleNamespace(get_historical_data=lambda *a,**k:data),required_data_date=DAY)
    assert s.evaluate("510300")==[] and not s.last_data_ok
    assert s.last_data_date=="2026-09-09"

def test_fresh_strategy_preserves_existing_signal_rule():
    received=[]
    def get(*a,**kw):received.append(kw);return frame()
    s=TrendConfirmStrategy(SimpleNamespace(get_historical_data=get),required_data_date=DAY)
    signals=s.evaluate("510300")
    assert s.last_data_ok and s.last_data_date==DAY
    assert [x.signal_type for x in signals]==["volume_breakout_ma60"]
    assert received==[{"days":250,"required_date":DAY}]

def test_existing_strategy_default_contract_unchanged():
    s=TrendConfirmStrategy(SimpleNamespace(get_historical_data=lambda code,days:frame("2025-01-01")))
    assert s.evaluate("510300") and s.last_data_ok
