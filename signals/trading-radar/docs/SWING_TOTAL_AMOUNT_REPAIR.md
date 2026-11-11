# Swing same-day turnover repair

`scripts/repair_swing_total_amount.py` is a narrow, fail-closed repair for a
missing same-day `macro_slice.total_amount`. It is not an EOD replay.

The command may append one immutable factor observation and fill only an empty
`total_amount` field. It never changes a completed EOD run, recomputes strategy
decisions, or sends a notification. Consequently, an EOD run that completed as
`partial` remains `partial` after repair. The turnover-specific acceptance check
may pass while the overall EOD acceptance still fails; that is the intended
audit behavior.

## Eligibility and evidence contract

The command operates only when all of these conditions remain true:

- local Beijing time is on a trading day and within `[15:45, 16:00)`;
- the database schema is exactly the version supported by the running code;
- the latest same-day EOD run is `partial` with `macro_data_partial`;
- exactly one same-day macro slice exists and its `total_amount` is `NULL`;
- Tencent supplies both `sh000001` and `sz399001` qfqday legs;
- each leg has a `+08:00` quote timestamp at or after 15:00, and its quote date
  equals its qfqday date;
- both legs agree on the requested trading date and the combined value is in
  the accepted A-share turnover range.

The store revalidates the schema, qualifying run, empty field, exact source URL,
date proof, value range, quote timestamps, and repair policy under a SQLite
writer lock before inserting anything. A concurrent EOD or repair therefore
becomes an auditable no-op instead of rewriting evidence.

`--dry-run` keeps the database read-only and reports `would_repair`, but it still
performs the live Tencent evidence fetch after eligibility succeeds.

## Scheduler integration

The portable repository intentionally does not install a scheduler or a service
unit. A deployment may invoke the command during its fixed time window:

```bash
python scripts/repair_swing_total_amount.py --config config.swing.json
```

The repair CLI and `radar_swing_eod.py` both acquire the lock named
`radar-swing-eod`. Every scheduler unit must provide the same host-visible
`RADAR_LOCK_DIR`. This matters for service sandboxes such as systemd
`PrivateTmp=true`: their default temporary directories are isolated, so `/tmp`
cannot provide cross-unit exclusion.

The scheduler should treat exit code `75` as a lock conflict and exit code `2`
as a qualified repair failure. A normal no-op, successful repair, or successful
dry run exits `0`.

## Verification

Run the repair, transport, evidence, and lock tests together:

```bash
pytest -q \
  tests/test_swing_total_amount_repair.py \
  tests/test_macro_http_transport.py \
  tests/test_swing_evidence_validity.py \
  tests/test_runtime_lock.py
```

Host-specific paths, proxy settings, credentials, service users, and timer units
belong in a private deployment overlay rather than this portable implementation.
