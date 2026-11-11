# Notification dispatch v2 (candidate - NOT deployed)

Scope: Telegram notifications sent through `DurableOutboxNotifier` (EOD trend alerts in
`radar_swing_eod.py`, the 15:40/15:50 watchdog `scripts/check_swing_eod_terminal.py`).
Dormant/independent senders (`monitor.py`, `dashboard/alert_system.py`, `radar_trainer.py`,
`radar_pro.py`, monitor engines) keep their own code paths; they only inherit the new
`TelegramNotifier.send()` (one POST, no internal retry, True only when confirmed).

## Outcomes of one POST (`TelegramNotifier.send_once` -> `SendResult`)

| outcome | evidence | next |
|---|---|---|
| confirmed | HTTP 200, `ok=true`, integer `message_id`, `chat.id` equals the configured chat | done |
| not_sent_retryable | TCP connect/DNS/proxy-tunnel never completed (`NewConnectionError`, `ConnectTimeout`, tunnel failed); or 429 with JSON `parameters.retry_after` | wait (>= retry_after), same budget |
| not_sent_permanent | 400/401/403/404 with Telegram JSON `ok=false`; local preflight failure; text > 4096 UTF-16 units | stop, fix config |
| uncertain | read timeout, post-send disconnect, every 5xx, SSLError (no stage proof), malformed/mismatched 200, 429 without JSON retry_after, non-JSON 4xx, crash leftover `sending`, save failure | never retried; human review |

## Queue (schema v6, additive)

`notification_dispatch` (one row per business event) + `notification_attempt` (one row per POST).
Legacy `notification_outbox` is never altered. Key = H(namespace, channel, operation, business id);
the body hash is only a consistency check; target conversation is a keyed digest (HMAC with a
token-derived key; no token / chat id on disk). Retry budget (<= 3 POSTs, 40 s, same Shanghai day) is
stored in the row and charged before each POST. History rows cannot be deleted (trigger).

States: pending, sending, retry_wait, confirmed, failed_permanent, failed_exhausted,
rate_limited_over_budget, uncertain, quarantined_legacy. Only `confirmed` is healthy;
everything else shows up as `review_needed` until a human calls `dispatch_review()` (which never
re-sends).

## Operations

* `RADAR_NOTIFY_V2=off` pauses sending (nothing sent, nothing consumed, no fallback to any old sender).
* Schema other than 6 (e.g. 5 before migration) pauses sending.
* `scripts/migrate_notification_v6.py --db <path> [--apply]` is the only way to reach v6.
* Observability (round 2): the host textfile exporter (`scripts/export_host_textfile_metrics.py`,
  existing timer) now RECOMPUTES `radar_swing_eod_notification_*` on every run with a read-only
  `SELECT` on `notification_dispatch` (`runtime_contracts/notification_health.py`: `mode=ro` URI +
  `PRAGMA query_only`, 2 s timeout, never writes, never creates a journal). A crashed sender therefore
  cannot leave a stale "0 to review". `radar_swing_eod_notification_health_live=1` marks live numbers.
  The sender-written `swing_notification_health.json` is only a fallback for a momentarily unreadable DB
  and only while younger than 15 minutes; older, missing or invalid -> `NaN`/`valid=0` (UNKNOWN, never
  healthy). A readable DB that is not schema v6 is UNKNOWN too. DB path: `RADAR_SWING_DB`
  (read inside `runtime_contracts`, default: `swing_macro.db` next to the watchdog file; the host script itself is unchanged). This is "independently observable", not proof
  that an independent alert reached anyone.

## Review round 2 (what changed vs. the first candidate)

* Legacy history is compared on the stable business identity for EVERY legacy status, `sent` included
  (the key change `eod:{audit_run_id}` -> `eod:{data date}` no longer hides an already sent report):
  same body -> duplicate (nothing sent); different body -> recorded conflict, quarantined, nothing sent;
  failed/sending/pending -> quarantined; an unparsable/unmappable same-day row of that operation ->
  quarantined (`legacy_unmappable`). An identity without a date (macro/datasource alerts) only matches
  legacy rows from the event's own Shanghai day.
* `dispatch_claim` refuses (`blocked_conflict`) any row with a recorded conflict, in the same transaction
  that would hand out the claim (pending, retry_wait, a waiter waking up). A POST already in flight is
  recorded truthfully by `dispatch_finish`; nothing is rewritten or "undone".
* Telegram POST uses `allow_redirects=False`; owned sessions also get `max_redirects=0`. A 3xx (or the
  resulting `TooManyRedirects`) is `uncertain` with stage `redirect`, never retried.
