> **配置权威：`deploy/production_pin.json`。** `deploy/RUNTIME-PIN.md` 是由该配置生成的阅读副本，不能单独证明容器现役身份。
> 本文保留发布与历史记录；运行状态须对照带时间的容器读回与独立发布基线。
> 文档分工澄清于2026-10-28；原声明、原文及2026-10-27备份均按发布收据保留。

# Current prospective release: etf-named-triggers-20261106.1 pin catch-up

Pin catch-up only (2026-11-06). Containers were already switched to the named-triggers images earlier the same day; this updates `deploy/production_pin.json` (and this document) so the declared pin matches the running frontend and integration. No container restart, compose change, image rebuild, or business-data touch is part of this catch-up.
Frontend: `trading-radar-quantdinger-frontend:etf-named-triggers-20261106.1` / `sha256:ca0ccdf2e5cd8d495c53022c1f1f902bac8dabd76f2af05d7212aa6ce89531fb`.
Integration: `trading-radar-integration:etf-named-triggers-20261106.1` / `sha256:ce8f9029fca346ab19f5c6efb7bfd79471be7af6d05551be22002ae25368a81b`.
Backend remains: `trading-radar-quantdinger-backend:security-hardening-20261029.1` / `sha256:61aa2748ebf7981b3450ce23c4d402a75d999768d328e49fda51144ae276748f`.
The complete prior document follows unchanged.

---

# Previous prospective release: security-hardening-20261029.1

Backend: `trading-radar-quantdinger-backend:security-hardening-20261029.1` / `sha256:61aa2748ebf7981b3450ce23c4d402a75d999768d328e49fda51144ae276748f`.
Rollback backend: `trading-radar-quantdinger-backend:unified-product-closeout-marketcontext-20261022.1` / `sha256:e26dbc711b37e5e5e9ad0f20c8b3faa23eb1c67c91cbd0a2336ca95c113a1998`.
Candidate-image verification: 837 passed, 5 skipped; production-overlay coverage 26.38%; dependency audit and `pip check` passed. Release materials: `/home/ubuntu/releases/quantdinger-security-20261029.1/`.
Scope: tenant-authorization ordering, bootstrap authentication guards, PostgreSQL pool cleanup, webhook/Discord SSRF protection, single-worker guard and vulnerable dependency upgrades. Frontend and integration remain pinned. NO_BACKUP is respected: no business database copy or pre-release dump was created.
The complete prior document follows unchanged.

---

# Previous prospective release: etf-controlled-release-20261027.1

Integration: `trading-radar-integration:etf-controlled-release-20261027.1` / `sha256:f8c73888cb19424e70917fb983001c942bf389866dd71ec4e43aec6a83445de2`.
Rollback integration: `trading-radar-integration:etf-release-reconciliation-marketcontext-20261022.1` / `sha256:e08142a1277b8736e9658720575cc11cd1a057f48e88153798d45ecb06d5a0a0`.
Backend, frontend and mihomo remain the explicitly accepted current instances.
This release reconciles four previously mismatched protected paths prospectively; their prior authorization/install gaps remain UNKNOWN. No historical receipt is replaced.
The complete prior document follows unchanged.

---

# Production pin — Quant-Server

## Current controlled release — 2026-10-18

Revision: `etf-release-reconciliation-20261018.1`. This is the current authority; all prior text is retained below as history, not permission to redeploy it.

Frontend and integration are rebuilt from the actual 2026-10-17 images with COPY-only overlays. Backend stays unchanged. No old static resources or images are removed.

| Current service | Current image reference | Exact local image ID |
|---|---|---|
| radar-quantdinger-backend | `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` | `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4` |
| radar-quantdinger-frontend | `trading-radar-quantdinger-frontend:etf-release-reconciliation-20261018.1` | `sha256:249473dd72e00df189fea247aad846c7bcdbde163287818968978d903d5b72a2` |
| radar-integration-api | `trading-radar-integration:etf-release-reconciliation-20261018.1` | `sha256:b999d5f8411b94b397621357ab016158f07618ae69d0ab8d7577851c1e31e5b3` |

The user explicitly reconfirmed prior approval of the daily-volume-breakout switch OFF and the 40-ETF pool. No strategy, cost, benchmark, admission or research window is changed here. Current config bytes and the original evidence-start timestamp are preserved, not rewritten.
Historical rows without frozen run-rule attribution stay unverified/legacy-strict. Future natural EODs record only run-bound switch/pool attribution, not a full-strategy profitability attestation.
Gate helper, canonical wrapper and independent 17-path validation remain unchanged. Existing monitoring hardening is retained under this reconciliation; this note is not retroactive proof of its original installation authorization.
Release materials: `/home/ubuntu/releases/etf-release-reconciliation-20261018.1/FROZEN.json`, `evidence/FINAL-TEST-RECEIPT.json`, `rollback-pre/` and the final independent readback. Configuration baseline acceptance precedes runtime acceptance and is not itself proof of successful deployment.
UNKNOWN and original evidence remain preserved. No business rerun/recompute/backfill, notification send, schema migration, cleanup or approval-service restart is authorized by this record.

## Retained previous pin document

---

# Production pin — Quant-Server

## Current deployment-record correction — 2026-10-16

The current identities below were read back on 2026-10-16. This correction
restores deployment configuration and records only: **no image replacement,
container recreation, application restart, database migration or research
rerun is part of this correction**. The running three-service product remains
the accepted 2026-10-14 release. Host Git HEAD is not its deployment authority.

| Current service | Current image reference | Exact local image ID |
|---|---|---|
| quantdinger-backend | `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` | `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4` |
| quantdinger-frontend | `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3` | `sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5` |
| integration-api | `trading-radar-integration:unified-product-closeout-20261014.1` | `sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da` |

Restored source bytes are anchored to the 2026-10-14 final postflight and the
independently recovered platform Git archive. The machine-readable Pin, both
application Compose layers and the release-guard source must agree with the
unchanged runtime. The correct installed release guard remains retained.
Any unexpected concurrent file or runtime change must stop publication;
do not recreate containers to make them match a stale Pin.

This section is the current verification note. All text below is the retained
historical record, including its original verification dates and rollback
instructions; those historical instructions are not authorization to execute
an old release or restore business data. UNKNOWN and original evidence remain
unchanged. Notification V2 is a separate release and is not reverted here.

---

Application image IDs last verified: 2026-09-29 (paired research-workflow B1+B2; historical dates retained).

This file records **what is actually running**, not what git HEAD is.

Machine-readable IDs for the three pinned application containers live in
`deploy/production_pin.json`. These are **local image IDs, not registry manifest
digests**. Runtime verification requires exact ID/reference, running=true and
healthy; malformed/incomplete pins fail closed. Verify with:

```bash
python3 scripts/verify_production_pin.py
```

The tracked `deploy/trading-radar-compose` is installed at
`/home/ubuntu/bin/trading-radar-compose`. For `up`, `start` and `restart` it runs:

1. Preflight: resolved canonical Compose image references + local image IDs.
   Running containers are NOT required here, so stopped services can be restored.
2. The requested Compose command (only after preflight passes).
3. Postflight: exact runtime IDs/references and health, bounded wait 60 seconds.
   Failure returns nonzero; there is no automatic rollback/restart.

Read-only `config`, `ps`, `logs` bypass the runtime health requirement. Project/
layer overrides are rejected; the existing guarded-rebuild policy remains.
The `--pin`, `--inspect-json` and `--compose-json` CLI arguments are parsed normally.
Direct `docker compose` remains outside this wrapper's gate; use the canonical
entrypoint for releases. The gate itself never pulls/builds images.

Do **not** rebuild QuantDinger from `~/QuantDinger` to “match” this file.

## Running images

