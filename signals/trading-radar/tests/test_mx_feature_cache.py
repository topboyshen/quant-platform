"""妙想外部特征 TTL 缓存。"""

from radar.data import mx_feature_cache as mc


def test_mx_cached_second_call_uses_cache(monkeypatch):
    mc.mx_cache_clear()
    monkeypatch.setenv("MX_FEATURE_CACHE_TTL_SEC", "60")
    monkeypatch.delenv("MX_FEATURE_CACHE_DISABLED", raising=False)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return calls["n"]

    assert mc.mx_cached("t", ("a",), factory) == 1
    assert mc.mx_cached("t", ("a",), factory) == 1
    assert calls["n"] == 1


def test_mx_cached_disabled_calls_each_time(monkeypatch):
    mc.mx_cache_clear()
    monkeypatch.setenv("MX_FEATURE_CACHE_DISABLED", "1")
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return calls["n"]

    assert mc.mx_cached("t", ("b",), factory) == 1
    assert mc.mx_cached("t", ("b",), factory) == 2
