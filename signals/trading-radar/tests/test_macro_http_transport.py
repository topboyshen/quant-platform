from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

import radar.swing.macro_snapshot as macro_module
from radar.swing.macro_snapshot import MacroSnapshotCollector
from radar.swing.store import SwingStore


def _tencent_response(
    code: str,
    *,
    amount_wan: float,
    bar_date: str | None = "2026-08-25",
    quote_time: object = "20260825150000",
) -> MagicMock:
    quote = [None] * 38
    quote[30] = quote_time
    quote[37] = amount_wan
    bars = [] if bar_date is None else [[bar_date, "0", "0", "0", "0", "0"]]
    response = MagicMock()
    response.json.return_value = {
        "data": {
            code: {
                "qt": {code: quote},
                "qfqday": bars,
            }
        }
    }
    return response


def _sina_response(
    *,
    sh_amount_yuan: float = 970_365_152_113,
    sz_amount_yuan: float = 1_131_349_872_245.798,
    sh_date: str = "2026-08-28",
    sz_date: str = "2026-08-28",
    codes: tuple[str, ...] = ("sh000001", "sz399001"),
    content_type: str = "application/javascript; charset=GB18030",
    body_override: bytes | None = None,
) -> MagicMock:
    values = {
        "sh000001": ("上证指数", sh_amount_yuan, sh_date),
        "sz399001": ("深证成指", sz_amount_yuan, sz_date),
    }
    lines = []
    for code in codes:
        name, amount_yuan, observed_date = values[code]
        fields = ["0"] * 32
        fields[0] = name
        fields[9] = str(amount_yuan)
        fields[30] = observed_date
        fields[31] = "15:00:00"
        lines.append(f'var hq_str_{code}="{",".join(fields)}";')
    body = body_override if body_override is not None else ("\n".join(lines) + "\n").encode("gb18030")

    response = MagicMock()
    response.status_code = 200
    response.is_redirect = False
    response.is_permanent_redirect = False
    response.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    response.iter_content.return_value = [body]
    return response


def test_macro_http_session_uses_explicit_radar_proxy(monkeypatch):
    monkeypatch.setenv("RADAR_ALL_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.invalid:1")
    collector = MacroSnapshotCollector(SwingStore(":memory:"))

    session = collector._http_session()

    assert session.trust_env is False
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert "Referer" not in session.headers


def test_macro_http_session_ignores_ambient_proxy_without_explicit_proxy(monkeypatch):
    monkeypatch.delenv("RADAR_ALL_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.invalid:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:2")
    collector = MacroSnapshotCollector(SwingStore(":memory:"))

    session = collector._http_session()

    assert session.trust_env is False
    assert session.proxies == {}