| Service | Image | Source revision |
|---|---|---|
| quantdinger-backend | `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` | Unified research completion; actual runtime verified; image `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4` |
| quantdinger-frontend | `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3` | Unified research completion; actual runtime verified; image `sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5` |
| integration-api | `trading-radar-integration:unified-product-closeout-20261014.1` | Unified research completion; actual runtime verified; image `sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da` |
| macro-risk | `trading-radar-macro-risk:2026.09.02-dedup.1` | Image `48c6f223bb9d`; risk-alert deduplication overlay |
| prometheus | `prom/prometheus:v2.53.0` | restored 2026-08-20 |
| grafana | `grafana/grafana:11.0.0` | restored 2026-08-20 |
| postgres / redis | official alpine tags | — |

## Effectiveness progress wiring restoration (2026-09-20)

The workbench integration overlay had replaced the earlier progress-enabled
`integration_app.py`, leaving the helper present but its call absent. This release
restores only its import, optional request clock, response field and same-clock
call site. The original helper, workbench aggregation, EOD classifier, credentials,
mounts and dependencies remain unchanged; sample policy and business data are not
modified. This does not make the strategy profitable or complete all integrations.

Candidate/base local image IDs:
- Current: `sha256:986089a5d1033efeedbc68c60bb32ee28a589b5f2fe259691c8a2d4314bb512b`.
- Immediate rollback: `trading-radar-integration:workbench-backend-freeze-20260920`,
  `sha256:ab0c13992e19f353d84361ea81004b9dfc8c8524d14854d7f47196d655596b68`.
- Interface file: `b023072747361765e4c6c4ae5daca2ce8f7063b1be4a962887e122bbb4ee254b`.

Only integration-api is recreated, through the canonical wrapper with
`up -d --no-deps --no-build --pull never integration-api`, after synchronizing its
Compose and Pin entries. Backend/frontend remain at the workbench versions above.
For this additive-only fix, restore integration's Compose/Pin/document entries and
its immediate previous workbench image through the same wrapper if rollback is
needed. This is NOT a rollback of the three-service workbench release below;
workbench credentials and mounts must remain in place.

Release source, regression receipts, config-only before/after records and acceptance:
`/home/ubuntu/releases/workbench-profit-hermes-20260920.1/`.
No business database copy, backup, replay, strategy, notification or timer change.

## Workbench phase-1 controlled deploy (2026-09-20)

Isolated end-to-end acceptance ran first on a separate stack (own bridge network, own
empty Postgres, freshly generated test credentials, read-only production mounts,
loopback-only port) before any production change. Evidence:
`releases/workbench-candidate-20260920.2/ACCEPTANCE.md` plus
`releases/workbench-candidate-20260920.2/DEPLOY-CHECKLIST.md`.

Scope is the three pinned services above plus the new workbench credential only:
no optional mounts (the Hermes heartbeat dir, the Profit Radar whitelist dir and
`production_pin.json` are NOT mounted), no permission changes, no database/timer/
proxy/notification/backup/JD changes, and no business task triggered by hand.

Switch order was integration-api -> quantdinger-backend -> quantdinger-frontend, one
service at a time, with that service’s Compose image reference and its
`production_pin.json` entry synced in the same step. This is mandatory: the canonical
wrapper preflights all three pinned services and postflights for exact ID/reference
plus running + healthy on all three, so setting all three pin entries to the new
images first would fail the first switch postflight. Switches used
`--no-deps --no-build`, never pulled, and never rebuilt (`ALLOW_UNPINNED_BUILD` not
used): `/home/ubuntu/bin/trading-radar-compose up -d --no-deps --no-build <service>`.

Rollback is NOT per-service independent: the layers are HTTP-coupled (frontend ->
backend `/api/radar/workbench/*`; backend -> integration `/integration/v1/workbench/*`).
Roll back in reverse order - frontend -> quantdinger-backend -> integration-api -
restoring each service image, its Compose image reference and its pin entry together,
re-running the gate after each step. Pre-change definitions are preserved in
`releases/workbench-candidate-20260920.2/rollback/` as `*.pre-workbench`; all three
pre-workbench images are still present locally.

New credential/variables with an explicit retain/revoke rule:
`secrets/radar_workbench_token` (root:root 0600) mounted into integration-api and
quantdinger-backend at `/run/secrets/radar_workbench_token`, plus backend
`RADAR_WORKBENCH_URL` / `RADAR_WORKBENCH_TOKEN_FILE` /
`RADAR_WORKBENCH_TIMEOUT_SECONDS` and integration `RADAR_WORKBENCH_TOKEN_FILE`.
Retain while ANY of the three services still runs the workbench image; revoke only
after integration-api has also been restored, as an explicit decision to delete or to
explicitly keep the unreferenced credential file. `radar_integration_token` is
unchanged and never gains workbench privileges.

Accepted limitations - not defects, and NOT to be "fixed green" by widening access:
`execution_digest` is `unavailable` because `data/approved-signals` stays `0700`
while the integration container runs `cap_drop: ALL`; `hermes_status`,
`profit_radar` and `release_version` are `not_integrated` because those optional
mounts are out of first-release scope.


## Report centre batch-1 controlled deploy (2026-09-21)

Frontend-only controlled switch. Candidate provenance: candidate tree
`releases/report-center-batch1-20260921.1` (baseline `navigation-home-20260921.1`,
exactly two file changes: new `src/utils/reportCatalog.js`, rewritten
`src/views/personal-reports/index.vue`; layout / home / workbench files byte-identical).

- Build: hardened offline recipe (`--network none --read-only --cap-drop ALL --user 1000:1000`),
  node image `docker.1ms.run/library/node:20-alpine` (local, not pulled), deps mounted read-only.
  Unit suite now **353 tests / 351 pass / 2 known baseline failures** (new `reportCenterCatalog`
  tests are part of the project suite); ESLint clean; `dist/index.html`
  `f34e9392fa897bfd48b78b9787f1798547ce75f79e2745aecf0e96ae24ffa826`.
- Runtime acceptance on the real artifact: **25/25** (isolated mock backend).
- Cross-version upgrade acceptance: **15/15** — an already-open old page navigating (without
  refresh) into not-yet-loaded routes gets no resource 404 / no blank screen, because the
  previous generation's static assets are RETAINED.
- Packaging: `Dockerfile.report-center.release` FROM the running frontend image
  `sha256:3316b2f7c2f1…`, then `COPY dist/` **without** removing `/usr/share/nginx/html` —
  deliberately retains the previous generation's static files (148 old assets + stamps) so
  already-open pages keep working until refresh. Identity: new 59 static files match the
  verified dist file-by-file (canonical tree hash `d725046f4e0bc2cc…`, `LC_ALL=C`);
  `/etc/nginx/**` file digests identical; Entrypoint/Cmd/User/ExposedPorts/StopSignal unchanged.
- Rollback: restore frontend image + pin entry to `…navigation-home.1` (`sha256:3316b2f7c2f1…`),
  run the canonical wrapper `up -d --no-deps --no-build quantdinger-frontend`, verify the gate.
  Pre-change copies are kept in `releases/report-center-batch1-20260921.1/rollback/`.
- Notes: compatibility with old sessions does NOT mean an old page becomes the new UI
  immediately — old pages keep working and a refresh loads the new interface.
  Old static assets and version stamps are retained; no cleanup in this release.
  Full-text reading (batch 2) and the execution-digest access are separate items.
## ETF diagnostic evidence release (2026-09-16)

Tracked provenance: `deploy/etf-diagnostics-20260916/release.json` and frontend
overlay. Host producer records best-effort run-bound reasons only for future
natural EOD runs. No strategy/configuration/notification/timer changes, manual
runs, historical recomputation, schema changes, or database copies/backups.
Only integration-api and quantdinger-frontend containers are recreated.
Linux isolated regression: 270/270; frontend 218 pass (two external-fixture
tests excluded); 55 baseline-equivalence cases; synthetic browser checks pass.

Rollback is per component: restore only that component's Compose image and
matching pin entry from release.json, preserving the other services' entries;
use the canonical wrapper with `--no-deps --no-build --pull never` for that
service only. Restore both host producer files from base commit b7e5c7b only
while the Swing service is inactive. Do not restore or copy any database.
Old images remain present, but no new cleanup/retention policies were changed.

## Backend compatibility overlay (2026-09-14)

