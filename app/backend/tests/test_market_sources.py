"""qp.market.sources: parsers, fallbacks and the probe matrix."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from qp.market import sources
from qp.market.http import FetchError


class Resp:
    def __init__(self, payload=None, *, text=None, body=b"", bad_json=False):
        self._payload = payload
        self._text = text
        self.body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload

    def text(self, _enc="utf-8"):
        return self._text if self._text is not None else self.body.decode()


def test_dolt_helpers(monkeypatch):
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append(kwargs)
        if "MAX(" in kwargs["params"]["q"]:
            return Resp({"query_execution_status": "Success", "rows": [{"latest": "2026-11-21"}]})
        return Resp(
            {
                "query_execution_status": "Success",
                "rows": [
                    {"date": "2026-11-20", "is_open": "1"},
                    {"date": "2026-11-21", "is_open": "1"},
                ],
            }
        )

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    rows = sources.dolt_calendar(dt.date(2026, 11, 20), dt.date(2026, 11, 21))
    assert rows == [(dt.date(2026, 11, 20), True), (dt.date(2026, 11, 21), True)]
    assert sources.dolt_latest_eod() == "2026-11-21"
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"query_execution_status": "Bad"}))
    with pytest.raises(FetchError, match="dolt_query_bad"):
        sources.dolt_sql("SELECT 1")


def test_index_parsers(monkeypatch):
    tencent = {"data": {"sh000001": {"qfqday": [["2026-11-20", "1", "2.5"]]}}}
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(tencent))
    assert sources.tencent_index("sh000001") == [("2026-11-20", 2.5)]

    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({}, bad_json=True))
    with pytest.raises(FetchError, match="tencent_not_json"):
        sources.tencent_index("sh000001")
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"data": {}}))
    with pytest.raises(FetchError, match="tencent_empty"):
        sources.tencent_index("sh000001")

    sina = [{"day": "2026-11-20", "close": "3.5"}]
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text=json.dumps(sina)))
    assert sources.sina_index("sh000001") == [("2026-11-20", 3.5)]
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text="not json"))
    with pytest.raises(FetchError, match="sina_not_json"):
        sources.sina_index("sh000001")
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text="[]"))
    with pytest.raises(FetchError, match="sina_empty"):
        sources.sina_index("sh000001")


def test_name_lookup_fallbacks(monkeypatch):
    pages = [
        Resp({"data": {"total": 2, "diff": [{"f12": "600000", "f13": 1, "f14": "浦发银行"}]}}),
        Resp({"data": {"total": 2, "diff": [{"f12": "000001", "f13": 0, "f14": "平安银行"}]}}),
    ]
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: pages.pop(0))
    assert sources.eastmoney_names(page_size=1) == {"SH600000": "浦发银行", "SZ000001": "平安银行"}
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"data": {"total": 0, "diff": []}}))
    with pytest.raises(FetchError, match="eastmoney_empty"):
        sources.eastmoney_names()

    quote = 'v_sh600000="1~浦发银行~x";v_sz000001="1~平安银行~x";v_bj430017="1~星昊医药~x";'
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text=quote))
    assert sources.tencent_names(["SH600000", "SZ000001", "BJ430017"]) == {
        "SH600000": "浦发银行",
        "SZ000001": "平安银行",
        "BJ430017": "星昊医药",
    }
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text=""))
    with pytest.raises(FetchError, match="tencent_names_empty"):
        sources.tencent_names(["SH600000"])
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp(text='v_sh600000="1~浦发银行~x";'))
    with pytest.raises(FetchError, match="tencent_names_partial"):
        sources.tencent_names(["SH600000", "SZ000001"])


def test_cninfo_and_github_release(monkeypatch):
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"totalAnnouncement": 12}))
    assert sources.cninfo_count(dt.date(2026, 11, 21)) == 12
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"totalRecordNum": 9}))
    assert sources.cninfo_count(dt.date(2026, 11, 21)) == 9
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"totalAnnouncement": "x"}))
    with pytest.raises(FetchError, match="cninfo_shape"):
        sources.cninfo_count(dt.date(2026, 11, 21))
    monkeypatch.setattr(sources, "fetch", lambda *_a, **_k: Resp({"tag_name": "v1"}))
    assert sources.github_release_tag() == "v1"


def test_probe_dolt_retries_only_transport_errors(monkeypatch):
    attempts = []

    def flaky(*_a, **_k):
        attempts.append(1)
        if len(attempts) == 1:
            raise FetchError("TimeoutError")
        return Resp({"query_execution_status": "Success", "rows": [{"one": 1}]})

    monkeypatch.setattr(sources, "fetch", flaky)
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    assert sources.probe_dolt() == {"rows": 1, "attempts": 2}

    def rejected(*_a, **_k):
        raise FetchError("dolt_query_bad")

    monkeypatch.setattr(sources, "fetch", rejected)
    with pytest.raises(FetchError, match="dolt_query_bad"):
        sources.probe_dolt()


def test_probe_all_success_matrix(monkeypatch):
    def fake_fetch(url, **kwargs):
        if url == sources.DOLT:
            return Resp({"query_execution_status": "Success", "rows": [{"one": 1}]})
        if url == sources.TENCENT_KLINE:
            return Resp({"data": {"sh000001": {"qfqday": [["2026-11-20", "1", "2.5"]]}}})
        if url.startswith(sources.TENCENT_QUOTE):
            return Resp(text='v_sh600000="1~浦发银行~x";v_sz000001="1~平安银行~x";v_bj430017="1~星昊医药~x";')
        if url == sources.SINA_KLINE:
            return Resp(text=json.dumps([{"day": "2026-11-20", "close": "3.5"}]))
        if url == sources.SINA_CAL:
            return Resp(body=b"calendar")
        if url == sources.EM_CLIST:
            return Resp({"data": {"total": 5000}})
        if url == sources.CNINFO_QUERY:
            return Resp({"totalAnnouncement": 12})
        if url == sources.GH_RELEASE:
            return Resp({"tag_name": "v1"})
        raise AssertionError(url)

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    monkeypatch.setattr(sources, "tcp_probe", lambda *_a, **_k: (True, 1.0, None))
    out = sources.probe_all()
    assert {row["source"] for row in out} == {
        "tencent",
        "sina",
        "akshare_sina_calendar",
        "eastmoney",
        "cninfo",
        "dolt",
        "github_release",
        "baostock",
    }
    assert all(row["ok"] for row in out)


def test_retryable_dolt_error_classification():
    assert sources._retryable_dolt_error(FetchError("http_503"))
    assert not sources._retryable_dolt_error(FetchError("dolt_query_bad"))
