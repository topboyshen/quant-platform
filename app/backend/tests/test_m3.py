import datetime as dt
import io
import json
import sqlite3
import struct
import sys
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

CAL = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]


def wbin(p: Path, start: int, vals):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(struct.pack("<f", start) + b"".join(struct.pack("<f", v) for v in vals))


@pytest.fixture()
def legacy(tmp_path):
    root = tmp_path / "qlib/cn_data"
    (root / "calendars").mkdir(parents=True)
    (root / "calendars/day.txt").write_text("\n".join(CAL) + "\n")
    (root / "instruments").mkdir()
    insts = [("SH600000", "2026-11-02", "2026-11-10"), ("SZ000001", "2026-11-02", "2026-11-10"),
             ("SH600519", "2026-11-02", "2026-11-10"), ("SZ300750", "2026-11-02", "2026-11-10"),
             ("SH688999", "2026-11-10", "2026-11-10")]
    (root / "instruments/all.txt").write_text("".join(f"{a}\t{b}\t{c}\n" for a, b, c in insts))
    (root / "instruments/csi300.txt").write_text("SH600000\t2026-11-02\t2026-11-10\nSH600519\t2026-11-02\t2026-11-10\n")
    closes = {"sh600000": [10, 10.5, 10.4, 10.6, 10.8, 11.0, 11.2], "sz000001": [5, 5, 5, 5, 5, 5, 4.9],
              "sh600519": [100] * 7, "sz300750": [200, 201, 202, 203, 204, 205, 206]}
    for s, c in closes.items():
        for f, v in (("close", c), ("high", [x * 1.01 for x in c]), ("low", [x * 0.99 for x in c]), ("amount", [1e6] * 7)):
            wbin(root / f"features/{s}/{f}.day.bin", 0, v)
    for f in ("close", "high", "low", "amount"):
        wbin(root / f"features/sh688999/{f}.day.bin", 6, [30.0])
    cal_json = tmp_path / "trading_calendar.json"
    cal_json.write_text(json.dumps({"source": "akshare.tool_trade_date_hist_sina", "generated_at": "2026-11-10T06:20:00+08:00",
                                    "trading_dates": ["2026-10-30"] + CAL + ["2026-11-11", "2026-11-12"]}))
    ic = tmp_path / "index_closes_latest.json"
    ic.write_text(json.dumps({"trade_date": "2026-11-10", "fetched_at": "2026-11-10T15:35:05+08:00", "stale": False,
                              "closes": [{"symbol": s, "trade_date": "2026-11-10", "close": v, "source": "tencent"} for s, v in
                                         (("sh000001", 3999.1234), ("sh000300", 4700.5), ("sz399001", 13000.0), ("sz399006", 2494.397))]}))
    core = tmp_path / "core-market.db"
    c = sqlite3.connect(core)
    c.execute("create table core_market(date text primary key, schema text, payload text, payload_sha256 text)")
    import hashlib
    from qp.collect.market import qlib_summary
    summ = qlib_summary(root, CAL, [list(x) for x in insts], "2026-11-10")
    payload = json.dumps({"generatedAt": "2026-11-10T22:30:02+08:00", "qlib": {k: summ[k] for k in
                          ("date", "coverage", "breadth", "topGainers", "topLosers", "topAmount")}})
    c.execute("insert into core_market values (?,?,?,?)", ("2026-11-10", "research.core-market.v1", payload,
                                                           hashlib.sha256(payload.encode()).hexdigest()))
    c.commit(); c.close()
    snapdb = tmp_path / "snapshot.db"
    c = sqlite3.connect(snapdb)
    c.execute("create table snapshot(date text, generated_at text, payload text)")
    c.execute("insert into snapshot values ('2026-09-29','2026-09-29T22:30:00+08:00', ?)", (json.dumps({"akshare": 1, "qlib": {}}),))
    c.commit(); c.close()
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"status": "critical", "businessMode": "FROZEN", "effectiveTradeDate": "2026-10-08",
                                  "generatedAt": "2026-11-10T22:45:00+08:00", "issues": [{"code": "x", "severity": "warning"}]}))
    return {"calendar": cal_json, "index_close": ic, "qlib": root, "qlib_status": tmp_path / "none.json", "core_db": core,
            "snapshot_db": snapdb, "health": health, "core_last_run": tmp_path / "none2.json", "cninfo_staging": tmp_path}