Tracked provenance: `deploy/backend-compat-20260914/release.json`, exact-source
patch and original Dockerfile. The production source is this frozen-image
overlay, NOT the divergent `~/QuantDinger` checkout. The historical commands
under ignored `releases/` are audit evidence, not reusable release templates.
Future releases must follow `deploy/QUANT_BACKUP_POLICY.md`: NO_BACKUP covers
manual and pre-release database copies too. The historical dump below is an
existing retained artifact, not a prerequisite or authorization for future work.

Backend only: preserve equity-curve risk/end points, close/check Tencent HTTP
responses, and keep A-share fallbacks reachable after exhausted Tencent retries.
Upstream references: cb78a62 and 25eaf36; no v5 engine or schema migration.

- Exact image ID: `sha256:509acb40e3087c9c0c1a56ac91e12bcdbb7d2b3941bbe1e8802981fb54f9d9ac`.
- Base/rollback image ID: `sha256:0fb958dd9d7ab220ddf961600c838140f985e11f0de35976ac1dabb255c22e9c`.
- Source and evidence: `releases/backend-compat-20260914.1/`.
- Pre-update PostgreSQL dump: `/home/ubuntu/backups/quantdinger-postgres/quantdinger-pre-compat-20260914T024458Z.dump`; SHA-256 sidecar verified,
  full isolated restore passed (schema and 8 key-table counts).
- 35 isolated compatibility tests + native numpy/pandas benchmark smoke passed;
  dependency hash unchanged: `9862b525147f948952d12699cd4b2d1196fd72bf605b0841651827f68efab7ee`.
- Frontend, ETF integration, Hermes, profit-radar v3, DB containers and schedules
  remain unchanged. No strategy edits, migrations, live orders or extra messages.
- Scoped rollback: restore ONLY the three definition files under
  `releases/backend-compat-20260914.1/rollback/`, then use the canonical wrapper
  `up -d --no-deps --no-build --pull never quantdinger-backend`.
- Final live result is recorded separately in `evidence/deployed.json`.

## ETF research-pool feature release (2026-09-12)

The new UI displays all configured ETFs independently of whether they emitted
signals. Exact run-bound evidence, calendar-aware dates, historical isolation,
search, status filters and no-signal research navigation are covered by regression
and isolated browser tests. Strategy/configuration, databases and the QuantDinger
backend image are unchanged. See `deploy/etf-pool/README.md` and `release.json` for
source/archive hashes, validation exclusions and scoped rollback.

This deliberate UI feature release supersedes the older frontend identity below;
old release sections remain historical records, not the current pin.

## Git checkouts on this host (NOT production)

| Repo | HEAD | vs origin | Note |
|---|---|---|---|
| `~/QuantDinger` `host/quant-server-20260820` | `3551b8e` fix(data): add Sina daily fallback | local host branch | Contains only the committed hotfix sources; it still does **not** contain frozen production base `96eb90b`. |
| `~/trading-radar` `host/ops-20260820` | this file's commit | local host branch | Existing deleted backup/model files and unrelated local changes remain intentionally untouched. |

## Backend hotfix overlay (2026-08-20)

The backend was rebuilt as a three-layer overlay on the exact previous image ID
`sha256:0f8b96f6da6b69ed4eb56f7a52a13dc9cbf38c3dea07d7f9a0768d01a42ecf32`.
It was **not** rebuilt from the divergent `~/QuantDinger` checkout.

| Item | Value |
|---|---|
| New tag | `trading-radar-quantdinger-backend:personal-2026.08.20-sina-chatfix.1` |
| New image ID | `sha256:0fb958dd9d7ab220ddf961600c838140f985e11f0de35976ac1dabb255c22e9c` |
| Sina source commit | `3551b8e7c7fcc56a19b26794a63a00b693f9b254` |
| Chat source commit | `c87bf99c7f0ff3f4ccfbce5d2e562f4ed6277b4f` |
| Source hashes | `cn_stock.py c6c02588…`; `ai_chat.py 18cb1b8b…` |
| Dependency proof | old/new `pip freeze` SHA-256 both `9862b525147f948952d12699cd4b2d1196fd72bf605b0841651827f68efab7ee` |
| Acceptance | 7 targeted tests passed; real Sina fallback returned 5 canonical `time` rows; API/frontend healthy; 9/9 containers healthy, restart 0, no OOM |

Rollback (keeps databases, volumes, config, and secrets in place):

```bash
cd /home/ubuntu/trading-radar
sed -i 's|personal-2026.08.20-sina-chatfix.1|personal-2026.08.14-credential-clean.1|' docker-compose.quantdinger.yml
/home/ubuntu/bin/trading-radar-compose config --quiet
/home/ubuntu/bin/trading-radar-compose up -d --no-deps quantdinger-backend
```

## Frontend pairing (checked 2026-08-20)

Do **not** rebuild frontend to “match 8/14 backend”.

Evidence:

1. `releases/order-v2.1-20260807/MANIFEST.md` already pairs backend commit `96eb90b` with frontend image `f10639189da8` / source SHA `35472889fb5639…`.
2. `releases/credential-clean-96eb90b-20260814.incoming` is a **backend-only** overlay on that same `96eb90b` line. Acceptance set `forbidden_scope.frontend_rebuilt = false`.
3. Running frontend **is** that 8/07 image. No newer matching frontend artifact exists on this host.
4. Frontend source lives in QuantDinger-Vue, not in `~/QuantDinger`.

Conclusion: 8/07 frontend remains the correct generation for the 8/20 backend overlay. The hotfix changes no frontend API contract.

## Monitoring (restored 2026-08-20)

Not intentionally retired. Compose still defined them; they were stopped. Restored after fixing data-dir ownership and Grafana secret mode.

| Service | Bind | Status |
|---|---|---|
| prometheus | `127.0.0.1:9091` | healthy |
| grafana | `127.0.0.1:3000` | healthy |

## integration-api fix (2026-08-20)

Root cause: `data/swing_macro.db` mode `0600` + `cap_drop: ALL` → container could not read SQLite.
Fix: `chmod 644` + `SwingStore._init_schema` now `os.chmod(..., 0o644)` after migrate.
`/ready` returns 200; QuantDinger can reach `http://integration-api:8092/ready`.

## Integration smoke overlay (2026-08-23)

The previous runtime was healthy but its documented in-container smoke command
failed because `Dockerfile.integration` never copied `scripts/smoke_integration.py`.
The replacement image is an immutable overlay on exact base image ID
`sha256:df09103f04b6226f3347a2bb8d6dd2323b99ab11c73b8e3e9b50847e555b7475`.
It copies only `/app/scripts/smoke_integration.py` and compiles that file; the
dependency snapshot must remain SHA-256 `6e3eb9bcdf06526f57da604c56a892152f524b6e238482e7e138812836a54cee`.

| Item | Value |
|---|---|
| New image ID | `sha256:69a75d6ac1480463088aac9432542c362dc5246892e45230514b581eb956b48e` |
| Source commit | `270c0e1` (overlay Dockerfile + smoke guardrail test) |
| Smoke source hash | `6feeb72efea53c622845d58423a0251fb4c8716d0c652aaa62375ab3f758174c` (host=image) |
| Acceptance | `/health` 200; `/ready` 200; authenticated strict smoke passed with market 2026-08-21, status ok, one real signal; dependency hash unchanged |

Rollback keeps the database, read-only data mount, and secrets unchanged:

```bash
cd /home/ubuntu/trading-radar
sed -i 's|trading-radar-integration:2026.08.23-smoke.1|trading-radar-integration:2026.07.28.3|' docker-compose.integration.override.yml
/home/ubuntu/bin/trading-radar-compose config --quiet
/home/ubuntu/bin/trading-radar-compose up -d --no-deps integration-api
```

## Four-index close cache (2026-08-23)

`research-index-close-cache.timer` runs at 15:35 on weekdays and atomically
publishes only a complete exact-date set for `sh000001`, `sh000300`,
`sz399001`, and `sz399006`. It uses the pinned QuantDinger backend's existing
Tencent -> Sina fallback and the shared A-share holiday calendar. Any partial,
mixed-date, non-positive, future, or upstream-failed batch leaves the previous
cache byte-for-byte intact. `research-digest.timer` remains a local renderer
and performs no upstream calls.

