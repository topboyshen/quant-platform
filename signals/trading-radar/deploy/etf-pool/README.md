# ETF research pool feature — 2026-09-12

## Scope and cause

The UI said “system researches 10 ETFs” using a daily-audit count but rendered
only recent triggered signals. On 09-11, all 10 configured ETFs had immutable
`no_trigger` evidence; only two August signals were in history. This was not an
EOD universe limit or a reason to generate additional trading signals.

This release preserves the 10 targets, parameters, risk gate and databases. It
separates the full configured universe from historical triggers, including
search/status filters, per-target evidence dates/reasons, 3-decimal reference
prices and research navigation for an ETF with no signal.

## Evidence contract

`research_pool` is additive to `quant.market-context.v1`. Membership is a bounded,
unique, validated ETF configuration list, never inferred from historical signals.
Rows bind to the exact latest audit `run_id`, with one risk snapshot and consistent
counts/dates/prices/actions. Calendar metadata uses the shared calendar contract.
Missing, failed, malformed, running and stale evidence cannot become `no_trigger`.
All reads share the existing read-only SQLite transaction: at most two additional
bounded queries (risk snapshot + target decisions), not a per-ETF query loop.
No upstream quote calls or business DB writes are added. An `allowed` target can
contain mixed per-signal gating, so the UI says “含放行” rather than claiming all
signals passed. The legacy signal list has no run binding: ETF selection carries
the run-bound status summary, not an inferred specific signal from that list.

Frontend requests are coalesced; an expired response cannot keep the pool green.
Historical selection carries `evidenceState=historical`, stays stale even after a
fresh request, and does not borrow current global risk as historical evidence.
Unknown fields/credentials still cannot cross the normalized research context.
The context remains research-only: no execution/trading action is introduced.

## Provenance and deployment

- Host base: `f7f38b2`.
- Frozen frontend source archive SHA-256:
  `35472889fb5639a24e275239eb85d397e91854fe4bdd957089f7cfe514e4f213`.
- `frontend.patch` contains only the reviewed source/tests delta, relative to that
  frozen source. It is a zero-context Git patch: verify the base archive SHA first,
  then use `git apply --unidiff-zero frontend.patch`. Applying it to that archive
  must reproduce all changed source/test file hashes.
  `~/QuantDinger` and `~/QuantDinger-Vue` were not used as build bases.
- New frontend source archive SHA-256:
  `2493d8138d6c0e9cb322c3c14e431ddbbe7a2d61bc55d7c8a130397ca7533c4b`.
- Source and prebuilt static files are retained under
  `releases/etf-pool-20260912.2/`, alongside `release.json`; these are source/build
  artifacts, not database backups. Existing backup archives remain untouched.
- Builds use minimal contexts, verified existing image bases, `--network=none`,
  `--pull=false`. No dependency install occurs inside either image build. Frontend
  dependencies use the original frozen pnpm lock (pnpm 10.33.4, Node 26.7.0).
- Integration overlays only `integration_app.py`, `etf_pool.py` and the standard
  library calendar package. Frontend overlays compiled static assets; old hashed
  assets stay available for already-open tabs. Nginx/auth config is unchanged.
- New images are named `personal-2026.09.12-etf-pool.2`. Exact local image IDs are in
  `deploy/production_pin.json` (not registry manifest digests).
- Canonical wrapper preflight/postflight is mandatory. Recreate only
  `integration-api` and `quantdinger-frontend` using
  `up -d --no-deps --no-build --pull never ...`. No other service is recreated.

## Validation

- 76 Python integration/pool tests pass in a network-isolated namespace, production
  source bind-mounted read-only, with exact integration Flask dependency versions
  in a separate test venv.
- 202 frontend unit tests pass; the two legacy tests needing a sibling backend's
  generic `docker-compose*.yml` were explicitly excluded because those fixtures
  are not in the frozen frontend archive. This is not an all-project test claim.
- Changed frontend source passes ESLint; the production Vite build succeeds.
- Isolated Chrome tests pass: 10 rows, search, price precision, no-signal selection,
  historical isolation, research navigation, light/dark/mobile layout, duplicate
  request coalescing and failure/recovery. No real account/session or trades.
- Candidate image full authenticated integration route passes with production
  data mounted read-only, fake test credential in tmpfs and networking disabled:
  10 rows / no_trigger=10 / response about 7.5 kB. Unauthorized request returns 401.
- Real upstream quote reliability still needs the next trading-day EOD observation;
  this UI repair neither reruns the strategy nor guarantees a future signal.

## Rollback

Keep the old image tags/IDs. Restore only this release's pin and two Compose files
from `f7f38b2`, then use the canonical wrapper to recreate those same two services
with `--no-deps --no-build --pull never`. If reverting host source, restore only
this release's listed files; do not reset/clean the whole worktree. The additive
new API is compatible with old UI tabs during rollback. No database rollback,
`reset-failed`, backup, retention/prune job or credential change is permitted.

Existing operator configuration, dashboard edits, prune script and untracked
host integrations remain outside this change. In particular, the dashboard was
observed changing independently during preparation; it is not staged or replaced.
