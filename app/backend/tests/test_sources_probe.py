"""Dolt probe budget/retry behavior and Eastmoney name fallback."""

from __future__ import annotations

import pytest

from qp.market import sources


class _Resp:
    """Minimal response exposing the GBK decode path used by tencent_names."""

    def __init__(self, text: str) -> None:
        self.body = text.encode("gbk")

    def text(self, enc: str = "utf-8") -> str:
        return self.body.decode(enc)


def test_probe_dolt_uses_real_fetch_budget_and_succeeds_first_try(monkeypatch):
    calls: list[tuple[str, float]] = []

    def fake(sql: str, timeout: float = 150) -> list[dict]:
        calls.append((sql, timeout))
        return [{"one": "1"}]

    monkeypatch.setattr(sources, "dolt_sql", fake)
    assert sources.probe_dolt() == {"rows": 1, "attempts": 1}
    assert calls == [("SELECT 1 AS one", sources.DOLT_PROBE_TIMEOUT)]
    assert sources.DOLT_PROBE_TIMEOUT == 180.0


def test_probe_dolt_retries_transient_timeout(monkeypatch):
    calls: list[str] = []

    def fake(sql: str, timeout: float = 150) -> list[dict]:
        calls.append(sql)
        if len(calls) == 1:
            raise sources.FetchError("TimeoutError")
        return [{"one": "1"}]

    monkeypatch.setattr(sources, "dolt_sql", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    assert sources.probe_dolt() == {"rows": 1, "attempts": 2}
    assert len(calls) == 2


def test_probe_dolt_raises_after_last_transient_failure(monkeypatch):
    def fake(sql: str, timeout: float = 150) -> list[dict]:
        raise sources.FetchError("http_504")

    monkeypatch.setattr(sources, "dolt_sql", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    with pytest.raises(sources.FetchError, match="http_504"):
        sources.probe_dolt()


def test_probe_dolt_does_not_retry_query_error(monkeypatch):
    calls: list[str] = []

    def fake(sql: str, timeout: float = 150) -> list[dict]:
        calls.append(sql)
        raise sources.FetchError("dolt_query_failed")

    monkeypatch.setattr(sources, "dolt_sql", fake)
    with pytest.raises(sources.FetchError, match="dolt_query_failed"):
        sources.probe_dolt()
    assert len(calls) == 1


def test_eastmoney_names_non_json_is_a_fetch_error(monkeypatch):
    class _BadJson:
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(sources, "fetch", lambda *a, **kw: _BadJson())
    with pytest.raises(sources.FetchError, match="eastmoney_not_json"):
        sources.eastmoney_names()


def test_tencent_names_parses_gbk_in_batches(monkeypatch):
    calls: list[str] = []

    def fake(url: str, *, headers=None, timeout=15):
        calls.append(url)
        if url.endswith("sh600000,sz000001"):
            return _Resp('v_sh600000="1~浦发银行~600000~10.00";\nv_sz000001="51~平安银行~000001~11.00";')
        return _Resp('v_bj430017="62~星昊医药~430017~12.00";')

    monkeypatch.setattr(sources, "fetch", fake)
    assert sources.tencent_names(["SH600000", "SZ000001", "BJ430017", "bad"], batch_size=2) == {
        "SH600000": "浦发银行",
        "SZ000001": "平安银行",
        "BJ430017": "星昊医药",
    }
    assert calls == [sources.TENCENT_QUOTE + "sh600000,sz000001", sources.TENCENT_QUOTE + "bj430017"]


def test_tencent_names_retries_transient_error(monkeypatch):
    calls: list[str] = []

    def fake(url: str, *, headers=None, timeout=15):
        calls.append(url)
        if len(calls) == 1:
            raise sources.FetchError("RemoteDisconnected")
        return _Resp('v_sh600000="1~浦发银行~600000~10.00";')

    monkeypatch.setattr(sources, "fetch", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    assert sources.tencent_names(["SH600000"]) == {"SH600000": "浦发银行"}
    assert len(calls) == 2


def test_tencent_names_fails_after_last_transient_error(monkeypatch):
    def fake(url: str, *, headers=None, timeout=15):
        raise sources.FetchError("TimeoutError")

    monkeypatch.setattr(sources, "fetch", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    with pytest.raises(sources.FetchError, match="tencent_names_batch_1:TimeoutError"):
        sources.tencent_names(["SH600000"])


def test_tencent_names_empty_response_is_an_error(monkeypatch):
    monkeypatch.setattr(sources, "fetch", lambda url, **kw: _Resp('v_sh600000="";'))
    with pytest.raises(sources.FetchError, match="tencent_names_empty"):
        sources.tencent_names(["SH600000"])


def test_tencent_names_partial_response_fails_closed(monkeypatch):
    syms = [f"SH60{i:04d}" for i in range(10)]
    monkeypatch.setattr(sources, "fetch", lambda url, **kw: _Resp('v_sh600000="1~浦发银行~600000~10.00";'))
    with pytest.raises(sources.FetchError, match=r"tencent_names_partial:1/10"):
        sources.tencent_names(syms)