Installed unit files and repository copies have identical SHA-256 hashes. The
timer is enabled/active; next scheduled run is 2026-08-24 15:35 CST. A
post-test Sunday recovery run legitimately resolved the last open session as
2026-08-21 and wrote all four closes from Tencent. The refreshed digest marks
the cache `fresh`, with explicit `expected_as_of`, `source`, and `fetched_at`.

## Four-index close cache stale-only repair (deployed 2026-08-26)

Commit `b78eaa6` keeps a single `research-index-close-cache.service` and
schedules that same service at 15:35, 15:45, and 15:55 on weekdays. Every
activation uses `--repair-if-stale`: a strictly valid, exact expected-date
cache is an explicit no-op with no upstream probe or file write; an invalid or
stale cache is fetched, validated, and atomically replaced only after a
complete four-index result. A canonical future-dated cache is refused before
the probe or write.

The installed timer uses `Persistent=false`. A failed source attempt returns
nonzero and leaves the last-known-good cache byte-for-byte unchanged; a later
activation of the same oneshot service can retry without a parallel writer.
The installed SHA-256 values are
`4421c2bec1e58cf261d22ff0bd5ce4d40d9e3c88076baefd7f27a649fbc06022`
for the service and
`13613c13596184adbf62518eb94f657c7c7502475a788dc680839d3b2b7f8190`
for the timer.

### 08:07 migration incident

The initial migration did not meet the no-backfill condition. The journal shows
that the service started at 08:07:25 CST during the `systemctl daemon-reload`
stage, before the timer stop/start messages at 08:07:26. This ordering is
consistent with the still-active old timer carrying its previous
Persistent/catch-up state while re-evaluating the newly added, already-missed
2026-08-25 calendar entries; it does not independently prove systemd's internal
decision path. Invocation `c578d835b09d4c75a3f2b1aef3f5879b` completed at
08:07:34 and repaired the cache to trade date 2026-08-25 from Tencent. The
cache changed from SHA-256 `761392c9cfdc...` to `32b09e78d52e...`.

This was an unintended migration-time repair, not a scheduled 2026-08-26
close. The validated four-index cache was retained and is classified as a late
migration repair; it was not written back into the Swing run or any production
SQLite database. By the time the stop action was taken, the service had already
completed successfully and written the cache; the timer stop was logged at
08:07:49. Containment prevented further invocations but did not prevent this
one. The successful invocation naturally replaced the prior failed service
state and InvocationID `ae76648986ba4962811aecd6226a29a0`; no
`reset-failed` was used.

After confirming the loaded unit reported `Persistent=no`, the timer was
started from the inactive state. It is now enabled, active/waiting, and next
scheduled for 15:35 CST; there was no second service invocation. The five
production SQLite databases, Swing EOD/watchdog invocations, local/COS backup
objects, and all nine healthy containers remained unchanged.

Stopping and starting the timer cleared its runtime `LastTrigger` field, so an
empty value is not evidence that the migration-time run did not happen. The
authoritative audit evidence is the service journal, InvocationID and execution
timestamps, plus the cache SHA-256 and mtime.

### Required migration sequence

When calendar entries are added to an already-active timer, `Persistent=false`
alone does not make an in-place reload safe. Use this order:

1. Stop the old timer before replacing either unit.
2. Install and statically verify the new service and timer while it is stopped.
3. Run `systemctl daemon-reload` while the timer remains inactive.
4. Confirm the loaded timer has `Persistent=no` and only the intended calendar
   entries.
5. Start the timer, then verify `active/waiting`, the next future trigger,
   and—relative to the pre-start baseline—no new service InvocationID or
   journal entry and no cache SHA-256 or mtime change.

Do not use `restart` across a calendar-definition change, do not manually start
the service, and do not use `reset-failed` as part of this migration.

## Grafana secret mode (2026-08-23)

The source secret is `0640 root:root`. `0640 root:ubuntu` is not compatible
with the current container identity (`uid=472`, supplementary `gid=0`), while
`root:root 0640` remains readable inside the read-only bind mount. Default
`admin/admin` returns 401; configured-secret authentication and `/api/health`
return 200. The secret value was never logged or passed as a command argument.

## Compose command

```bash
cd /home/ubuntu/trading-radar
/home/ubuntu/bin/trading-radar-compose config --quiet
/home/ubuntu/bin/trading-radar-compose up -d --no-deps quantdinger-backend
```

## Navigation + homepage (2026-09-21)

Owner confirmed this frontend-only release. Five navigation groups and homepage
information hierarchy; existing routes, permissions, ETF diagnostics/progress,
refresh and research context retained. No backend/API/DB/timer/secret/permission/
proxy/notification/backup changes. No manual business execution.

Current frontend: `trading-radar-quantdinger-frontend:personal-2026.09.21-navigation-home.1`, `sha256:3316b2f7c2f1fafba24b96145ed9cf9e07eacc644f09e205d5c1ed286002dfcc`.
Immediate frontend-only rollback: `trading-radar-quantdinger-frontend:workbench-candidate-20260920.3`, `sha256:7a3186985f12f0e0f2e44e4db93fcc2bbf1431f23964108123a801d7d86a717b`.
Evidence and exact metadata originals: `/home/ubuntu/releases/navigation-home-20260921.1/`.
Use `release-switch.py rollback` only while its recorded metadata still matches;
it refuses concurrent drift. Restore only frontend Compose/Pin/docs, then canonical
frontend-only up with --no-deps --no-build --pull never. Keep all other services,
mounts and secrets unchanged. Three metadata writes are individually atomic, not a
cross-file transaction; the pending record remains until deployment/rollback passes.


## Report center boundary fixes (2026-09-21)

Frontend-only release: state tone, visible selection fallback, fail-closed report
identity, mixed-format family selection, and exact cutoff provenance.
Current frontend: `trading-radar-quantdinger-frontend:report-center-fixes-20260921.1`, `sha256:743eadd1fff970325ca97b01612ffe0929e53f126e3ab5a61d06b69716c7ee1d`.
Immediate frontend rollback: `trading-radar-quantdinger-frontend:report-center-batch1-20260921.1`, `sha256:fb3a3746ca73e05ace1e6b827655abc7e91923506e55536ea0c9066cc0e6611d`.
Evidence and three configuration originals: `/home/ubuntu/releases/report-center-fixes-20260921.1`.
Use this release's `release-switch.py rollback --candidate-id sha256:743eadd1fff970325ca97b01612ffe0929e53f126e3ab5a61d06b69716c7ee1d`;
rollback refuses metadata/runtime drift. Only the canonical wrapper may apply
`up -d --no-deps --no-build --pull never quantdinger-frontend`.

    
## Workbench evidence access repair (2026-09-21)

Integration-only release: `trading-radar-integration:workbench-evidence-20260921.1`, `sha256:0f695dfe8f599882eb6b99612edcde9c9debe3b3ece6fb1ff9ec536e48e5757f`.
Immediate rollback: `trading-radar-integration:workbench-profit-hermes-20260920.2`, `sha256:af45bf4919344cdb879b8389e14263594b899f3dc875a45abf8e985115640be5`.
Two directory-level RO whitelist mounts: digest-view to /app/data/approved-signals
and release to /app/release. The approved-signals container path is a whitelist
view, NOT the host intake directory. Host 0700 is unchanged; no intake DB access.
Existing staging timer calls scripts/publish_workbench_evidence.py. Operational
samples expire after 90 seconds; business generated_at/as_of_day stay unchanged.
No Docker socket, added capability, secrets, notification or business task runs.
Evidence and rollback metadata: `/home/ubuntu/releases/workbench-evidence-20260921.1`.
Rollback: restore host sync script first (publisher becomes unreferenced), then
release-switch.py rollback with this candidate ID; only integration is recreated.
No business database copies. Frontend/backend/Hermes unchanged.


## Market selection layout (2026-09-21)

