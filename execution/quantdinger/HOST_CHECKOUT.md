# This git checkout is NOT production

`/home/ubuntu/QuantDinger` on Quant-Server is a **host research/hotfix checkout only**.
It is **not** the production runtime identity.

| | |
|---|---|
| Path | `/home/ubuntu/QuantDinger` |
| Role | source checkout / cherry-pick staging |
| Production identity | `trading-radar/deploy/PRODUCTION_PIN.md` + pinned OCI image tags in compose |
| Compose entrypoint | `/home/ubuntu/bin/trading-radar-compose` (four-layer gate) |

Rules:

- Do **not** `git pull` / `git reset --hard origin/main` here to “fix drift”.
- Do **not** rebuild backend/frontend from this tree expecting it to match prod.
- `trading-radar-compose` **refuses** `build` / `--build` for `quantdinger-backend` and `quantdinger-frontend` unless `ALLOW_UNPINNED_BUILD=1`.
- Emergency rebuild only: `ALLOW_UNPINNED_BUILD=1 /home/ubuntu/bin/trading-radar-compose ...` then update `PRODUCTION_PIN.md`.
- Production frontend image remains `personal-2026.08.07-order-v2.1` (Vue2). See `deploy/VUE2_UPGRADE_NOTES.md`.

`/home/ubuntu/trading-radar` is the compose/ops repo (`host/ops-20260820`). Same rule: **pin file wins over HEAD**.
