# Quant integration candidate: production runbook

This directory is a patch candidate, not a replacement release tree. Apply it
to `/home/ubuntu/trading-radar` only after review and from a timestamped staging
directory. Do not copy `tests/fixtures/docker-compose.full-candidate.yml` into
production and do not overwrite the production `docker-compose.yml`.

## Deliberate behavior changes

- Dashboard authentication is fail-closed and header-only. Exactly `/health`
  remains public. HTML, static files, `/api/status`, `/api/health`, metrics,
  Blueprint/direct routes and SSE all require `X-Dashboard-Token`.
- Consequently, the existing browser Dashboard and native `EventSource` client
  will return 401 until a same-origin session-auth or trusted reverse-proxy
  adapter is implemented. Do not weaken this boundary with query-string tokens.
- The Quant integration API is a separate Gunicorn process. It exposes only
  liveness, readiness and a read-only market-context contract at the fixed
  address `172.28.92.2:8092` on an internal Docker network. Docker does not
  publish that port on the host, and Nginx must not proxy port 8092.

## Preflight

1. Back up only the files this patch changes and record the current image IDs:

   ```sh
   cd /home/ubuntu/trading-radar
   stamp=$(date -u +%Y%m%dT%H%M%SZ)
   mkdir -p "releases/quant-integration-$stamp/previous"
   docker compose images > "releases/quant-integration-$stamp/images.txt"
   ```

   Copy the existing `dashboard/app.py`, `dashboard/api/routes.py` and
   `.dockerignore` into that `previous` directory before installation. Keep the
   staged candidate alongside the backup so rollback does not depend on Git or
   the network.

2. Merge the candidate `.dockerignore` additions into the existing file. At a
   minimum these two patterns must be present; never replace unrelated existing
   ignore rules:

   ```text
   secrets/
   **/secrets/*
   ```

3. Create two independent host secrets without putting either value in an
   environment variable, command argument or log:

   ```sh
   install -d -m 700 secrets
   umask 077
   openssl rand -hex 32 > secrets/dashboard_api_token
   openssl rand -hex 32 > secrets/radar_integration_token
   sudo chown root:root secrets/dashboard_api_token secrets/radar_integration_token
   sudo chmod 600 secrets/dashboard_api_token secrets/radar_integration_token
   ```

   The application rejects symlinks, non-regular files, non-ASCII/short values,
   and files writable by group or others. The containers run as UID 0 with all
   capabilities dropped, so root ownership is required for these `0600` bind
   mounts; do not make them world-readable to work around a permission error.

4. Confirm `config.swing.json` contains a `swing` object whose `store_file`
   resolves below `/app/data`. The SQLite database must contain all columns used
   by `macro_slice` and `trend_signal`, including `trend_signal.data_date`.
   Readiness will remain 503 on a missing database or incompatible schema. Also
   ensure the project's trading-calendar cache is mounted below `/app/data` and
   covers the release date. Market freshness uses the latest expected A-share
   EOD date after a 15:30 Asia/Shanghai close grace; an unavailable calendar is
   explicitly degraded instead of guessed from calendar-day age.

   Before releasing, exercise only the calendar's public API against one known
   closed session and the following open session, and confirm Gunicorn exists in
   the Dashboard image:

   ```sh
   docker compose run --rm --no-deps dashboard python -c \
     "import gunicorn; from datetime import datetime; from radar.core.scan_scheduler import HolidayCalendar as H; assert H.is_holiday(datetime(2026,10,1)); assert not H.is_holiday(datetime(2026,10,8))"
   ```

   If either assertion/import fails, stop before release; do not infer calendar
   support from private class fields or silently replace Gunicorn with Flask's
   development server.

## Validate and release

Always merge the production Compose file with the small override. Run these
commands from the production project root:

```sh
docker compose -f docker-compose.yml -f docker-compose.integration.override.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.integration.override.yml build dashboard integration-api
docker compose -f docker-compose.yml -f docker-compose.integration.override.yml up -d --no-deps dashboard integration-api
```

Do not run `docker compose -f docker-compose.integration.override.yml` alone:
the Dashboard stanza is intentionally only a merge fragment.

First verify local liveness/readiness, then run the authenticated contract
smoke. The smoke script reads the secret file internally so the bearer token is
not exposed in shell history or process arguments:

```sh
curl --fail --silent --show-error http://172.28.92.2:8092/health
curl --fail --silent --show-error http://172.28.92.2:8092/ready
docker exec radar-integration-api python /app/scripts/smoke_integration.py \
  --base-url http://127.0.0.1:8092 \
  --token-file /run/secrets/radar_integration_token
```

The release is not healthy merely because `/health` returns 200: `/ready` must
also be 200. The default authenticated smoke is a structural release gate. It
verifies the real Bearer path, schema, `expected_as_of`, signal date causality
and risk flags; it permits a valid degraded snapshot and prints that status.
This lets a safe code release proceed during a weekend, holiday or temporary
upstream-data gap.

Use the stricter data-quality activation gate after the expected trading-day
EOD slice is due:

```sh
docker exec radar-integration-api python /app/scripts/smoke_integration.py \
  --base-url http://127.0.0.1:8092 \
  --token-file /run/secrets/radar_integration_token \
  --require-fresh --require-ok
```

Do not use the strict activation result as a structural deployment gate before
the close grace or during a known degraded-data window.

For remote AI-Filter access, use a dedicated SSH key whose Quant-side
`authorized_keys` options restrict forwarding to `permitopen="172.28.92.2:8092"`.
On JD the restricted forward is exposed to containers only through the Docker
bridge endpoint `172.17.0.1:18092`. Configure only the AI-Filter Worker to call
that endpoint and mount the independent integration token only into Worker;
neither the web nor SSE process receives it. Keep 8092/18092 absent from public
firewall and Nginx configuration.

## Rollback

If readiness or the authenticated smoke fails:

1. Restore the timestamped copies of the three modified existing files and
   remove only the newly installed integration files/override from the active
   release.
2. Rebuild/restart Dashboard from the restored tree using the same two-file
   Compose command, or restore the recorded previous image ID if deployment is
   image-pinned.
3. Stop and remove only `integration-api`, then re-run the original Dashboard
   `/health` check.
4. Retain logs and the failed release directory for diagnosis; do not print or
   archive either secret.

Rollback must not replace the production Compose file, reset the dirty worktree,
or touch the Swing database.