Frontend-only release: responsive ETF pool and personal A-share watchlist,
compact asset identity, expandable full diagnostics, and mobile/dark layout.
Existing navigation, report center, APIs and business rules are unchanged.
Current frontend: `trading-radar-quantdinger-frontend:market-layout-20260921.1`, `sha256:d62bc0000fbe10b673ff549eea7ecae6b2401e8e66441a02c0f5f1e419249d3b`.
Immediate frontend rollback: `trading-radar-quantdinger-frontend:report-center-fixes-20260921.1`, `sha256:743eadd1fff970325ca97b01612ffe0929e53f126e3ab5a61d06b69716c7ee1d`.
Evidence and three configuration originals: `/home/ubuntu/releases/market-layout-20260921.1`.
Use this release's `release-switch.py rollback --candidate-id sha256:d62bc0000fbe10b673ff549eea7ecae6b2401e8e66441a02c0f5f1e419249d3b`;
rollback refuses metadata/runtime drift. Only the canonical wrapper may apply
`up -d --no-deps --no-build --pull never quantdinger-frontend`.


## Research lab batch 1 (2026-09-22)

Frontend-only release: parameter classification and safe default-value writeback;
historical-run identity isolation and loading/failure/stale-response handling.
No backend/integration, database, permission, proxy, notification or timer changes;
no manual business execution. Current source and dist are the frozen reviewed
artifacts, not the host checkout. Existing unrelated working-tree edits remain.
Current frontend: `trading-radar-quantdinger-frontend:research-lab-batch1-20260922.1`, `sha256:92babaaea44b67827939a36becc3c0a8c8b27577762d62772f0ec6655bb48c28`.
Immediate frontend rollback: `trading-radar-quantdinger-frontend:market-layout-20260921.1`, `sha256:d62bc0000fbe10b673ff549eea7ecae6b2401e8e66441a02c0f5f1e419249d3b`.
Evidence and three configuration originals: `/home/ubuntu/releases/research-lab-batch1-20260922.1/release-ready-20260922.1/switch-20260922`.
Use this release's `release-switch.py rollback --candidate-id sha256:92babaaea44b67827939a36becc3c0a8c8b27577762d62772f0ec6655bb48c28`;
rollback refuses metadata/runtime drift. Only the canonical wrapper may apply
`up -d --no-deps --no-build --pull never quantdinger-frontend`.
Forward asset compatibility passed in isolated real-browser acceptance; older
chunks remain in the image. Rollback is NOT seamless for already-open new pages:
protect unsaved work and refresh to load the old entry if a new-only chunk fails.
No automatic draft migration or production rollback drill was performed.


## Research lab linkage frontend (2026-09-24)

Frontend-only static release: four-page source-aware research navigation.
No backend/integration, database, strategy rules, timers, proxy, credentials,
permissions, or business tasks changed. Current frontend: `trading-radar-quantdinger-frontend:research-lab-linkage-20260924.3`,
`sha256:4205ab3f2362f33c7521f23a3ff20f0f8f4ef78329c71582fc60c29ea5f7ab7b`. Immediate reviewed rollback: `trading-radar-quantdinger-frontend:research-lab-batch1-20260922.1`, `sha256:92babaaea44b67827939a36becc3c0a8c8b27577762d62772f0ec6655bb48c28`.
Release evidence and exact before/after configuration snapshots: `/home/ubuntu/releases/research-lab-linkage-20260924.3/deploy-20260924`.
Use only the canonical Compose wrapper for frontend replacement; do not run the
2026-09-22 script with its obsolete hard-coded image IDs. Rollback requires
an explicit `--drafts-protected` attestation and refuses configuration/runtime
drift. The forward image retains old chunks; the old image lacks new chunks,
so save unsaved drafts before rollback and refresh any new page afterward.


## Research Lab V2 quantdinger-backend step (2026-09-28)

Release `research-lab-v2-all-20260928.1`; current `trading-radar-quantdinger-backend:research-lab-v2-all-20260928.1`, `sha256:4c505dc16c57c68c2882dbc1a4309b8c26aaf728b0bbe26ddc1582490c99967c`. Only the selected service Compose image/Pin/current row changed. Backend admission uses environment ceiling run and owner whitelist 1; DB admission is separately audited and must remain closed until post-cutover gates. No business task is triggered by deployment. Rollback retains V0004/new research data; no database backup/restore/drop. Protect unsaved drafts before a UI rollback.


## Research Lab V2 quantdinger-frontend step (2026-09-28)

Release `research-lab-v2-all-20260928.1`; current `trading-radar-quantdinger-frontend:research-lab-v2-all-20260928.1`, `sha256:415636381c385eb3fe0ccd6f4d64af477a44077768be8fbf18cb15ad655af5b2`. Only the selected service Compose image/Pin/current row changed. Backend admission uses environment ceiling run and owner whitelist 1; DB admission is separately audited and must remain closed until post-cutover gates. No business task is triggered by deployment. Rollback retains V0004/new research data; no database backup/restore/drop. Protect unsaved drafts before a UI rollback.


## Research workflow B1+B2 backend step (2026-09-29)

Release research-workflow-batch2-20260929.1, exact image `sha256:bf726216dd96fc91c775a29a8b965599312e7b1352273b312e9107d89c5d908b`. Backend then frontend; admission writes/runs held closed through mixed versions. Explicit typed mapping with immutable indicator/code/contract provenance; no strategy-rule, migration, business-history, timer, integration or credential change. Owner approved backend ENABLE_OFFLINE_AI_CALIBRATION=false to prevent startup calibration. Retain previous frontend assets. Rollback frontend then backend under closed admission; keep calibration false even on old backend, protect drafts and refresh cached clients. Do not reopen old backend runs on new v1 records without reviewed compatibility. Evidence: /home/ubuntu/releases/research-workflow-batch2-20260929.1/execution-revision-1.


## Research workflow B1+B2 frontend step (2026-09-29)

Release research-workflow-batch2-20260929.1, exact image `sha256:e5f62c4b66a30a05a0742f676709cb8374202653af62ca5ec871c34c5c6e50be`. Backend then frontend; admission writes/runs held closed through mixed versions. Explicit typed mapping with immutable indicator/code/contract provenance; no strategy-rule, migration, business-history, timer, integration or credential change. Owner approved backend ENABLE_OFFLINE_AI_CALIBRATION=false to prevent startup calibration. Retain previous frontend assets. Rollback frontend then backend under closed admission; keep calibration false even on old backend, protect drafts and refresh cached clients. Do not reopen old backend runs on new v1 records without reviewed compatibility. Evidence: /home/ubuntu/releases/research-workflow-batch2-20260929.1/execution-revision-1.


## Profit execution candidate (2026-10-09)

integration-api switched to `trading-radar-integration:profit-execution-candidate-20261008.1` `sha256:7eba0c24a339c761a523aa17545bfaf6d69823342a0b588cac92445ef14b9b21`. Backend/frontend not changed in this step. Rollback images retained. No database restore/drop.

quantdinger-backend switched to `trading-radar-quantdinger-backend:profit-execution-candidate-20261008.1` `sha256:4bb5f55a527dfc2ee421e11fcd98b559dd835e8ff6135704c073a152d35888eb`. Frontend unchanged.

## nnG8 read-only integration configuration step (2026-10-10)

2026-10-10T01:02:58.955453+00:00. Only `integration-api` configuration/Pin were staged; local image ID `sha256:bddfadc83dd19a84de679ff320b2873b2eac7e2a1c0cf4153afdfd48806d1175`.
This helper did not rebuild a container or run any business task. Main installation must
record all-service runtime/health acceptance before proceeding to the next phase.
Historical release sections are retained unchanged; machine Pin is the current gate.

## nnG8 read-only integration runtime accepted (2026-10-10)

2026-10-10T01:07:55.779295+00:00. All three services match healthy machine Pin; only the target container was recreated.
No business task or database write was performed by this helper.

## nnG8 read-only backend-disabled configuration step (2026-10-10)

