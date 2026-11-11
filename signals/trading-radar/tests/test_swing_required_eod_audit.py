"""Real EOD/run/store audit integration, only synthetic data and local SQLite."""
import json
import logging
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

import radar_swing_eod as eod
from radar.data.market_data_v2 import MarketDataFetcher
from radar.swing.store import MacroSlice, SwingStore

DAY = "2026-09-10"

@pytest.mark.parametrize("fresh_count,expected_status", [(0,"failed"),(5,"partial"),(10,"success")])
def test_real_eod_persists_failure_not_false_no_signal(tmp_path,monkeypatch,fresh_count,expected_status):
    codes=["510300","510500","159915","588000","512480","512890","513180","512000","512170","518880"]
    cfg={"telegram":{"bot_token":"DISABLED","chat_id":"DISABLED"},
         "market":{"morning_start":"09:30","morning_end":"11:30","afternoon_start":"13:00","afternoon_end":"15:00"},
         "swing":{"decision_time":"15:30 Asia/Shanghai","log_file":str(tmp_path/"test.log"),"store_file":str(tmp_path/"swing.db"),
            "history_days":250,"ma_periods":[20,60],"volume_breakout_ratio":1.5,
            "technical":{"weekly_macd_golden_cross":True,"volume_breakout_ma":60},
            "macro":{"extreme_percentile_low":5,"extreme_percentile_high":95,"min_history_days":20}},
         "targets":[{"code":c,"name":c,"mode":"trend","sector":"宽基"} for c in codes]}
    p=tmp_path/"config.json";p.write_text(json.dumps(cfg))
    monkeypatch.setattr(SwingStore,"today_str",staticmethod(lambda:DAY))
    monkeypatch.setattr(eod,"ScanScheduler",lambda *a:SimpleNamespace(is_trading_day=lambda:True))
    monkeypatch.setattr(eod,"_setup_logging",lambda *a:logging.getLogger("eod-fixture"))
    monkeypatch.setattr(eod,"_check_runtime_dependencies",lambda *a:{"akshare":True,"requests":True,"store_writable":True})
    monkeypatch.setattr(eod,"_load_eod_risk_state",lambda *a,**kw:{"level":"ok","message":"synthetic"})
    class Collector:
        def __init__(self,store,*a,**kw):self.store=store
        def collect(self,*,persist):
            macro=MacroSlice(date=DAY,source="synthetic")
            if persist:self.store.upsert(macro)
            return macro
        def detect_extremes(self,*a):return []
    monkeypatch.setattr(eod,"MacroSnapshotCollector",Collector)
    calls=[]
    class Notifier:
        def __init__(self,*a,**kw):pass
        def send_datasource_alert(self,*a,**kw):calls.append((a,kw));return True
        def send_macro_alert(self,*a,**kw):raise AssertionError("no macro alerts expected")
        def send_trend_alert(self,*a,**kw):raise AssertionError("no synthetic signal expected")
    monkeypatch.setattr(eod,"TelegramNotifier",Notifier)
    observed=[]
    f=MarketDataFetcher(logging.getLogger("fixture-fetch"),use_akshare=False)
    monkeypatch.setattr(f,"_try_load_parquet",lambda *a:None)
    def data(code,days):
        target=DAY if codes.index(code)<fresh_count else "2026-09-09"
        return pd.DataFrame({"date":pd.date_range(end=target,periods=260),"open":10.,"high":11.,"low":9.,"close":10.,"volume":1000})
    monkeypatch.setattr(f,"_fetch_eastmoney_kline",data)
    monkeypatch.setattr(f,"_fetch_tencent_kline",lambda *a:None)
    monkeypatch.setattr(f,"_fetch_akshare_kline",lambda *a,**kw:None)
    monkeypatch.setattr(f,"_normalize_and_persist",lambda code,df,**kw:df)
    original=f.get_historical_data
    def observed_fetch(code,days=60,**kwargs):
        observed.append(kwargs.get("required_date"))
        return original(code,days,**kwargs)
    monkeypatch.setattr(f,"get_historical_data",observed_fetch)
    monkeypatch.setattr(eod,"MarketDataFetcher",lambda *a,**kw:f)
    result=eod.run(str(p))
    assert observed==[DAY]*10
    assert result["audit_status"]==expected_status
    assert result["trend_data_ok"]==fresh_count
    assert len(result["trend_data_failed"])==10-fresh_count
    run=SwingStore(cfg["swing"]["store_file"]).get_latest_eod_run()
    assert run["status"]==expected_status
    assert run["evaluated_count"]==fresh_count and run["failed_count"]==10-fresh_count
    assert run["signal_count"]==0
    assert run["decision_outcome"]==("no_signal" if fresh_count==10 else "data_unavailable")
    with sqlite3.connect(cfg["swing"]["store_file"]) as db:
        rows=db.execute("select data_status,decision_state,data_date from eod_target_decision order by code").fetchall()
        assert len(rows)==10
        assert sum(s=="failed" and st=="data_failed" for s,st,d in rows)==10-fresh_count
        assert all(d==DAY for s,st,d in rows if s=="ok")
        assert db.execute("select count(*) from trend_signal").fetchone()[0]==0
    # Since 9fd6f25 the terminal 15:50 daily report carries the kline gap;
    # the EOD run no longer sends datasource alerts itself.
    assert calls==[]
    assert result["sent"]==0
    assert result["deferred_to_daily_report"] is (fresh_count==0)
    if fresh_count==0:
        assert any("ETF 趋势确认信号未完成" in fact for fact in result["facts"])
    elif fresh_count==5:
        assert any("ETF历史K线部分失败" in fact for fact in result["facts"])
