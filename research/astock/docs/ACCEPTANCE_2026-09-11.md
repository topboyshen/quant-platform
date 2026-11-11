# AStock freeze and next slice — 2026-09-10

Compiler, analysis whitelist, titles and weekday timers are frozen until the
natural 2026-09-11 08:00 run is observed. Do not add fields, candidates,
collectors or calendars before that.

## 2026-09-11 08:00 pass

Observe only. Do not start the unit by hand. Do not resend Telegram.

- source `status=success`
- exactly one Telegram event `astock-2026-09-11-success-telegram-v1`
- report title `AStock 数据快报（语义未审）｜2026-09-11`
- `analysisStatus` is `passed` or `skipped_by_analyst`, not `degraded`
- XHS remains `DRAFT_ONLY` / `draft_ready` or same-day `noop`
- `astock-xhs-draft.timer` stays without OnCalendar

Fail closed: if source fails, do not invent a send. If notify is ambiguous, do
not replay. Fix delivery before any new data source.

## After pass: official announcements only

Next development slice is watchlist filings from CNINFO or the exchange, not
more compiler copy.

- keep media and official announcements in separate sections
- never upgrade a media headline to an announcement
- watchlist codes only; skip when the official source is missing
- no buy/sell, targets, positions, or invented cashflow
- Research Ledger stays FROZEN; do not pipe it into the 08:00 report