2026-10-10T01:08:25.918437+00:00. Only `quantdinger-backend` configuration/Pin were staged; local image ID `sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030`.
This helper did not rebuild a container or run any business task. Main installation must
record all-service runtime/health acceptance before proceeding to the next phase.
Historical release sections are retained unchanged; machine Pin is the current gate.

## nnG8 read-only backend-disabled runtime accepted (2026-10-10)

2026-10-10T01:09:39.483015+00:00. All three services match healthy machine Pin; only the target container was recreated.
No business task or database write was performed by this helper.

## nnG8 read-only backend-enabled configuration step (2026-10-10)

2026-10-10T01:09:46.621468+00:00. Only `quantdinger-backend` configuration/Pin were staged; local image ID `sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030`.
This helper did not rebuild a container or run any business task. Main installation must
record all-service runtime/health acceptance before proceeding to the next phase.
Historical release sections are retained unchanged; machine Pin is the current gate.

## nnG8 read-only backend-enabled runtime accepted (2026-10-10)

2026-10-10T01:10:55.393137+00:00. All three services match healthy machine Pin; only the target container was recreated.
No business task or database write was performed by this helper.

## nnG8 read-only frontend configuration step (2026-10-10)

2026-10-10T01:10:59.205666+00:00. Only `quantdinger-frontend` configuration/Pin were staged; local image ID `sha256:d9fc1a485db671de3ccfd0eeb8b624a8050aa6dd630d8bba0103a79004ceda38`.
This helper did not rebuild a container or run any business task. Main installation must
record all-service runtime/health acceptance before proceeding to the next phase.
Historical release sections are retained unchanged; machine Pin is the current gate.

## nnG8 read-only frontend runtime accepted (2026-10-10)

2026-10-10T01:13:05.881175+00:00. All three services match healthy machine Pin; only the target container was recreated.
No business task or database write was performed by this helper.

<!-- nng8-linkage:nng8-research-linkage-20261010.1:quantdinger-backend:staging -->
<!-- linkage-identities: {"newImageId":"sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860","newReference":"trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2","oldImageId":"sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030","oldReference":"trading-radar-quantdinger-backend:nng8-sync-health-candidate-20261009.1","service":"quantdinger-backend","stageId":"nng8-research-linkage-20261010.1"} -->
## nnG8 research linkage quantdinger-backend staging (nng8-research-linkage-20261010.1)

Recorded at: 2026-10-10T04:33:36.573105+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:nng8-sync-health-candidate-20261009.1` / `sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030`.
Target identity: `trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2` / `sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- nng8-linkage:nng8-research-linkage-20261010.1:quantdinger-backend:runtime -->
<!-- linkage-identities: {"newImageId":"sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860","newReference":"trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2","oldImageId":"sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030","oldReference":"trading-radar-quantdinger-backend:nng8-sync-health-candidate-20261009.1","service":"quantdinger-backend","stageId":"nng8-research-linkage-20261010.1"} -->
## nnG8 research linkage quantdinger-backend runtime (nng8-research-linkage-20261010.1)

Recorded at: 2026-10-10T04:33:46.744432+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:nng8-sync-health-candidate-20261009.1` / `sha256:7658ade72ee853154326d506f6049db0fbcb0b808366f2c604beacc47e550030`.
Target identity: `trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2` / `sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860`.
Observed container: `20f7779e6609c995c589932b6a64117c3d3f566bfbedfbf8f44942b645a99d26`.
Observed image: `trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2` / `sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- nng8-linkage:nng8-research-linkage-20261010.1:quantdinger-frontend:staging -->
<!-- linkage-identities: {"newImageId":"sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3","newReference":"trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2","oldImageId":"sha256:d9fc1a485db671de3ccfd0eeb8b624a8050aa6dd630d8bba0103a79004ceda38","oldReference":"trading-radar-quantdinger-frontend:nng8-sync-health-candidate-20261009.1","service":"quantdinger-frontend","stageId":"nng8-research-linkage-20261010.1"} -->
## nnG8 research linkage quantdinger-frontend staging (nng8-research-linkage-20261010.1)

Recorded at: 2026-10-10T04:33:47.137588+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:nng8-sync-health-candidate-20261009.1` / `sha256:d9fc1a485db671de3ccfd0eeb8b624a8050aa6dd630d8bba0103a79004ceda38`.
Target identity: `trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2` / `sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- nng8-linkage:nng8-research-linkage-20261010.1:quantdinger-frontend:runtime -->
<!-- linkage-identities: {"newImageId":"sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3","newReference":"trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2","oldImageId":"sha256:d9fc1a485db671de3ccfd0eeb8b624a8050aa6dd630d8bba0103a79004ceda38","oldReference":"trading-radar-quantdinger-frontend:nng8-sync-health-candidate-20261009.1","service":"quantdinger-frontend","stageId":"nng8-research-linkage-20261010.1"} -->
## nnG8 research linkage quantdinger-frontend runtime (nng8-research-linkage-20261010.1)

Recorded at: 2026-10-10T04:33:56.168399+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:nng8-sync-health-candidate-20261009.1` / `sha256:d9fc1a485db671de3ccfd0eeb8b624a8050aa6dd630d8bba0103a79004ceda38`.
Target identity: `trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2` / `sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3`.
Observed container: `69f0d53967c98f7f65fda9a6f2ff64ae9b070b763f00c23d4e26569d4b76be58`.
Observed image: `trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2` / `sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-continuation-20261010.2:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e","newReference":"trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2","oldImageId":"sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3","oldReference":"trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2","service":"quantdinger-frontend","stageId":"unified-research-continuation-20261010.2"} -->
## Unified research frontend quantdinger-frontend staging (unified-research-continuation-20261010.2)

Recorded at: 2026-10-10T09:39:12.398349+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2` / `sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3`.
Target identity: `trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2` / `sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-continuation-20261010.2:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e","newReference":"trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2","oldImageId":"sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3","oldReference":"trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2","service":"quantdinger-frontend","stageId":"unified-research-continuation-20261010.2"} -->
## Unified research frontend quantdinger-frontend runtime (unified-research-continuation-20261010.2)

