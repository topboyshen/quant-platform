# Read-only post-close acceptance gate

`scripts/check_quant_monday_acceptance.py` produces deterministic JSON and Markdown evidence for a completed post-close run. It is designed for one-shot operational acceptance: inspect once, write the report outside the repository, and diagnose failures in a separate change window.

## Safety contract

The checker:

- opens SQLite with `mode=ro` and enables `PRAGMA query_only=ON`;
- reads the published macro-risk state without modifying it;
- inspects runtime health without restarting or reconfiguring services;
- uses Telegram read methods only and never sends a message;
- does not replay outbox rows, rerun the close process, deploy, or mutate production data;
- excludes credentials and API response bodies from its reports.

Provide environment-specific database, state, credential-file, proxy, checker, container, date, and output paths through the command-line options shown by:

```bash
python3 -B scripts/check_quant_monday_acceptance.py --help
```

Run the gate only after the configured post-close process and its watchdog window have completed. Reports should be written outside the Git worktree and kept private because they may contain operational identifiers, timestamps, source names, measured values, and local paths.

## Result contract

- `0` / `pass`: every required evidence contract passed.
- `1` / `fail`: at least one contract failed; preserve the report and investigate separately.
- `2` / `not_ready`: the acceptance window has not opened; no production inspection was attempted.

The gate checks:

- exactly one successful target-date post-close run with complete target coverage;
- immutable audit linkage and SQLite foreign-key integrity;
- a current, valid market total from an approved source;
- no active or newly failed outbox work;
- fresh and complete macro-risk state plus a healthy runtime;
- anonymous egress and read-only Telegram identity/chat checks.

Cached macro fields may be reported as a warning when the published state is still fresh and complete. A failing gate must not trigger an automatic repair, replay, restart, or deployment.

## Verification

The accompanying tests cover pass, fail, and not-ready outcomes; immutable-audit and outbox contracts; stale macro state; Telegram retry behavior and credential handling; runtime inspection; report redaction; and the absence of mutation commands.
