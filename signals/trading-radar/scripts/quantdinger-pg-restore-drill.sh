#!/usr/bin/env bash
# One-shot restore drill: load newest local dump into a throwaway DB, verify, drop.
# Never touches the production `quantdinger` database.
set -Eeuo pipefail
umask 077

readonly CONTAINER="${QUANTDINGER_POSTGRES_CONTAINER:-radar-quantdinger-postgres}"
readonly DB_USER="${QUANTDINGER_POSTGRES_USER:-quantdinger}"
readonly PROD_DB="${QUANTDINGER_POSTGRES_DB:-quantdinger}"
readonly BACKUP_DIR="/home/ubuntu/backups/quantdinger-postgres"
readonly DRILL_DB="quantdinger_restore_drill"

log() { printf "[%s] %s\n" "$(date -u "+%Y-%m-%dT%H:%M:%SZ")" "$1"; }

newest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "quantdinger-*.sql.gz" -printf "%T@ %p\n" | sort -nr | head -1 | awk "{print \$2}")"
if [[ -z "$newest" ]]; then
  log "ERROR: no dump found in $BACKUP_DIR"
  exit 1
fi
checksum="${newest}.sha256"
if [[ ! -f "$checksum" ]]; then
  log "ERROR: missing checksum $checksum"
  exit 1
fi

log "Verifying checksum for $(basename "$newest")"
(cd "$BACKUP_DIR" && sha256sum -c -- "$(basename "$checksum")")

health="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
if [[ "$health" != "healthy" ]]; then
  log "ERROR: postgres not healthy: $health"
  exit 1
fi

docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DRILL_DB' AND pid <> pg_backend_pid();" \
  >/dev/null
docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS $DRILL_DB;"
docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE $DRILL_DB OWNER $DB_USER;"

log "Restoring into $DRILL_DB"
gzip -cd -- "$newest" | docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DRILL_DB" -v ON_ERROR_STOP=1 -q

prod_tables="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$PROD_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
drill_tables="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DRILL_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
prod_size="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$PROD_DB" -Atc "SELECT pg_database_size('$PROD_DB');")"
drill_size="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DRILL_DB" -Atc "SELECT pg_database_size('$DRILL_DB');")"

log "prod tables=$prod_tables size=$prod_size"
log "drill tables=$drill_tables size=$drill_size"

if [[ "$drill_tables" -lt 1 ]]; then
  log "ERROR: restored DB has no public tables"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB;"
  exit 1
fi
if [[ "$drill_tables" != "$prod_tables" ]]; then
  log "ERROR: table count mismatch prod=$prod_tables drill=$drill_tables"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB;"
  exit 1
fi

docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE $DRILL_DB;"
log "Restore drill OK; throwaway DB dropped"