def test_reader_and_summary(legacy):
    from qp.collect.market import collect
    snap = collect(legacy)
    assert snap["calendar"]["rows"] == 10 and snap["index_close"]["rows"][0]["symbol"] == "sh000001"
    assert snap["qlib"]["reference_dates"]["sh600000"] == "2026-11-10"
    s = {x["date"]: x for x in snap["qlib_summary"]}["2026-11-10"]
    # sh688999 listed on the day -> not eligible; sh600000 up, sz000001 down, sh600519 flat, sz300750 up
    assert s["coverage"] == {"valid": 4, "eligible": 4, "invalid": 0, "ratio": 1.0}
    assert s["breadth"] == {"up": 2, "down": 1, "flat": 1, "total": 4}
    assert snap["core_market"]["rows"][0]["payload_sha256"] == snap["core_market"]["rows"][0]["computed_sha256"]
    assert "payload" not in json.dumps(snap["snapshot_db"]["rows"][0]["keys"])


def test_board():
    from qp.jobs.market import board_of
    assert [board_of(x) for x in ("SH600000", "SH688001", "SZ300750", "BJ920085", "SH000300", "SZ399006", "SZ000001")] == \
        ["main", "star", "chinext", "bse", "index", "index", "main"]


@pytest.fixture()
def market_env(engine, legacy, tmp_path, monkeypatch):
    import qp.jobs.market as m
    from qp.collect.market import collect
    inbox = tmp_path / "market-legacy.json"
    inbox.write_text(json.dumps(collect(legacy)))
    monkeypatch.setattr(m, "MARKET_INBOX", inbox)
    days = [dt.date(2026, 10, 30)] + [dt.date.fromisoformat(x) for x in CAL] + [dt.date(2026, 11, 11), dt.date(2026, 11, 12)]
    allc = []
    x = dt.date(2026, 10, 1)
    while x <= dt.date(2026, 12, 31):
        allc.append((x, x in days or (x > dt.date(2026, 11, 12) and x.weekday() < 5)))
        x += dt.timedelta(days=1)
    monkeypatch.setattr(m.src, "dolt_calendar", lambda a, b: allc)
    monkeypatch.setattr(m.src, "tencent_index", lambda sym, n: [("2026-11-09", 1.0), ("2026-11-10",
                        {"sh000001": 3999.1234, "sh000300": 4700.5, "sz399001": 13000.0, "sz399006": 2494.397}[sym])])
    monkeypatch.setattr(m.src, "eastmoney_names", lambda: {"SH600000": "浦发银行", "SZ000001": "平安银行", "SH600519": "贵州茅台",
                                                         "SZ300750": "宁德时代", "SH688999": "测试"})
    monkeypatch.setattr(m.src, "tencent_names", lambda syms: {"SH600000": "浦发银行", "SZ000001": "平安银行",
                                                             "SH600519": "贵州茅台", "SZ300750": "宁德时代",
                                                             "SH688999": "测试"})
    monkeypatch.setattr(m.src, "probe_all", lambda: [{"source": "tencent", "ok": True, "via_proxy": True, "ms": 5, "detail": {}},
                                                     {"source": "cninfo", "ok": False, "via_proxy": False, "ms": 9,
                                                      "detail": {"error": "http_403"}}])
    monkeypatch.setattr(m.src, "dolt_latest_eod", lambda: "2026-11-10")
    monkeypatch.setattr(m.src, "github_release_tag", lambda: "2026-11-10")
    monkeypatch.setattr(m.src, "cninfo_count", lambda day: 1271)
    return m


