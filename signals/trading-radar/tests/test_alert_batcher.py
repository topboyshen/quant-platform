"""告警合并与消息构建。"""

import threading
from unittest.mock import MagicMock

import radar.alert.alert_manager as alert_manager_module
from radar.alert.alert_manager import (
    AlertItem as ManagedAlertItem,
)
from radar.alert.alert_manager import (
    AlertLevel,
    AlertManager,
)
from radar.alert.batcher import (
    AlertBatcher,
    AlertItem,
    AlertPriority,
    AlertType,
    build_merged_message,
)


def test_flush_groups_by_code(null_logger):
    b = AlertBatcher(
        priorities={
            AlertType.PRICE: AlertPriority.HIGH,
            AlertType.ML: AlertPriority.MEDIUM,
        },
        batch_enabled=True,
        logger=null_logger,
    )
    b.add(
        AlertItem(
            AlertType.ML,
            "000001",
            "A",
            10.0,
            {"prediction": "x", "probability": 0.8},
        )
    )
    b.add(
        AlertItem(
            AlertType.ML,
            "000002",
            "B",
            20.0,
            {"prediction": "y", "probability": 0.7},
        )
    )
    g = b.flush()
    assert set(g.keys()) == {"000001", "000002"}
    assert len(g["000001"]["alerts"]) == 1


def test_flush_deduplicates_alert_type_and_limits_per_stock(null_logger):
    b = AlertBatcher(
        priorities={
            AlertType.PRICE: AlertPriority.HIGH,
            AlertType.ML: AlertPriority.MEDIUM,
            AlertType.RL: AlertPriority.LOW,
        },
        batch_enabled=True,
        logger=null_logger,
    )
    for alert in (
        AlertItem(AlertType.ML, "000001", "A", 10.0, {"prediction": "old", "probability": 0.6}),
        AlertItem(AlertType.ML, "000001", "A", 10.1, {"prediction": "new", "probability": 0.9}),
        AlertItem(AlertType.RL, "000001", "A", 10.2, {"rl_suggestion": "观望"}),
        AlertItem(AlertType.PRICE, "000001", "A", 10.3, {"direction": "below", "trigger": 9.5}),
    ):
        b._alerts.append(alert)

    grouped = b.flush()
    alerts = grouped["000001"]["alerts"]

    assert [item["type"] for item in alerts] == ["price", "ml", "rl"]
    assert alerts[1]["data"]["prediction"] == "new"
    assert grouped["000001"]["suppressed_count"] == 0


def test_build_merged_message_reports_suppressed_count():
    title, body = build_merged_message(
        "000001",
        "测试",
        10.0,
        [{"type": AlertType.RL.value, "data": {"rl_suggestion": "观望"}, "priority": 3}],
        suppressed_count=2,
    )
    assert "策略信号" in title
    assert "另有 2 条" in body


def test_build_merged_message_contains_price_and_ml():
    alerts = [
        {
            "type": AlertType.PRICE.value,
            "data": {"direction": "below", "trigger": 9.5},
            "priority": 1,
        },
        {
            "type": AlertType.ML.value,
            "data": {"prediction": "可能触发", "probability": 0.81},
            "priority": 2,
        },
    ]
    title, body = build_merged_message("000001", "测试", 10.0, alerts)
    assert "综合告警" in title
    assert "000001" in body
    assert "价格触发" in body
    assert "ML预测" in body


def test_alert_manager_false_delivery_releases_dedup_and_uses_short_backoff(
    monkeypatch,
    null_logger,
):
    now = [1_000_000.0]
    monkeypatch.setattr(alert_manager_module.time, "time", lambda: now[0])
    notifier = MagicMock()
    notifier.send.return_value = False
    manager = AlertManager(notifier, null_logger)
    manager.BATCH_WINDOW = 3600
    outcomes = []

    item = ManagedAlertItem(
        AlertLevel.CRITICAL,
        "数据异常",
        "body",
        alert_type="system",
        on_delivery=outcomes.append,
    )
    assert manager.submit(item) is True
    manager.flush()

    stats = manager.get_stats()
    assert stats["sent"] == 0
    assert stats["failed"] == 1
    assert item._hash not in manager._dedup_history
    assert outcomes == [False]

    retry = ManagedAlertItem(
        AlertLevel.CRITICAL,
        "数据异常",
        "body changed",
        alert_type="system",
        on_delivery=outcomes.append,
    )
    assert retry._hash == item._hash
    assert manager.submit(retry) is False
    assert manager.get_stats()["retry_deferred"] == 1

    now[0] += manager.FAILURE_BACKOFF + 0.1
    notifier.send.return_value = True
    assert manager.submit(retry) is True
    manager.flush()

    assert manager.get_stats()["sent"] == 1
    assert retry._hash in manager._dedup_history
    assert outcomes == [False, True]


def test_alert_manager_false_batch_is_not_counted_as_sent(null_logger):
    notifier = MagicMock()
    notifier.send.return_value = False
    manager = AlertManager(notifier, null_logger)
    manager.BATCH_WINDOW = 3600
    manager._silent_hours = (0, 0)
    outcomes = []

    items = [
        ManagedAlertItem(
            AlertLevel.WARNING,
            f"warning-{index}",
            "body",
            stock_code=f"00000{index}",
            alert_type="ml",
            on_delivery=outcomes.append,
        )
        for index in (1, 2)
    ]
    assert all(manager.submit(item) for item in items)
    manager.flush()

    assert manager.get_stats()["sent"] == 0
    assert manager.get_stats()["failed"] == 2
    assert outcomes == [False, False]
    assert all(item._hash not in manager._dedup_history for item in items)


def test_alert_manager_concurrent_flush_delivers_each_item_once(null_logger):
    notifier = MagicMock()
    notifier.send.return_value = True
    manager = AlertManager(notifier, null_logger)
    manager.BATCH_WINDOW = 3600
    outcomes = []
    item = ManagedAlertItem(
        AlertLevel.CRITICAL,
        "并发刷新",
        "body",
        alert_type="system",
        on_delivery=outcomes.append,
    )
    assert manager.submit(item) is True

    barrier = threading.Barrier(3)

    def flush_at_once():
        barrier.wait()
        manager.flush()

    workers = [threading.Thread(target=flush_at_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    notifier.send.assert_called_once()
    assert outcomes == [True]
    assert manager.get_stats()["sent"] == 1


def test_alert_manager_concurrent_submit_reserves_dedup_atomically(null_logger):
    notifier = MagicMock()
    notifier.send.return_value = True
    manager = AlertManager(notifier, null_logger)
    manager.BATCH_WINDOW = 3600
    barrier = threading.Barrier(9)
    accepted = []
    accepted_lock = threading.Lock()

    def submit_at_once():
        item = ManagedAlertItem(
            AlertLevel.CRITICAL,
            "同一逻辑告警",
            "body",
            alert_type="system",
        )
        barrier.wait()
        result = manager.submit(item)
        with accepted_lock:
            accepted.append(result)

    workers = [threading.Thread(target=submit_at_once) for _ in range(8)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    assert accepted.count(True) == 1
    assert accepted.count(False) == 7
    manager.flush()
    notifier.send.assert_called_once()
