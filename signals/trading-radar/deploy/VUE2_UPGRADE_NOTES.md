# Vue2 upgrade notes (P2)

Production frontend image (pinned):

- `trading-radar-quantdinger-frontend:personal-2026.09.12-etf-pool.2`
- Deliberate ETF UI overlay on the frozen 8/07 source; see `deploy/etf-pool/README.md`.

Facts:

- Source line is **Vue2** (QuantDinger-Vue), not rebuilt from `~/QuantDinger`.
- Do **not** casually `docker compose build` / rebuild frontend to “match” a backend hotfix.
- Frontend pairing with the 8/20 backend overlay was verified in `PRODUCTION_PIN.md` (8/07 frontend remains correct generation).
- Vue3 migration is a deliberate product project, not a host ops cleanup item.

Release gate:

- Use `/home/ubuntu/bin/trading-radar-compose` only.
- Rebuild requires `ALLOW_UNPINNED_BUILD=1` and a subsequent `PRODUCTION_PIN.md` update.
