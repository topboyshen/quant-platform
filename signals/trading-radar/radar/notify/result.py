"""Result vocabulary shared by the Telegram sender, the outbox and the dispatch store.

Dependency-free on purpose (no requests/urllib3) so storage and metrics code can import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Outcome vocabulary of one POST (``send_once``).  Exactly one network attempt is made;
# retrying is the responsibility of the durable outbox layer only.
CONFIRMED = "confirmed"
NOT_SENT_RETRYABLE = "not_sent_retryable"
NOT_SENT_PERMANENT = "not_sent_permanent"
UNCERTAIN = "uncertain"
OUTCOMES = (CONFIRMED, NOT_SENT_RETRYABLE, NOT_SENT_PERMANENT, UNCERTAIN)

MAX_TEXT_UTF16_UNITS = 4096


@dataclass(frozen=True)
class SendResult:
    """Structured result of one Telegram POST.  Contains no body, token or chat id."""

    outcome: str
    stage: str = ""
    http_status: Optional[int] = None
    error_class: str = ""
    retry_after: Optional[float] = None
    message_id: Optional[int] = None

    def __bool__(self) -> bool:  # only a verified receipt counts as success
        return self.outcome == CONFIRMED

    @property
    def confirmed(self) -> bool:
        return self.outcome == CONFIRMED


@dataclass(frozen=True)
class DeliveryResult:
    """Result of one durable delivery call (outbox layer).  ``bool()`` is True only when
    the message is confirmed (or a legacy ``sent`` record proves it was delivered)."""

    outcome: str
    attempts: int = 0
    state: str = ""
    reason: str = ""
    duplicate: bool = False
    receipt_saved: bool = True
    business_key: str = ""

    def __bool__(self) -> bool:
        return self.outcome == CONFIRMED

    @property
    def confirmed(self) -> bool:
        return self.outcome == CONFIRMED

    def summary(self) -> dict:
        """JSON-safe view for result artifacts: no payload, token or chat id."""
        return {
            "outcome": self.outcome,
            "attempts": self.attempts,
            "confirmed": self.confirmed,
            "reason": self.reason,
        }
