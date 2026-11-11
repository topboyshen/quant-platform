# Quant Monday post-close acceptance (2026-08-31)

## Run once

Run as the normal `ubuntu` service owner at or after **15:52 Asia/Shanghai**:

```bash
cd /home/ubuntu/trading-radar
python3 -B scripts/check_quant_monday_acceptance.py --decision-date 2026-08-31
```

The command prints the final result and two report paths. JSON and Markdown reports are written to `~/quant-acceptance-reports/`, outside the Git worktree.

Exit codes:

- `0`: PASS
- `1`: FAIL
- `2`: NOT READY; the 15:52 acceptance window has not opened

Do not rerun a failing EOD, replay outbox rows, send a test message, restart a service or alter the database as part of this gate. Preserve the report and diagnose in a separate change window.

## Read-only contract

The script:

- opens `data/swing_macro.db` with SQLite `mode=ro` and `PRAGMA query_only=ON`;
- reads `data/radar_risk_state.json`;
- reads `/etc/systemd/system/stock-radar.env` only after verifying it is a regular `0600` file owned by the current user;
- calls Telegram `getMe` and `getChat` with HTTPS GET only, three times each, through the local Mihomo proxy;
- invokes the anonymous HTTP/SOCKS Telegram and gstatic egress checker;
- runs read-only `docker inspect` for `radar-macro-risk`;
- never calls `sendMessage`, never includes credentials or API response bodies in reports, and never mutates a service or database.

## PASS criteria

### 15:30 EOD and immutable audit

- The acceptance window is open and the target date has exactly one EOD run.
- The run started no earlier than 15:30, reached `success`, has `data_date` equal to the target date, checked every target and has zero failures.
- `reason_codes` is empty and the outcome is `emitted`, `no_signal` or `blocked`.
- Target decisions equal `target_count`, all have `data_status=ok`, all reference the same non-empty risk snapshot, and that snapshot is complete.
- SQLite `foreign_key_check` is empty.

### Total amount

- The target-date macro slice exists.
- Total amount is finite and between 1,000 and 80,000 亿元.
- `observed_date` equals the target date; `available=true`, `stale=false`, `anomalous=false`.
- Source is one of `tencent`, `sina`, `akshare` or `eastmoney`, and matches the source recorded on the macro slice.

### Telegram and outbox

- All four anonymous HTTP/SOCKS egress probes pass.
- Both read-only Bot API methods succeed in at least two of three attempts. A single isolated retry is recorded but does not fail the gate.
- Outbox has no `pending` or `sending` rows and no failed row created or attempted on the target date.
- The six pre-existing failed rows remain historical evidence and are neither failed by the gate nor replayed.
- Every `sent` row has a send timestamp and at least one attempt.

### Macro risk

- The `radar-macro-risk` container is running and healthy with `restart_count=0` and `oom_killed=false`.
- Published state is at most 900 seconds old, not from the future, and uses an allowed risk level.
- VIX, US 10-year yield and crude oil values are positive and finite; all three sources are present and `missing_fields=[]`.
- A non-empty `cached_fields` list is reported as an in-contract cache warning; it does not by itself fail the gate while the state remains fresh and complete.

## Report interpretation

The top-level result is deterministic:

- `pass`: every check passed; isolated 2/3 Telegram GET recovery may be noted in the summary.
- `fail`: at least one evidence contract failed. The report contains only bounded, non-secret evidence.
- `not_ready`: no production inspection was attempted because the formal window had not opened.
