# Dependency audit policy

CI runs `app/deploy/bin/dependency-audit.py` after the test jobs. The job is
blocking, not advisory.

## Categories

| Category | Rule |
|---|---|
| Blocked | Python advisory; npm `high` or `critical` without an active exception |
| Time-boxed exception | Listed in `app/backend/dependency-exceptions.json` with `expires` and `reason`; expired exceptions fail the build |
| Investigate | npm `moderate` / `low`; printed but non-blocking |

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