Recorded at: 2026-10-10T09:39:23.794315+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:nng8-research-linkage-candidate-20261010.2` / `sha256:b9252c9094a82a7a10b2ff89ebf817d9644199c3cb49f94f7d85b2c5219c7af3`.
Target identity: `trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2` / `sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e`.
Observed container: `d4737ee14791ccb1cd88491c331da70a186d99afba7f714b5e3354be7f5ea8f6`.
Observed image: `trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2` / `sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:integration-api:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7","newReference":"trading-radar-integration:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:bddfadc83dd19a84de679ff320b2873b2eac7e2a1c0cf4153afdfd48806d1175","oldReference":"trading-radar-integration:nng8-sync-health-candidate-20261009.2","service":"integration-api","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion integration-api staging (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:55:59.207593+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-integration:nng8-sync-health-candidate-20261009.2` / `sha256:bddfadc83dd19a84de679ff320b2873b2eac7e2a1c0cf4153afdfd48806d1175`.
Target identity: `trading-radar-integration:unified-research-completion-candidate-20261010.1` / `sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:integration-api:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7","newReference":"trading-radar-integration:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:bddfadc83dd19a84de679ff320b2873b2eac7e2a1c0cf4153afdfd48806d1175","oldReference":"trading-radar-integration:nng8-sync-health-candidate-20261009.2","service":"integration-api","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion integration-api runtime (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:56:12.122805+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-integration:nng8-sync-health-candidate-20261009.2` / `sha256:bddfadc83dd19a84de679ff320b2873b2eac7e2a1c0cf4153afdfd48806d1175`.
Target identity: `trading-radar-integration:unified-research-completion-candidate-20261010.1` / `sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7`.
Observed container: `ad2318981e7056c5b9fbcd22aa66a6a3404bc6bf2eacb36aa7fed865ce7eb717`.
Observed image: `trading-radar-integration:unified-research-completion-candidate-20261010.1` / `sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:quantdinger-backend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a","newReference":"trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860","oldReference":"trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2","service":"quantdinger-backend","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion quantdinger-backend staging (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:56:14.026163+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2` / `sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860`.
Target identity: `trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1` / `sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:quantdinger-backend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a","newReference":"trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860","oldReference":"trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2","service":"quantdinger-backend","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion quantdinger-backend runtime (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:56:26.657528+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:nng8-research-linkage-candidate-20261010.2` / `sha256:82f8ce714ae21ea1cc848ed466a86c17d7e0c4caac05e4e2031397976920f860`.
Target identity: `trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1` / `sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a`.
Observed container: `794193182a8ad14019e832a4015b0580c1b192a3e89ab06e3974a423fb383260`.
Observed image: `trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1` / `sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1","newReference":"trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e","oldReference":"trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2","service":"quantdinger-frontend","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion quantdinger-frontend staging (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:56:28.209404+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2` / `sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e`.
Target identity: `trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1` / `sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-research-completion-20261010.1:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1","newReference":"trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1","oldImageId":"sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e","oldReference":"trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2","service":"quantdinger-frontend","stageId":"unified-research-completion-20261010.1"} -->
## Unified research completion quantdinger-frontend runtime (unified-research-completion-20261010.1)

Recorded at: 2026-10-10T10:56:39.555732+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:unified-research-candidate-20261010.2` / `sha256:8465f5ad08a45a9b87888db5b14ed0adb9a9636ef44764cadcc2d302c1947d3e`.
Target identity: `trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1` / `sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1`.
Observed container: `bebb277713a54ac6456818d0b7df7fa30fcb773dfcf3c7ac6b311971584f405a`.
Observed image: `trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1` / `sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:nng8-operations-20261010.1:quantdinger-backend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc","newReference":"trading-radar-quantdinger-backend:nng8-operations-20261010.2","oldImageId":"sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a","oldReference":"trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1","service":"quantdinger-backend","stageId":"nng8-operations-20261010.1"} -->
## Unified research completion quantdinger-backend staging (nng8-operations-20261010.1)

Recorded at: 2026-10-10T12:29:59.818147+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1` / `sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a`.
Target identity: `trading-radar-quantdinger-backend:nng8-operations-20261010.2` / `sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:nng8-operations-20261010.1:quantdinger-backend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc","newReference":"trading-radar-quantdinger-backend:nng8-operations-20261010.2","oldImageId":"sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a","oldReference":"trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1","service":"quantdinger-backend","stageId":"nng8-operations-20261010.1"} -->
## Unified research completion quantdinger-backend runtime (nng8-operations-20261010.1)

Recorded at: 2026-10-10T12:30:13.445759+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:unified-research-completion-candidate-20261010.1` / `sha256:e23e5134494afa6c8376d4ad75afdae5ae8a7b312506f6af1ad143cebeee6d0a`.
Target identity: `trading-radar-quantdinger-backend:nng8-operations-20261010.2` / `sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc`.
Observed container: `fd90968f02f245321cbb4ad3b9d4a1b6ec34bc29a8effba940df1f5fc7410716`.
Observed image: `trading-radar-quantdinger-backend:nng8-operations-20261010.2` / `sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:nng8-operations-20261010.1:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55","newReference":"trading-radar-quantdinger-frontend:nng8-operations-20261010.1","oldImageId":"sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1","oldReference":"trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1","service":"quantdinger-frontend","stageId":"nng8-operations-20261010.1"} -->
## Unified research completion quantdinger-frontend staging (nng8-operations-20261010.1)

Recorded at: 2026-10-10T12:30:14.971650+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1` / `sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1`.
Target identity: `trading-radar-quantdinger-frontend:nng8-operations-20261010.1` / `sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:nng8-operations-20261010.1:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55","newReference":"trading-radar-quantdinger-frontend:nng8-operations-20261010.1","oldImageId":"sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1","oldReference":"trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1","service":"quantdinger-frontend","stageId":"nng8-operations-20261010.1"} -->
## Unified research completion quantdinger-frontend runtime (nng8-operations-20261010.1)

Recorded at: 2026-10-10T12:30:26.366862+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:unified-research-completion-candidate-20261010.1` / `sha256:ace4cdea29e478f36a2b449e164eb668d7aace76b531c881fd7357b77472f0d1`.
Target identity: `trading-radar-quantdinger-frontend:nng8-operations-20261010.1` / `sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55`.
Observed container: `6b882484af46d1a6bcc50d6fd9e2e1b760df016128e557e08f3cb0e4bdd6ee19`.
Observed image: `trading-radar-quantdinger-frontend:nng8-operations-20261010.1` / `sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:research-trust-completion-20261010.2:quantdinger-backend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51","newReference":"trading-radar-quantdinger-backend:research-trust-completion-20261010.2","oldImageId":"sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc","oldReference":"trading-radar-quantdinger-backend:nng8-operations-20261010.2","service":"quantdinger-backend","stageId":"research-trust-completion-20261010.2"} -->
## Unified research completion quantdinger-backend staging (research-trust-completion-20261010.2)

Recorded at: 2026-10-10T14:33:17.519186+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:nng8-operations-20261010.2` / `sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc`.
Target identity: `trading-radar-quantdinger-backend:research-trust-completion-20261010.2` / `sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:research-trust-completion-20261010.2:quantdinger-backend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51","newReference":"trading-radar-quantdinger-backend:research-trust-completion-20261010.2","oldImageId":"sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc","oldReference":"trading-radar-quantdinger-backend:nng8-operations-20261010.2","service":"quantdinger-backend","stageId":"research-trust-completion-20261010.2"} -->
## Unified research completion quantdinger-backend runtime (research-trust-completion-20261010.2)

Recorded at: 2026-10-10T14:33:30.404539+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:nng8-operations-20261010.2` / `sha256:3084c96649f6fdfb3aaa935574788d1066c222da31588754f2f9eae49d2940cc`.
Target identity: `trading-radar-quantdinger-backend:research-trust-completion-20261010.2` / `sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51`.
Observed container: `02026a38173a426beedfde0103edb7e360712cbf2cc79d5d9e7d9181b21a87fd`.
Observed image: `trading-radar-quantdinger-backend:research-trust-completion-20261010.2` / `sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:research-trust-completion-20261010.2:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70","newReference":"trading-radar-quantdinger-frontend:research-trust-completion-20261010.2","oldImageId":"sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55","oldReference":"trading-radar-quantdinger-frontend:nng8-operations-20261010.1","service":"quantdinger-frontend","stageId":"research-trust-completion-20261010.2"} -->
## Unified research completion quantdinger-frontend staging (research-trust-completion-20261010.2)

Recorded at: 2026-10-10T14:33:31.773923+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:nng8-operations-20261010.1` / `sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55`.
Target identity: `trading-radar-quantdinger-frontend:research-trust-completion-20261010.2` / `sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:research-trust-completion-20261010.2:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70","newReference":"trading-radar-quantdinger-frontend:research-trust-completion-20261010.2","oldImageId":"sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55","oldReference":"trading-radar-quantdinger-frontend:nng8-operations-20261010.1","service":"quantdinger-frontend","stageId":"research-trust-completion-20261010.2"} -->
## Unified research completion quantdinger-frontend runtime (research-trust-completion-20261010.2)

Recorded at: 2026-10-10T14:33:43.246145+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:nng8-operations-20261010.1` / `sha256:cd4af17d84924a84880a21cf2c66c883680978ee6126dadbec4eda0e78988b55`.
Target identity: `trading-radar-quantdinger-frontend:research-trust-completion-20261010.2` / `sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70`.
Observed container: `de7c04a5927f23f76d4e8416804fac96bbade5feff767932c2a1949bdf8005ad`.
Observed image: `trading-radar-quantdinger-frontend:research-trust-completion-20261010.2` / `sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:security-p-frontend-only-20261013.1:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20","newReference":"quantdinger-frontend:security-log-protection-20261013.1","oldImageId":"sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70","oldReference":"trading-radar-quantdinger-frontend:research-trust-completion-20261010.2","service":"quantdinger-frontend","stageId":"security-p-frontend-only-20261013.1"} -->
## Unified research completion quantdinger-frontend staging (security-p-frontend-only-20261013.1)

Recorded at: 2026-10-14T06:28:55.643512+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:research-trust-completion-20261010.2` / `sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70`.
Target identity: `quantdinger-frontend:security-log-protection-20261013.1` / `sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:security-p-frontend-only-20261013.1:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20","newReference":"quantdinger-frontend:security-log-protection-20261013.1","oldImageId":"sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70","oldReference":"trading-radar-quantdinger-frontend:research-trust-completion-20261010.2","service":"quantdinger-frontend","stageId":"security-p-frontend-only-20261013.1"} -->
## Unified research completion quantdinger-frontend runtime (security-p-frontend-only-20261013.1)

Recorded at: 2026-10-14T06:28:55.643512+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:research-trust-completion-20261010.2` / `sha256:831dfab099e10332b79e0c67bdf41684cd57eccfd4a042f8e3b51f8c9b7c2d70`.
Target identity: `quantdinger-frontend:security-log-protection-20261013.1` / `sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20`.
Observed container: `c383aa84ba8819f1d40d937c27e092104626ceebd46b2d5f82bb62bb286d5432`.
Observed image: `quantdinger-frontend:security-log-protection-20261013.1` / `sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:integration-api:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da","newReference":"trading-radar-integration:unified-product-closeout-20261014.1","oldImageId":"sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7","oldReference":"trading-radar-integration:unified-research-completion-candidate-20261010.1","service":"integration-api","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion integration-api staging (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:19:30.939797+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-integration:unified-research-completion-candidate-20261010.1` / `sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7`.
Target identity: `trading-radar-integration:unified-product-closeout-20261014.1` / `sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:integration-api:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da","newReference":"trading-radar-integration:unified-product-closeout-20261014.1","oldImageId":"sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7","oldReference":"trading-radar-integration:unified-research-completion-candidate-20261010.1","service":"integration-api","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion integration-api runtime (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:19:43.064865+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-integration:unified-research-completion-candidate-20261010.1` / `sha256:cf751f6ee614c77630afbd6acfdc49598d18b5732c7eac93df1a57d5307042c7`.
Target identity: `trading-radar-integration:unified-product-closeout-20261014.1` / `sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da`.
Observed container: `84e9c79e56f3293ba762ee690a68eb9e4f115b5e2da7ffcefcbde88aa4136568`.
Observed image: `trading-radar-integration:unified-product-closeout-20261014.1` / `sha256:c8bde783b3d73574bf0bb4afaf36548e7f1903c8ceefe8067bd52fb43a8001da`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:quantdinger-backend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf","newReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.1","oldImageId":"sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51","oldReference":"trading-radar-quantdinger-backend:research-trust-completion-20261010.2","service":"quantdinger-backend","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion quantdinger-backend staging (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:19:44.516298+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:research-trust-completion-20261010.2` / `sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51`.
Target identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.1` / `sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:quantdinger-backend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf","newReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.1","oldImageId":"sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51","oldReference":"trading-radar-quantdinger-backend:research-trust-completion-20261010.2","service":"quantdinger-backend","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion quantdinger-backend runtime (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:20:00.861051+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:research-trust-completion-20261010.2` / `sha256:9685d08ed0b265945b327a89287ca9e1e4b5cd2e8eeb974519aedca3ca523d51`.
Target identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.1` / `sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf`.
Observed container: `992dbb04958b1dc5e5df2afcfba47291cee33d78f862bdce8cabfa1b4e6f2738`.
Observed image: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.1` / `sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016","newReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2","oldImageId":"sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20","oldReference":"quantdinger-frontend:security-log-protection-20261013.1","service":"quantdinger-frontend","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion quantdinger-frontend staging (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:20:02.288520+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `quantdinger-frontend:security-log-protection-20261013.1` / `sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20`.
Target identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2` / `sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-20261014.1:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016","newReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2","oldImageId":"sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20","oldReference":"quantdinger-frontend:security-log-protection-20261013.1","service":"quantdinger-frontend","stageId":"unified-product-closeout-20261014.1"} -->
## Unified research completion quantdinger-frontend runtime (unified-product-closeout-20261014.1)

Recorded at: 2026-10-14T12:20:13.259301+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `quantdinger-frontend:security-log-protection-20261013.1` / `sha256:9addf3de67ae4b9b76bc236c21ab64c5d446c7b3c65e6a9303edb2f4105b0d20`.
Target identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2` / `sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016`.
Observed container: `6ed57a2a6d4dedb9c8b6549ae79d5ac26470f7d56cb34d035f59741018692941`.
Observed image: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2` / `sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-backend-followup-20261014.2:quantdinger-backend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4","newReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.2","oldImageId":"sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf","oldReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.1","service":"quantdinger-backend","stageId":"unified-product-closeout-backend-followup-20261014.2"} -->
## Unified research completion quantdinger-backend staging (unified-product-closeout-backend-followup-20261014.2)

Recorded at: 2026-10-14T12:52:10.766420+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.1` / `sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf`.
Target identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` / `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-backend-followup-20261014.2:quantdinger-backend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4","newReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.2","oldImageId":"sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf","oldReference":"trading-radar-quantdinger-backend:unified-product-closeout-20261014.1","service":"quantdinger-backend","stageId":"unified-product-closeout-backend-followup-20261014.2"} -->
## Unified research completion quantdinger-backend runtime (unified-product-closeout-backend-followup-20261014.2)

Recorded at: 2026-10-14T12:52:22.160260+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.1` / `sha256:ae4a8c1df56440ba970ce6408e2c08bb79a9ef4a8dfa3eb60e2c4158e620a5cf`.
Target identity: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` / `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4`.
Observed container: `eef9c5b9e33234530c174ee2a1bf28fe132e0a65d667d11ed0334585538350d7`.
Observed image: `trading-radar-quantdinger-backend:unified-product-closeout-20261014.2` / `sha256:659d6ae54745b6c4834daa5322d8a5cc08934b3d886f281cd09975226c61f8e4`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-frontend-followup-20261014.3:quantdinger-frontend:staging -->
<!-- unified-frontend-identities: {"newImageId":"sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5","newReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3","oldImageId":"sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016","oldReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2","service":"quantdinger-frontend","stageId":"unified-product-closeout-frontend-followup-20261014.3"} -->
## Unified research completion quantdinger-frontend staging (unified-product-closeout-frontend-followup-20261014.3)

Recorded at: 2026-10-14T13:20:10.305939+00:00.
Configuration staged; runtime acceptance pending. Running images above remain the previously accepted runtime.
Previous identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2` / `sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016`.
Target identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3` / `sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.

<!-- unified-frontend:unified-product-closeout-frontend-followup-20261014.3:quantdinger-frontend:runtime -->
<!-- unified-frontend-identities: {"newImageId":"sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5","newReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3","oldImageId":"sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016","oldReference":"trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2","service":"quantdinger-frontend","stageId":"unified-product-closeout-frontend-followup-20261014.3"} -->
## Unified research completion quantdinger-frontend runtime (unified-product-closeout-frontend-followup-20261014.3)

Recorded at: 2026-10-14T13:20:20.943596+00:00.
Actual target runtime was observed running and healthy by the guarded caller; non-target runtime was verified unchanged.
Previous identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.2` / `sha256:5155f6e85d4bbb1ca1327a72fa951ce8fa253871520e67959e1cd6090feab016`.
Target identity: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3` / `sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5`.
Observed container: `dd70ffbedced18fa6bdb319c1039ae226fcea1ca16f9f6ed2a659b9f1306e293`.
Observed image: `trading-radar-quantdinger-frontend:unified-product-closeout-20261014.3` / `sha256:a52c8a4a27431d4eaeeb72a0b9c2ff5d43f295beab411178451d91fdb909e6a5`.
Historical release sections are retained byte-for-byte. This pure helper performs no deployment or business call.