def test_import_compare_health(engine, market_env):
    from qp.jobs import run_job
    from qp.models.market import CompareResult, HealthResult, ImportLog, Instrument
    from qp.models.ops import Alert
    from qp.models.notify import Outbox
    for name in ("market.calendar", "market.index_close", "market.instruments", "market.sources", "market.cninfo",
                 "market.qlib_check", "market.ingest_legacy"):
        r = run_job(engine, name, trigger="test")
        assert r.status == "succeeded", (name, r.error, r.result)
    with Session(engine) as s:
        logs = s.execute(select(ImportLog)).scalars().all()
        assert {x.item for x in logs} >= {"calendar", "index_close", "qlib_calendar", "qlib_instruments", "qlib_members:csi300",
                                          "core_market", "snapshot_db"}
        assert all(x.status == "match" for x in logs), [(x.item, x.legacy_rows, x.db_rows) for x in logs if x.status != "match"]
        assert s.get(Instrument, "SH600000").name == "浦发银行" and s.get(Instrument, "SH600000").in_qlib
    assert run_job(engine, "market.compare", trigger="test").status == "succeeded"
    with Session(engine) as s:
        res = {r.item: r.status for r in s.execute(select(CompareResult)).scalars()}
        assert res["calendar.legacy_vs_qp"] == "match", res
        assert res["calendar.qlib_vs_qp"] == "match"
        assert res["index_close.legacy_vs_qp"] == "match"
        assert res["snapshot.core_vs_qp"] == "match"
        assert not s.execute(select(Alert).where(Alert.key.like("market:compare:%"), Alert.state == "firing")).first()
    # introduce a mismatch: legacy index close differs
    with Session(engine) as s:
        from qp.models.market import IndexClose
        row = s.get(IndexClose, ("qp", "sh000300", dt.date(2026, 11, 10)))
        row.close = 4701.0
        s.commit()
    run_job(engine, "market.compare", trigger="test")
    with Session(engine) as s:
        a = s.execute(select(Alert).where(Alert.key == "market:compare:index_close.legacy_vs_qp")).scalar_one()
        assert a.state == "firing"
        assert s.execute(select(Outbox).where(Outbox.dedup_key.like("alert:market:compare:%"))).scalars().first().mode == "shadow"
    r = run_job(engine, "market.health", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        hs = {h.check: h.status for h in s.execute(select(HealthResult)).scalars()}
        assert hs["collector.market_reader"] == "ok" and hs["sources.reachable"] == "warning"
        assert "legacy.data_health" not in hs and "snapshot.legacy_core_current" not in hs
        assert hs["compare.latest"] == "warning", hs  # bound to the latest successful compare run
        # inventory alerts must not resolve market alerts
    from qp.jobs.inventory import apply_alerts
    with Session(engine) as s:
        # even an explicit recovery key must not let the inventory owner touch a market alert
        apply_alerts(s, [], 0, only_resolve={"market:compare:index_close.legacy_vs_qp"})
        s.commit()
        assert s.execute(select(Alert).where(Alert.key == "market:compare:index_close.legacy_vs_qp")).scalar_one().state == "firing"


def test_low_memory_aborts(engine, monkeypatch, market_env):
    from qp.jobs import run_job

    def low_memory():
        # /proc/meminfo is Linux-only; simulate the guard instead of depending
        # on the host's available RAM or OS.
        raise market_env.LowMemory("MemAvailable 1MB < simulated threshold")

    monkeypatch.setattr(market_env, "mem_guard", low_memory)
    r = run_job(engine, "market.cninfo", trigger="test")
    assert r.status == "failed" and "LowMemory" in r.error


def test_market_api(engine, market_env, monkeypatch):
    from fastapi.testclient import TestClient
    from qp.api.main import app
    from qp.cli import main
    from qp.jobs import run_job
    for name in ("market.calendar", "market.index_close", "market.instruments", "market.ingest_legacy", "market.compare",
                 "market.health"):
        run_job(engine, name)
    c = TestClient(app, base_url="https://testserver")
    for pth in ("instruments", "calendar", "index-close", "health", "coverage", "compare", "imports"):
        assert c.get(f"/api/market/{pth}").status_code == 401
    b = bcrypt.hashpw(b"pw-m3", bcrypt.gensalt(rounds=4)).decode()
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"admin\t{b}\n"))
    main(["user", "import-legacy", "--stdin", "--force"])
    r = c.post("/api/auth/login", json={"username": "admin", "password": "pw-m3"})
    c.cookies.set("qp_session", r.cookies.get("qp_session"))
    j = c.get("/api/market/instruments?q=600").json()
    assert j["total"] >= 1 and j["rows"][0]["symbol"].startswith("SH600")
    assert c.get("/api/market/instruments?index=csi300").json()["total"] == 2
    cal = c.get("/api/market/calendar?year=2026").json()
    assert "2026-11-10" in cal["qp"]["open"] and "2026-11-10" in cal["legacy"]["open"] and "2026-11-10" in cal["qlib"]["open"]
    assert any(x["origin"] == "legacy" for x in c.get("/api/market/index-close?days=400").json())
    h = c.get("/api/market/health").json()
    assert h["checks"] and h["legacy_health"]["data"]["status"] == "critical"
    assert c.get("/api/market/coverage").json()["summaries"]
    assert c.get("/api/market/compare").json()
    cm = c.get("/api/market/compare/meta").json()
    assert set(cm["index_close"]) == {"legacy", "qp"} and cm["legacy_chain_stopped_on"] == "2026-11-13"
    assert all(x["status"] == "match" for x in c.get("/api/market/imports").json()["latest"])


