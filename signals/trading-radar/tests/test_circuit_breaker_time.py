"""APICircuitBreaker：时间推进后半开→恢复（mock time）。"""

from unittest.mock import MagicMock

from radar.data.api_protection import (
    APICircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


def test_circuit_opens_then_recovers_after_timeout(monkeypatch):
    cfg = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=10.0)
    br = APICircuitBreaker("t", config=cfg, logger=MagicMock())
    t0 = 1000.0
    monkeypatch.setattr("radar.data.api_protection.time.time", lambda: t0)

    br.record_failure()
    assert br.state == CircuitState.CLOSED
    br.record_failure()
    assert br.state == CircuitState.OPEN

    # 未到恢复时间
    monkeypatch.setattr("radar.data.api_protection.time.time", lambda: t0 + 5)
    assert br.can_execute() is False

    # 超过 recovery_timeout → HALF_OPEN，允许执行
    monkeypatch.setattr("radar.data.api_protection.time.time", lambda: t0 + 11)
    assert br.can_execute() is True
    assert br.state == CircuitState.HALF_OPEN
