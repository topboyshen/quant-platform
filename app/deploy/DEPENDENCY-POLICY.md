# Dependency audit policy

CI runs `app/deploy/bin/dependency-audit.py` after the test jobs. The job is
blocking, not advisory.

## Categories

| Category | Rule |
|---|---|
| Blocked | Python advisory; npm `high` or `critical` without an active exception |
| Time-boxed exception | Listed in `app/backend/dependency-exceptions.json` with `expires` and `reason`; expired exceptions fail the build |
| Investigate | npm `moderate` / `low`; printed but non-blocking |

## Moderate/low review cadence

`moderate` / `low` npm advisories are not ignored: they are reviewed **every
Monday** together with the weekly dependency audit. For each advisory that
remains:

1. Check whether the affected package is reachable from a production entry
   point; record the path in the release receipt.
2. If reachable, open a fix and merge it within **14 days** (or add a
   time-boxed exception with an expiry and reason).
3. If not reachable, record the acceptance in that week's release receipt and
   review it again next Monday.

`high` / `critical` remain blocking and are never deferred to this cadence.

## Exception record

```json
{
  "exceptions": [
    {
      "id": "CVE-2099-0000",
      "package": "example-package",
      "expires": "2026-12-31",
      "reason": "upstream fix not released; compensating control ..."
    }
  ]
}
```

For npm advisories the package name may be used as `id`/`package`. An exception
is valid only through the end of its expiry date. Do not use an exception as a
permanent allowlist; renew it with a new reason and review evidence.

## Commands

```bash
cd app/backend
python -m pip_audit -r requirements.lock --format json
cd ../frontend
npm audit --omit=dev --json
python ../deploy/bin/dependency-audit.py
```

The script intentionally treats Python advisories as blocking because
`pip-audit`'s JSON does not carry a stable severity field. If a Python advisory
needs an exception, record the advisory id and package explicitly.
