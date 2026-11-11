# Read-only Quant runtime observer

This observer reads three fixed JSON files and probes three allowlisted internal
HTTP endpoints. It has no credentials, Docker socket or business write path.
HTTP 200 does not certify database internals, trading quality or Telegram delivery.

## Release and operation

- `release.json` records immutable runtime/base image IDs and the source hash.
- `docker-compose.runtime-observer.yml` at the repository root pins the image ID.
- Use standalone Compose project `quant-runtime-observer` with project directory
  `/home/ubuntu/trading-radar`; never combine business Compose files or use down
  on the business project. Only this observer service may be recreated.
- Internal address: `runtime-observer:9116`; no host port. `/health` is liveness,
  `/metrics` is file/calendar freshness, `/probe?target=backend|frontend|grafana`
  yields `probe_success`, independently from Prometheus scrape `up`.
- Run as UID/GID 65534, read-only rootfs/data, no-new-privileges, all capabilities
  dropped, memory 64 MiB, CPU 0.1 and PIDs 64.
- Data directory mount is deliberate: reopen files every scrape to see atomic
  cache replacement. Digest currently writes in place; transient invalid reads
  fail closed and the five-minute alert hold filters short write races.

## Freshness contracts

Trading calendar coverage and generation age are separate. Covered weekends and
holidays expect the last trading session; an immediately preceding Friday also
covers the weekend. Unknown coverage is never guessed green. Calendar generation
older than 14 days alerts separately without invalidating covered sessions.

At 15:30 on a trading day the expected date becomes today. Cache stale alerts
wait 30 minutes to accommodate 15:35/45/55 natural repair. Digest input stale
alerts wait 35 minutes to allow the 16:00 refresh. Digest generation older than
90 minutes alerts separately. No automatic backfill, repair, database change or
Telegram notification is performed by this observer.

## Changes and rollback

Use the stdlib tests in `tests/test_runtime_observer.py` and the installed
Prometheus version's `promtool test rules tests/runtime-observer.rules.test.yml`
before deployment (rule paths resolve relative to the test file). Preserve existing three macro
rules and the existing dashboard. The observer dashboard has its own UID.

Prometheus config files are single-file bind mounts: write in place with
compare-before-write checks, verify hashes *inside* Prometheus, run promtool,
then use the enabled lifecycle reload. Do not replace inodes or restart business.
For rollback restore the previous committed config contents in place, validate
and reload, then remove only the new observer and its dashboard if necessary.

This release adds monitoring only. Quant NO_BACKUP and Phase2 FROZEN are unchanged.

## 2026-09-07 observer-only memory release

Version `2026.09.07-memory.1` changes only the fixed internal HTTP probe
implementation: direct HTTPConnection avoids per-request default HTTPS/CA
contexts and explicitly closes responses/connections. Allowlists, timeouts,
HTTP status semantics, metrics, freshness rules and resource limits are unchanged.
Both observer test modules pass (66 tests) on the pinned Python 3.12.14 image.

The release gate uses a network-none, read-only Linux container at 64 MiB,
0.1 CPU and 64 PIDs, with synthetic data and 600 seconds of paced handler load.
It is a bounded regression gate, not proof of indefinite memory stability.
Continue checking natural scrapes and cgroup pressure after deployment.

For this code-only release do not reload Prometheus or alter any dashboard.
Only run the standalone project and service below after verifying image/source
hashes and the release gate; do not use a business Compose project:

```sh
docker compose --project-name quant-runtime-observer \
  --project-directory /home/ubuntu/trading-radar \
  -f /home/ubuntu/trading-radar/docker-compose.runtime-observer.yml \
  up -d --no-deps --no-build --pull never runtime-observer
```

If this observer release fails acceptance, compare current file hashes before
restoring only these release files to parent commit
`537e27500f6fdb9f44d9b0e4f27519040f897e98`, removing only the newly added
`tests/test_probe_memory_contract.py`, and rerun the same observer-only command.
The retained rollback image is
`sha256:07bf1b1e5e1bd6b7ca9e43d4989a9a189573c6ef166d0cb816e39a229f48db90`.
Preserve unrelated worktree changes and never use reset-hard or add-all.
No JD, Hermes, proxy, calendar timer, backup, or Phase2 changes are part of this
release.