def test_instruments_uses_tencent_primary(engine, monkeypatch):
    import qp.jobs.market as m
    from qp.jobs import run_job
    from qp.models.market import Instrument

    with Session(engine) as s:
        for symbol, exch, in_qlib in (("SH600000", "SH", True), ("SZ000001", "SZ", True), ("SH600001", "SH", False)):
            s.merge(Instrument(symbol=symbol, name="旧名", exchange=exch, board="main", in_qlib=in_qlib))
        s.commit()

    seen: list[str] = []

    def tencent(syms: list[str]) -> dict[str, str]:
        seen.extend(syms)
        return {"SH600000": "浦发银行", "SZ000001": "平安银行"}

    def eastmoney_unexpected():
        raise AssertionError("eastmoney fallback must not run when tencent succeeds")

    monkeypatch.setattr(m.src, "tencent_names", tencent)
    monkeypatch.setattr(m.src, "eastmoney_names", eastmoney_unexpected)
    r = run_job(engine, "market.instruments", trigger="test")
    assert r.status == "succeeded", (r.error, r.result)
    assert r.result == {"names": 2, "source": "tencent"}
    assert {"SH600000", "SZ000001"} <= set(seen)
    assert "SH600001" not in seen  # only in_qlib instruments are requested
    with Session(engine) as s:
        assert s.get(Instrument, "SH600000").name == "浦发银行"
        assert s.get(Instrument, "SZ000001").name_source == "tencent"
        assert s.get(Instrument, "SH600001").name == "旧名"


def test_instruments_falls_back_to_eastmoney(engine, monkeypatch):
    import qp.jobs.market as m
    from qp.jobs import run_job
    from qp.models.market import Instrument

    with Session(engine) as s:
        for symbol, exch, in_qlib in (("SH600000", "SH", True), ("SZ000001", "SZ", True), ("SH600001", "SH", False)):
            s.merge(Instrument(symbol=symbol, name="旧名", exchange=exch, board="main", in_qlib=in_qlib))
        s.commit()

    def tencent_down(syms: list[str]) -> dict[str, str]:
        raise m.src.FetchError("tencent_names_batch_1:RemoteDisconnected")

    def eastmoney() -> dict[str, str]:
        return {"SH600000": "浦发银行", "SZ000001": "平安银行", "SH600001": "老八股"}

    monkeypatch.setattr(m.src, "tencent_names", tencent_down)
    monkeypatch.setattr(m.src, "eastmoney_names", eastmoney)
    r = run_job(engine, "market.instruments", trigger="test")
    assert r.status == "succeeded", (r.error, r.result)
    assert r.result == {"names": 3, "source": "eastmoney",
                        "tencent_error": "tencent_names_batch_1:RemoteDisconnected"}
    with Session(engine) as s:
        assert s.get(Instrument, "SH600000").name == "浦发银行"
        assert s.get(Instrument, "SZ000001").name_source == "eastmoney"
        assert s.get(Instrument, "SH600001").name == "老八股"


def test_source_verdict_eastmoney_covered_by_tencent():
    from types import SimpleNamespace

    from qp.jobs.market import source_verdict

    up = SimpleNamespace(ok=True)
    down = SimpleNamespace(ok=False)
    bad, covered = source_verdict({"eastmoney": down, "tencent": up}, calendar_covered=False)
    assert bad == []
    assert covered["eastmoney"] == ["tencent"]
    bad, covered = source_verdict({"eastmoney": down, "tencent": down}, calendar_covered=False)
    assert bad == ["eastmoney", "tencent"]
    assert covered["eastmoney"] == []
