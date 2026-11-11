"""Durable notification wrapper (v2): the ONLY layer that retries.

Flow for one business event::

    enqueue (idempotent, business identity) -> claim (charges the persisted budget)
    -> exactly one ``*_ex`` POST -> classify -> persist -> maybe wait -> claim again ...

Guarantees (see tests/test_notify_dispatch_v2.py):

* ``uncertain`` never leaves automatically and never triggers another POST;
* a crash (before/after the POST) leaves a ``sending`` row whose expired lease becomes
  ``uncertain`` - there is no code path that re-sends it;
* platform success + local save failure only retries *saving the receipt*;
* the retry budget (<=3 POSTs, ~40 s, same Shanghai day) is persisted, so a restart or a
  re-call cannot obtain a fresh budget;
* a rate-limit wait longer than the remaining budget stops with an explicit
  ``rate_limit_over_budget`` (not-sent) state instead of retrying early;
* feature flag ``RADAR_NOTIFY_V2=off`` PAUSES sending (nothing is sent, nothing is
  consumed).  There is deliberately no fallback to the pre-v2 sender.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from radar.notify import identity
from radar.notify.health import write_notification_health
from radar.notify.result import (
    CONFIRMED,
    NOT_SENT_PERMANENT,
    UNCERTAIN,
    DeliveryResult,
    SendResult,
)
from radar.swing.dispatch_store import DispatchClaimLost

FLAG_ENV = "RADAR_NOTIFY_V2"
_OFF_VALUES = {"0", "false", "no", "off", "paused", "disabled"}
_SAVE_ATTEMPTS = 5

_STATE_TO_OUTCOME = {
    "confirmed": CONFIRMED,
    "failed_permanent": NOT_SENT_PERMANENT,
    "failed_exhausted": "failed_exhausted",
    "rate_limited_over_budget": "rate_limit_over_budget",
    "uncertain": UNCERTAIN,
    "quarantined_legacy": "quarantined_legacy",
    "sending": "in_progress",
    "retry_wait": "retry_wait_pending",
    "pending": "in_progress",
}


def notify_v2_enabled() -> bool:
    return os.environ.get(FLAG_ENV, "on").strip().lower() not in _OFF_VALUES


class DurableOutboxNotifier:
    """Persist a notification intent, send it at most once per attempt, classify, retry
    only on proof of non-delivery within a persisted budget."""

    def __init__(
        self,
        store: Any,
        notifier: Any,
        logger: logging.Logger | None = None,
        *,
        scope: str = "",
        sleep: Callable[[float], None] = time.sleep,
        target_alias: str = "telegram-default",
    ):
        self.store = store
        self.notifier = notifier
        self.logger = logger or logging.getLogger(__name__)
        self.scope = str(scope or "")[:128]
        self._sleep = sleep
        self.target_alias = target_alias

    # ------------------------------------------------------------------ helpers
    def _target_digest(self) -> str:
        digest = getattr(self.notifier, "target_digest", None)
        if callable(digest):
            try:
                return str(digest())
            except Exception:  # noqa: BLE001
                return "unavailable"
        return "untracked"

    def _publish_health(self) -> None:
        try:
            write_notification_health(self.store)
        except Exception as exc:  # noqa: BLE001 - observability must never break delivery
            self.logger.warning("通知健康快照写入失败: %s", type(exc).__name__)

    def _attempt(self, operation: str, args: list, kwargs: Dict[str, Any]) -> SendResult:
        """Exactly one POST.  Never raises; an unexpected exception is ``uncertain``."""
        try:
            ex = getattr(type(self.notifier), operation + "_ex", None)
            if ex is not None:
                result = getattr(self.notifier, operation + "_ex")(*args, **kwargs)
            else:  # bool-only notifier (fakes / legacy): no evidence of non-delivery
                result = getattr(self.notifier, operation)(*args, **kwargs)
            if isinstance(result, SendResult):
                return result
            if result is True:
                return SendResult(CONFIRMED, "legacy_bool")
            return SendResult(UNCERTAIN, "legacy_bool", error_class="bool_without_evidence")
        except Exception as exc:  # noqa: BLE001
            return SendResult(UNCERTAIN, "exception", error_class=type(exc).__name__)

    def _save(self, key: str, token: str, attempt_no: int, result: SendResult) -> Optional[Dict[str, Any]]:
        """Persist the classified result.  Only the *save* is retried - never the POST."""
        for index in range(1, _SAVE_ATTEMPTS + 1):
            try:
                return self.store.dispatch_finish(
                    key, claim_token=token, attempt_no=attempt_no, result=result
                )
            except DispatchClaimLost:
                self.logger.error("通知发送结果无法保存：尝试已失去持有权（outcome=%s）", result.outcome)
                return None
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                self.logger.error(
                    "通知结果保存失败 (%s/%s): %s outcome=%s",
                    index, _SAVE_ATTEMPTS, type(exc).__name__, result.outcome,
                )
                if index < _SAVE_ATTEMPTS:
                    self._sleep(0.2 * index)
        return None

    @staticmethod
    def _from_row(row: Dict[str, Any], *, duplicate: bool = False, reason: str = "") -> DeliveryResult:
        state = str(row.get("state"))
        return DeliveryResult(
            outcome=_STATE_TO_OUTCOME.get(state, UNCERTAIN),
            attempts=int(row.get("attempts_used") or 0),
            state=state,
            reason=reason or str(row.get("reason_code") or ""),
            duplicate=duplicate,
            business_key=str(row.get("business_key") or ""),
        )

    # ------------------------------------------------------------------ delivery
    def _deliver_ex(self, operation: str, args: list, kwargs: Dict[str, Any]) -> DeliveryResult:
        if not notify_v2_enabled():
            self.logger.error("通知已暂停 (%s=off)：未发送，也不回退到旧发送器 operation=%s", FLAG_ENV, operation)
            return DeliveryResult("paused", reason="feature_flag_off")
        ready, why = self.store.dispatch_ready()
        if not ready:
            self.logger.error("通知已暂停：dispatch 表未就绪 (%s)，未发送 operation=%s", why, operation)
            return DeliveryResult("paused", reason=why)

        channel = "telegram"
        namespace = self.scope or "default"
        business = identity.business_id(operation, args, kwargs)
        key = identity.business_key(namespace, channel, operation, business)
        payload_obj = identity.legacy_payload(args, kwargs)
        row = self.store.dispatch_enqueue(
            business_key=key,
            namespace=namespace,
            channel=channel,
            operation=operation,
            target_alias=self.target_alias,
            target_digest=self._target_digest(),
            payload_json=identity.canonical_json(payload_obj),
            payload_sha256=identity.payload_sha256(args, kwargs),
            payload_obj=payload_obj,
            business=business,
            legacy_keys=list(
                identity.legacy_key_candidates(
                    self.store.notification_key, operation, self.scope, args, kwargs
                )
            ),
        )
        if row.get("conflict"):
            self.logger.error(
                "同一业务事件出现不同%s，已停止发送并等待人工核查 operation=%s",
                "正文" if row["conflict"] == "payload" else "目标会话", operation,
            )
            self._publish_health()
            return DeliveryResult(
                "blocked_conflict" if row["conflict"] == "payload" else "blocked_target_changed",
                attempts=int(row.get("attempts_used") or 0),
                state=str(row.get("state")),
                reason=f"{row['conflict']}_mismatch",
                business_key=key,
            )
        if row["state"] == "confirmed":
            # Already delivered (earlier call, or a legacy 'sent' record): never POST again.
            return self._from_row(row, duplicate=True)

        final: Optional[DeliveryResult] = None
        while final is None:
            claim = self.store.dispatch_claim(key)
            status = claim["status"]
            if status == "blocked_conflict":
                kind = str(claim["row"].get("last_conflict_kind") or "payload")
                self.logger.error(
                    "同一业务事件已有未解决的冲突（%s），本次不再发送 operation=%s", kind, operation
                )
                final = DeliveryResult(
                    "blocked_target_changed" if kind == "target" else "blocked_conflict",
                    attempts=int(claim["row"].get("attempts_used") or 0),
                    state=str(claim["row"].get("state")),
                    reason=f"{kind}_mismatch",
                    business_key=key,
                )
                break
            if status != "claimed":
                reason = {"in_progress": "another_attempt_in_flight", "not_due": "retry_not_due"}.get(status, "")
                final = self._from_row(claim["row"], reason=reason)
                break
            attempt_no, token = int(claim["attempt_no"]), str(claim["claim_token"])
            result = self._attempt(operation, args, kwargs)
            saved = self._save(key, token, attempt_no, result)
            if saved is None:
                if result.outcome == CONFIRMED:
                    # Delivered but not recorded: the row stays 'sending' -> later 'uncertain'.
                    # Nothing here can cause another POST.
                    final = DeliveryResult(CONFIRMED, attempts=attempt_no, state="sending",
                                           reason="receipt_not_saved", receipt_saved=False,
                                           business_key=key)
                else:
                    final = DeliveryResult(UNCERTAIN, attempts=attempt_no, state="sending",
                                           reason="result_not_saved", receipt_saved=False,
                                           business_key=key)
                break
            if saved["state"] == "retry_wait":
                due = datetime.fromisoformat(str(saved["next_attempt_at"]).replace("Z", "+00:00"))
                wait = max(0.0, (due - self.store._dispatch_now()).total_seconds())
                self.logger.warning("通知未发出，%.1fs 后重试 (%s/%s)", wait, saved["attempts_used"], saved["max_attempts"])
                self._sleep(wait)
                continue
            final = self._from_row(saved)
        self._publish_health()
        return final

    # ----------------------------------------------------------------- public API
    def send_macro_alert_ex(
        self, fact: str, level: str = "info", detail: Dict[str, Any] | None = None
    ) -> DeliveryResult:
        return self._deliver_ex("send_macro_alert", [fact], {"level": level, "detail": detail})

    def send_trend_alert_ex(
        self, name: str, code: str, signal: str, period: str, detail: Dict[str, Any] | None = None
    ) -> DeliveryResult:
        return self._deliver_ex("send_trend_alert", [name, code, signal, period], {"detail": detail})

    def send_system_alert_ex(self, title: str, message: str) -> DeliveryResult:
        return self._deliver_ex("send_system_alert", [title, message], {})

    def send_datasource_alert_ex(
        self, source_name: str, failure_count: int, status: str = "abnormal"
    ) -> DeliveryResult:
        return self._deliver_ex("send_datasource_alert", [source_name, failure_count], {"status": status})

    def send_macro_alert(
        self, fact: str, level: str = "info", detail: Dict[str, Any] | None = None
    ) -> bool:
        return bool(self.send_macro_alert_ex(fact, level, detail))

    def send_trend_alert(
        self, name: str, code: str, signal: str, period: str, detail: Dict[str, Any] | None = None
    ) -> bool:
        return bool(self.send_trend_alert_ex(name, code, signal, period, detail))

    def send_system_alert(self, title: str, message: str) -> bool:
        return bool(self.send_system_alert_ex(title, message))

    def send_datasource_alert(
        self, source_name: str, failure_count: int, status: str = "abnormal"
    ) -> bool:
        return bool(self.send_datasource_alert_ex(source_name, failure_count, status))
