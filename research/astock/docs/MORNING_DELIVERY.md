# AStock morning delivery — lightweight chain candidate, 2026-09-28

This describes the chain **after** the coordinated on-demand XHS cutover.
Until installation, inspect the effective user units rather than assuming
repository templates are live. Historical events and seals are not rewritten.

## Production schedule

| China time, weekdays | Owner | Capability |
| --- | --- | --- |
| 08:00 | astock-xhs-source.timer | Generate the sealed report, runtime external delivery disabled |
| Trading-day source success | astock-research-seal.service | Verify the same-day archive and create its immutable seal |
| Seal success | astock-morning-notify.service | Send only today's validated, sealed report over existing Hermes |
| Seal success / independent 08:05 and 08:10 timers | nng8-research-digest.service | Send the sealed summary on trading days, or a signed research_skip status receipt on a validated closed day |
| Explicit operator request only | astock-xhs-draft.service | Build or verify a local DRAFT_ONLY artifact, without a delivery trigger |

The duplicate legacy 08:00 Telegram cron is commented out with a superseded
marker, not moved to another schedule. Qlib, snapshot and Radar entries are
outside this change; the paper simulator remains paused.

The daily notification requires the existing persistent owner approval **and**
its explicit runtime Telegram gate. It never invokes generation, model analysis,
public publishing or trading. XHS remains DRAFT_ONLY, publishEligible=false and
humanApprovalRequired=true. Sending the research document to the approved
Telegram route does not imply its research semantics have been reviewed.

## Acceptance before enqueue

The new fixed CLI derives today's date in Asia/Shanghai. It accepts no arbitrary
date, project root, recipient or file path. It requires a successful same-day
orchestrator occurrence, ordered original source timestamps, successful last-run
content gates and the fixed daily report path. Delivery first validates the
immutable research seal against report, last-run, cutoff and context. Only an
ENOENT seal may use the historical immutable draft proof; an invalid or
unreadable seal must not be bypassed by the draft. It does not re-read mutable
afternoon source context as if it were the original morning evidence.

Scheduled delivery refuses a report completed over 120 minutes ago. Non-trading
day skips produce no daily report or Telegram daily notification;
missing/stale/failed/incomplete/unsealed report input fails closed. A validated
non-trading-day marker ends the source-to-seal daily-report publish branch.
The independently scheduled relay may still send its signed research_skip
status receipt; that receipt does not represent a daily report publication.
On a trading day, delivery is chained from seal OnSuccess after the 08:00 source
run. The 09:15 notify calendar timer and 09:10 draft calendar timer stay
disabled and have no OnCalendar, so neither becomes a second send entry.
Restart=no avoids uncontrolled retries. An already-sent event
is a no-op. Failure is visible in unit state/journal, not silently treated as a
delivered report.

## Exactly targeted delivery and acknowledgements

`deliverRunEvent` now restricts drain/recovery to its exact emitted event ID.
It cannot send an unrelated older pending event. The old explicit bulk `drain`
API remains available for separately authorized operator use; it is not in this
schedule. An existing validated sent event is a no-op. Sending, ambiguous, dead,
multiple-state or hash-conflicting events require human review. A send-boundary
check repeats authorization and the sealed proof.

Hermes is invoked as a transport, not an LLM. Structured JSON acknowledgement is
checked in memory: success=true, Telegram platform, valid message ID, no error,
no skip and no warning. Raw replies may contain private chat identifiers and
must never be logged. Text-only partial success, attachment warnings, malformed
output, nonzero exit or timeout are not success. An uncertain attempted send
stays ambiguous and is never automatically replayed. A platform acknowledgement
does not prove the recipient opened the message.

The systemd service permits writes only to work/notifications. Hermes home,
credentials, optional logs and session mirror remain read-only; do not relax
this if a CLI update tries to write them. Upgrade-recovery markers or an active
profile selection block the service until explicitly reviewed. No gateway
restart, backup or credentials migration is part of sending.

## Operator commands

Read-only readiness check for a current-day, manually authorized recovery:

```sh
ASTOCK_FINAL_DELIVERY=telegram node scripts/deliver-morning-report.mjs --manual-same-day --check
```

The actual `--manual-same-day` operation sends the existing document and relaxes
only the automated 120-minute age limit, never the current-day, seal, policy or
idempotency checks. Use it only for explicitly authorized recovery, with the
same read-only-home protection as the production service. Do not rerun the
report workflow to repair notification failure. Do not reset ambiguous/dead
events or change IDs to force a resend.

Diagnostics:

```sh
systemctl --user status astock-morning-notify.service astock-research-seal.service
journalctl --user -u astock-morning-notify.service --since today
```

The old `astock-morning-notify.timer` and `astock-xhs-draft.timer` have no timer
trigger and must not be installed. Their `.timer.txt` copies under
`docs/retired-systemd/` are historical references, not deployable units.
For an existing deployment, retirement is a separately authorized operation:
archive and mask those two timers only, retain the services and source → seal →
notify/digest wiring, and inspect the effective unit graph after daemon-reload.
This source change does not claim that any live units have been retired.

Recovery/rollback: preserve the single source → seal success chain and verify
the effective user-unit graph in a controlled window. Keep the retired timers
masked, the source's delivery disabled, and the duplicate cron disabled. Do not
roll back by restoring the old competing sender or by bulk-draining history.
Retain notification events for duplicate protection; do not create data backups.
