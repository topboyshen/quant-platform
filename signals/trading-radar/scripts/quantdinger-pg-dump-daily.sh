#!/usr/bin/env bash
# Daily QuantDinger Postgres logical dump (local). Restore-drill-friendly.
set -Eeuo pipefail
umask 077

readonly RETAIN_COUNT="${QUANTDINGER_DAILY_RETAIN:-14}"
readonly BACKUP_DIR="/home/ubuntu/backups/quantdinger-postgres"
readonly INNER="/home/ubuntu/trading-radar/scripts/backup_quantdinger_postgres.sh"
export QUANTDINGER_BACKUP_RETENTION_DAYS="${QUANTDINGER_BACKUP_RETENTION_DAYS:-14}"

log() { printf "[%s] %s\n" "$(date -u "+%Y-%m-%dT%H:%M:%SZ")" "$1"; }

if [[ ! -x "$INNER" ]]; then
  log "ERROR: missing inner backup script: $INNER"
  exit 1
fi

log "Starting daily QuantDinger postgres dump (local only)"
"$INNER"

mapfile -t archives < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "quantdinger-*.sql.gz" -printf "%T@ %p\n" | sort -nr | awk "{print \$2}")
total=${#archives[@]}
if (( total > RETAIN_COUNT )); then
  for ((i=RETAIN_COUNT; i<total; i++)); do
    old="${archives[$i]}"
    log "Pruning old dump: $(basename "$old")"
    rm -f -- "$old" "${old}.sha256"
  done
fi

newest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "quantdinger-*.sql.gz" -printf "%T@ %p\n" | sort -nr | head -1 | awk "{print \$2}")"
if [[ -z "$newest" ]]; then
  log "ERROR: no dump found after backup"
  exit 1
fi
size="$(du -h "$newest" | cut -f1)"
log "Newest dump: $newest ($size)"
[[ -f "${newest}.sha256" ]] && log "Checksum: ${newest}.sha256"
