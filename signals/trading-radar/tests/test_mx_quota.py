"""妙想日配额本地计数。"""


import pytest

import radar.data.mx_quota as mx_quota


@pytest.fixture(autouse=True)
def reset_mx_quota_env(monkeypatch):
    monkeypatch.delenv("MX_QUOTA_DISABLED", raising=False)
    monkeypatch.delenv("MX_DAILY_CALL_LIMIT", raising=False)
    monkeypatch.delenv("MX_QUOTA_SOFT_RESERVE", raising=False)


def test_acquire_respects_daily_limit(tmp_path, monkeypatch):
    st = tmp_path / "q.json"
    monkeypatch.setenv("MX_QUOTA_STATE_FILE", str(st))
    monkeypatch.setenv("MX_DAILY_CALL_LIMIT", "3")
    monkeypatch.setenv("MX_QUOTA_SOFT_RESERVE", "0")

    mx_quota.acquire_mx_call()
    mx_quota.acquire_mx_call()
    mx_quota.acquire_mx_call()
    with pytest.raises(mx_quota.MXQuotaExceeded):
        mx_quota.acquire_mx_call()


def test_new_day_resets_counter(tmp_path, monkeypatch):
    st = tmp_path / "q.json"
    monkeypatch.setenv("MX_QUOTA_STATE_FILE", str(st))
    monkeypatch.setenv("MX_DAILY_CALL_LIMIT", "2")
    monkeypatch.setenv("MX_QUOTA_SOFT_RESERVE", "0")

    monkeypatch.setattr(mx_quota, "_today_key", lambda: "2099-01-01")
    mx_quota.acquire_mx_call()
    mx_quota.acquire_mx_call()
    with pytest.raises(mx_quota.MXQuotaExceeded):
        mx_quota.acquire_mx_call()

    monkeypatch.setattr(mx_quota, "_today_key", lambda: "2099-01-02")
    mx_quota.acquire_mx_call()
    snap = mx_quota.get_mx_quota_snapshot()
    assert snap["used"] == 1


def test_soft_reserve_reduces_effective_limit(tmp_path, monkeypatch):
    st = tmp_path / "q.json"
    monkeypatch.setenv("MX_QUOTA_STATE_FILE", str(st))
    monkeypatch.setenv("MX_DAILY_CALL_LIMIT", "500")
    monkeypatch.setenv("MX_QUOTA_SOFT_RESERVE", "10")

    snap = mx_quota.get_mx_quota_snapshot()
    assert snap["effective_limit"] == 490
    assert snap["hard_limit"] == 500