def test_tencent_total_amount_uses_one_shared_http_session(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [
        _tencent_response("sh000001", amount_wan=120_000_000),
        _tencent_response("sz399001", amount_wan=130_000_000),
    ]
    get_session = MagicMock(return_value=session)
    monkeypatch.setattr(collector, "_http_session", get_session)

    total = collector._tencent_total_amount()

    assert total == 25_000.0
    assert collector._total_amount_observed_date == "2026-08-25"
    assert (
        collector._total_amount_date_evidence
        == collector.TENCENT_TOTAL_AMOUNT_DATE_EVIDENCE
    )
    get_session.assert_called_once_with()
    assert session.get.call_count == 2
    for call, code in zip(
        session.get.call_args_list,
        ("sh000001", "sz399001"),
        strict=True,
    ):
        assert call.args == ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",)
        assert call.kwargs == {
            "params": {"param": f"{code},day,,,3,qfq"},
            "headers": {"User-Agent": "Mozilla/5.0"},
            "timeout": 8,
        }


def _http_error(status: int) -> requests.HTTPError:
    response = MagicMock()
    response.status_code = status
    error = requests.HTTPError(response=response)
    error.response = response
    return error


def test_tencent_total_amount_retries_direct_http_after_https_501(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [_http_error(501), _http_error(501)]
    monkeypatch.setattr(collector, "_http_session", lambda: session)
    payloads = {
        "sh000001": _tencent_response("sh000001", amount_wan=120_000_000).json.return_value["data"]["sh000001"],
        "sz399001": _tencent_response("sz399001", amount_wan=130_000_000).json.return_value["data"]["sz399001"],
    }
    monkeypatch.setattr(collector, "_tencent_fqkline_http_direct", lambda code: payloads[code])

    total = collector._tencent_total_amount()

    assert total == 25_000.0
    assert [call.args[0] for call in session.get.call_args_list] == [
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    ]


def test_tencent_total_amount_does_not_downgrade_non_501_https_errors(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [_http_error(503)]
    monkeypatch.setattr(collector, "_http_session", lambda: session)
    assert collector._tencent_total_amount() is None
    assert session.get.call_count == 1


def test_tencent_prepared_requests_do_not_leak_eastmoney_referer(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = collector._http_session()
    responses = iter(
        [
            _tencent_response("sh000001", amount_wan=120_000_000),
            _tencent_response("sz399001", amount_wan=130_000_000),
        ]
    )
    prepared_headers: list[dict[str, str]] = []

    def send(request, **_kwargs):
        prepared_headers.append(dict(request.headers))
        return next(responses)

    monkeypatch.setattr(session, "send", send)

    assert collector._tencent_total_amount() == 25_000.0
    assert len(prepared_headers) == 2
    assert all(headers.get("User-Agent") == "Mozilla/5.0" for headers in prepared_headers)
    assert all("Referer" not in headers for headers in prepared_headers)


def test_tencent_total_requires_two_matching_canonical_component_dates(monkeypatch):
    cases = (
        (None, "2026-08-25"),
        ("2026-99-99", "2026-08-25"),
        ("20260825", "2026-08-25"),
        ("2026-08-25", "2026-08-26"),
    )
    for sh_date, sz_date in cases:
        collector = MacroSnapshotCollector(SwingStore(":memory:"))
        session = MagicMock()
        session.get.side_effect = [
            _tencent_response(
                "sh000001", amount_wan=120_000_000, bar_date=sh_date
            ),
            _tencent_response(
                "sz399001", amount_wan=130_000_000, bar_date=sz_date
            ),
        ]
        monkeypatch.setattr(collector, "_http_session", lambda: session)

        assert collector._tencent_total_amount() is None
        assert collector._total_amount_observed_date is None
        assert collector._total_amount_date_evidence is None


@pytest.mark.parametrize(
    "quote_time",
    (None, "202608251500", "2026082515000x", "20261301150000", 20260825150000),
    ids=("missing", "short", "nondigit", "invalid-date", "not-string"),
)
def test_tencent_total_rejects_invalid_quote_timestamp(monkeypatch, quote_time):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [
        _tencent_response(
            "sh000001", amount_wan=120_000_000, quote_time=quote_time
        ),
        _tencent_response("sz399001", amount_wan=130_000_000),
    ]
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._tencent_total_amount() is None
    assert collector._total_amount_observed_date is None
    assert collector._total_amount_date_evidence is None


def test_tencent_total_rejects_old_quote_with_new_bar(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [
        _tencent_response(
            "sh000001",
            amount_wan=120_000_000,
            bar_date="2026-08-25",
            quote_time="20260824150000",
        ),
        _tencent_response("sz399001", amount_wan=130_000_000),
    ]
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._tencent_total_amount() is None
    assert collector._total_amount_observed_date is None
    assert collector._total_amount_date_evidence is None


def test_tencent_total_accepts_day_series_when_quote_date_matches(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    sh_response = _tencent_response("sh000001", amount_wan=120_000_000)
    sh_payload = sh_response.json.return_value["data"]["sh000001"]
    sh_payload["day"] = sh_payload.pop("qfqday")
    sz_response = _tencent_response("sz399001", amount_wan=130_000_000)
    sz_payload = sz_response.json.return_value["data"]["sz399001"]
    sz_payload["day"] = sz_payload.pop("qfqday")
    session = MagicMock()
    session.get.side_effect = [sh_response, sz_response]
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._tencent_total_amount() == 25_000.0
    assert collector._total_amount_observed_date == "2026-08-25"


def test_tencent_total_rejects_two_individually_consistent_but_mixed_dates(
    monkeypatch,
):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = MagicMock()
    session.get.side_effect = [
        _tencent_response(
            "sh000001",
            amount_wan=120_000_000,
            bar_date="2026-08-25",
            quote_time="20260825150000",
        ),
        _tencent_response(
            "sz399001",
            amount_wan=130_000_000,
            bar_date="2026-08-26",
            quote_time="20260826150000",
        ),
    ]
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._tencent_total_amount() is None
    assert collector._total_amount_observed_date is None
    assert collector._total_amount_date_evidence is None


def test_sina_total_amount_uses_bounded_https_contract(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = "2026-08-28"
    session = MagicMock()
    session.get.return_value = _sina_response()
    get_session = MagicMock(return_value=session)
    monkeypatch.setattr(collector, "_http_session", get_session)

    total = collector._sina_total_amount()

    assert total == 21_017.15
    assert collector._total_amount_observed_date == "2026-08-28"
    get_session.assert_called_once_with()
    session.get.assert_called_once_with(
        "https://hq.sinajs.cn/list=sh000001,sz399001",
        headers={
            "Accept": "application/javascript,text/plain;q=0.9",
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=8,
        allow_redirects=False,
        stream=True,
    )
    session.get.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "response",
    [
        _sina_response(codes=("sh000001",)),
        _sina_response(sh_date="2026-08-27"),
        _sina_response(sh_date="20260828", sz_date="20260828"),
        _sina_response(sh_amount_yuan=0),
        _sina_response(codes=("sh000001", "sh000001")),
    ],
    ids=(
        "missing-component",
        "mixed-dates",
        "noncanonical-dates",
        "zero-component-amount",
        "duplicate-component",
    ),
)
def test_sina_total_amount_rejects_incomplete_or_ambiguous_components(
    monkeypatch, response
):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = "2026-08-28"
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._sina_total_amount() is None
    assert collector._total_amount_observed_date is None
    response.close.assert_called_once_with()


def test_sina_total_amount_rejects_stale_collection_date(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = "2026-08-29"
    response = _sina_response()
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._sina_total_amount() is None
    assert collector._total_amount_observed_date is None


@pytest.mark.parametrize("failure", ("redirect", "content-type", "oversize"))
def test_sina_total_amount_rejects_unsafe_http_responses(monkeypatch, failure):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = "2026-08-28"
    response = _sina_response()
    if failure == "redirect":
        response.is_redirect = True
    elif failure == "content-type":
        response.headers["Content-Type"] = "text/html; charset=UTF-8"
    else:
        body = b"x" * (collector.SINA_TOTAL_AMOUNT_MAX_BYTES + 1)
        response.headers.pop("Content-Length")
        response.iter_content.return_value = [body]
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(collector, "_http_session", lambda: session)

    assert collector._sina_total_amount() is None
    assert collector._total_amount_observed_date is None
    response.close.assert_called_once_with()


def test_collect_total_amount_prefers_sina_before_heavier_fallbacks(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    collector._collection_date = "2026-08-28"
    monkeypatch.setattr(collector, "_tencent_total_amount", lambda: None)

    def sina_total():
        collector._total_amount_observed_date = "2026-08-28"
        return 21_017.15

    monkeypatch.setattr(collector, "_sina_total_amount", sina_total)
    monkeypatch.setattr(
        collector,
        "_eastmoney_total_amount",
        lambda: (_ for _ in ()).throw(AssertionError("later fallback must not run")),
    )
    sources = []

    assert collector._collect_total_amount(sources) == 21_017.15
    assert sources == ["sina"]
    assert collector.get_collect_status()["total_amount"] == {
        "available": True,
        "source": "sina",
        "message": "",
        "observed_date": "2026-08-28",
        "stale": False,
        "anomalous": False,
    }


def test_eastmoney_prepared_request_has_source_specific_referer(monkeypatch):
    collector = MacroSnapshotCollector(SwingStore(":memory:"))
    session = collector._http_session()
    response = MagicMock()
    response.json.return_value = {"data": {}}
    prepared_headers: list[dict[str, str]] = []

    def send(request, **_kwargs):
        prepared_headers.append(dict(request.headers))
        return response

    monkeypatch.setattr(session, "send", send)
    monkeypatch.setattr(macro_module._time, "sleep", lambda _seconds: None)

    assert collector._get_json_with_retry(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {"pn": 1},
        attempts=1,
    ) == {"data": {}}
    assert len(prepared_headers) == 1
    assert prepared_headers[0].get("Referer") == "https://quote.eastmoney.com/"
