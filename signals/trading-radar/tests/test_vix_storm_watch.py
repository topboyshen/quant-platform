from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vix_storm_watch.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("vix_storm_watch_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the defining module up in sys.modules while processing
    # @dataclass; register it before exec_module (the importlib recipe).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def test_vix_watch_exits_successfully_when_vix_unavailable(monkeypatch, tmp_path, capsys):
    module = _load_script_module()
    state_file = tmp_path / "vix_state.json"
    error = "cboe_timeout+yahoo_snapshot_missing"

    monkeypatch.setattr(module, "STATE_FILE", state_file)
    monkeypatch.setattr(module, "in_us_cash_window", lambda: True)
    monkeypatch.setattr(module, "fetch_quote", lambda: (None, {}, error))

    # A single failed observation only freezes the state; the health alert
    # needs two consecutive failures so one transient miss stays quiet.
    assert module.main() == 0
    state = module.load_state()
    assert state["consecutive_failures"] == 1
    assert state["last_fetch_error"] == error
    assert state["health_alerted"] is False
    assert state["level"] == "green"

    assert module.main() == 0
    state = module.load_state()
    assert state["consecutive_failures"] == 2
    assert state["health_alerted"] is True
    assert "VIX 风险门控数据降级" in capsys.readouterr().out
