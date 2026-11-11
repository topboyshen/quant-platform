#!/usr/bin/env bash
set -Eeuo pipefail

# NO_BACKUP: owner policy includes manual and pre-release database copies.
# Deliberately no environment override. A policy reversal requires new approval.
printf '%s\n' 'REFUSED: Quant NO_BACKUP policy; no dump, upload, restore or retention executed.' >&2
exit 78

umask 077

# QuantDinger PostgreSQL logical backup. The default mode creates and validates
# a local archive. --upload additionally uploads the archive and checksum to
# Tencent COS, downloads both again, and compares their contents before local
# retention is applied.

readonly CONTAINER="${QUANTDINGER_POSTGRES_CONTAINER:-radar-quantdinger-postgres}"
readonly DATABASE="${QUANTDINGER_POSTGRES_DB:-quantdinger}"
readonly DB_USER="${QUANTDINGER_POSTGRES_USER:-quantdinger}"
readonly BACKUP_DIR="/home/ubuntu/backups/quantdinger-postgres"
readonly RETENTION_DAYS="${QUANTDINGER_BACKUP_RETENTION_DAYS:-14}"
readonly LOCK_FILE="${BACKUP_DIR}/.backup.lock"
readonly BACKUP_TIMEOUT_SECONDS="${QUANTDINGER_BACKUP_TIMEOUT_SECONDS:-900}"

readonly COS_BUCKET="jiejiezi-backup-1259080705"
readonly COS_REMOTE_DIR="backups/postgres/quantdinger"
readonly COSCMD_BIN="${QUANTDINGER_COSCMD_BIN:-/home/ubuntu/.coscmd-venv/bin/coscmd}"
readonly COS_TRANSFER_TIMEOUT_SECONDS="${QUANTDINGER_COS_TIMEOUT_SECONDS:-900}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

usage() {
  printf '用法: %s [--upload]\n' "$0" >&2
}

upload_to_cos=0
if [[ $# -gt 1 ]]; then
  usage
  exit 2
fi
case "${1:-}" in
  "") ;;
  --upload) upload_to_cos=1 ;;
  *)
    usage
    exit 2
    ;;
esac

for numeric_setting in RETENTION_DAYS BACKUP_TIMEOUT_SECONDS COS_TRANSFER_TIMEOUT_SECONDS; do
  numeric_value="${!numeric_setting}"
  if [[ ! "$numeric_value" =~ ^[1-9][0-9]*$ ]]; then
    log "❌ $numeric_setting 必须是正整数，当前值无效"
    exit 2
  fi
done
readonly HOST_BACKUP_TIMEOUT_SECONDS="$((BACKUP_TIMEOUT_SECONDS + 35))"

resolved_backup_dir="$(realpath -m -- "$BACKUP_DIR")"
if [[ "$BACKUP_DIR" != /* || "$resolved_backup_dir" != "$BACKUP_DIR" || -L "$BACKUP_DIR" ]]; then
  log "❌ 备份目录必须是无符号链接组件的规范绝对路径: $BACKUP_DIR"
  exit 1
fi
install -d -m 700 "$BACKUP_DIR"

canonical_backup_dir="$(realpath -e -- "$BACKUP_DIR")"
backup_owner="$(stat -c '%U' "$BACKUP_DIR")"
current_user="$(id -un)"
current_uid="$(id -u)"
backup_mode="$(stat -c '%a' "$BACKUP_DIR")"
backup_uid="$(stat -c '%u' "$BACKUP_DIR")"
if [[ "$canonical_backup_dir" != "$BACKUP_DIR" || "$backup_owner" != "$current_user" || "$backup_uid" != "$current_uid" || "$backup_mode" != "700" ]]; then
  log "❌ 备份目录必须是无符号链接组件的规范绝对路径、由当前用户拥有且权限为 0700"
  exit 1
fi
if [[ -L "$LOCK_FILE" || ( -e "$LOCK_FILE" && ! -f "$LOCK_FILE" ) ]]; then
  log "❌ 备份锁文件类型不安全: $LOCK_FILE"
  exit 1
fi
if [[ -e "$LOCK_FILE" && ( "$(stat -c '%u' "$LOCK_FILE")" != "$current_uid" || "$(stat -c '%h' "$LOCK_FILE")" != "1" ) ]]; then
  log "❌ 备份锁文件不属于当前用户"
  exit 1
fi

exec 9>>"$LOCK_FILE"
if ! flock -n 9; then
  log "❌ 另一个 PostgreSQL 备份任务正在运行，本次退出"
  exit 1
fi
chmod 600 "$LOCK_FILE"
if [[ "$(stat -c '%u' "$LOCK_FILE")" != "$current_uid" || "$(stat -c '%h' "$LOCK_FILE")" != "1" ]]; then
  log "❌ 备份锁文件所有者或链接数不安全"
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary="$(mktemp "$BACKUP_DIR/.quantdinger-${timestamp}.XXXXXXXX.sql.gz.partial")"
temporary_name="${temporary##*/}"
archive_name="${temporary_name#.}"
archive_name="${archive_name%.partial}"
checksum_name="${archive_name}.sha256"
checksum_temporary="$(mktemp "$BACKUP_DIR/.${checksum_name}.XXXXXXXX.partial")"
archive="$BACKUP_DIR/$archive_name"
checksum="$BACKUP_DIR/$checksum_name"
verification_dir=""
retention_list=""
container_dump_pid_file="/tmp/.quantdinger-pgdump-${archive_name}.pid"
dump_cleanup_pending=0
backup_marker="-- QuantDinger backup ${archive_name} complete"
native_dump_marker="-- PostgreSQL database dump complete"

cleanup() {
  cleanup_rc=$?
  trap - EXIT
  set +e
  cleanup_failed=0
  if [[ "$dump_cleanup_pending" -eq 1 ]]; then
    if ! docker exec "$CONTAINER" sh -c '
      pid_file=$1
      if [ ! -f "$pid_file" ]; then
        exit 0
      fi
      runner_pid=$(cat "$pid_file")
      case "$runner_pid" in
        ""|*[!0-9]*) rm -f -- "$pid_file"; exit 1 ;;
      esac
      kill -TERM "-$runner_pid" 2>/dev/null || true
      sleep 1
      kill -KILL "-$runner_pid" 2>/dev/null || true
      rm -f -- "$pid_file"
    ' sh "$container_dump_pid_file"; then
      log "❌ 无法确认容器内 pg_dump 进程组已清理"
      cleanup_failed=1
    fi
  fi
  for cleanup_file in "$temporary" "$checksum_temporary" "$retention_list"; do
    if [[ -n "$cleanup_file" && -e "$cleanup_file" ]] && ! rm -f -- "$cleanup_file"; then
      log "❌ 无法清理临时文件: $cleanup_file"
      cleanup_failed=1
    fi
  done
  if [[ -n "$verification_dir" ]]; then
    if ! rm -rf -- "$verification_dir"; then
      log "❌ 无法清理 COS 回读临时目录: $verification_dir"
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_rc" -eq 0 && "$cleanup_failed" -ne 0 ]]; then
    cleanup_rc=1
  fi
  exit "$cleanup_rc"
}
trap cleanup EXIT

container_health="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
if [[ "$container_health" != "healthy" ]]; then
  log "❌ PostgreSQL 容器状态不是 healthy: $container_health"
  exit 1
fi

log "开始创建 PostgreSQL 逻辑备份: $DATABASE"
dump_cleanup_pending=1
{
  timeout --signal=TERM --kill-after=35s "${HOST_BACKUP_TIMEOUT_SECONDS}s" \
    docker exec "$CONTAINER" sh -c '
      pid_file=$1
      timeout_seconds=$2
      db_user=$3
      database=$4
      setsid timeout -s TERM -k 30 "$timeout_seconds" \
        pg_dump \
          --username "$db_user" \
          --dbname "$database" \
          --no-owner \
          --no-privileges &
      runner_pid=$!
      printf "%s\n" "$runner_pid" >"$pid_file"
      wait "$runner_pid"
      runner_rc=$?
      if [ "$runner_rc" -eq 0 ]; then
        rm -f -- "$pid_file"
      fi
      exit "$runner_rc"
    ' sh "$container_dump_pid_file" "$BACKUP_TIMEOUT_SECONDS" "$DB_USER" "$DATABASE" \
    && printf '\n%s\n' "$backup_marker"
} | gzip -9 >"$temporary"
dump_cleanup_pending=0

if [[ ! -s "$temporary" ]]; then
  log "❌ pg_dump 生成了空文件"
  exit 1
fi
gzip -t "$temporary"

dump_tail="$(gzip -cd -- "$temporary" | tail -n 20)"
if ! grep -Fqx -- "$native_dump_marker" <<<"$dump_tail"; then
  log "❌ 备份缺少 pg_dump 原生完成标记"
  exit 1
fi
if [[ "$(tail -n 1 <<<"$dump_tail")" != "$backup_marker" ]]; then
  log "❌ 备份缺少本次运行的精确完成标记"
  exit 1
fi

archive_sha="$(sha256sum "$temporary")"
archive_sha="${archive_sha%% *}"
printf '%s  %s\n' "$archive_sha" "$archive_name" >"$checksum_temporary"

mv -- "$temporary" "$archive"
(cd "$BACKUP_DIR" && sha256sum -c -- "${checksum_temporary##*/}" >/dev/null)
mv -- "$checksum_temporary" "$checksum"
archive_size="$(du -h "$archive" | cut -f1)"
log "本地备份完整性校验通过: $archive_name ($archive_size, sha256=$archive_sha)"

if [[ "$upload_to_cos" -eq 1 ]]; then
  if [[ ! -x "$COSCMD_BIN" ]]; then
    log "❌ coscmd 不可执行: $COSCMD_BIN"
    exit 1
  fi

  remote_archive="$COS_REMOTE_DIR/$archive_name"
  remote_checksum="$COS_REMOTE_DIR/$checksum_name"
  log "开始上传到 cos://$COS_BUCKET/$COS_REMOTE_DIR/"
  timeout --signal=TERM --kill-after=30s "${COS_TRANSFER_TIMEOUT_SECONDS}s" \
    "$COSCMD_BIN" -b "$COS_BUCKET" upload "$archive" "$remote_archive"

  verification_dir="$(mktemp -d "$BACKUP_DIR/.cos-verify.XXXXXXXX")"
  downloaded_archive="$verification_dir/$archive_name"
  downloaded_checksum="$verification_dir/$checksum_name"
  timeout --signal=TERM --kill-after=30s "${COS_TRANSFER_TIMEOUT_SECONDS}s" \
    "$COSCMD_BIN" -b "$COS_BUCKET" download "$remote_archive" "$downloaded_archive"

  downloaded_sha="$(sha256sum "$downloaded_archive")"
  downloaded_sha="${downloaded_sha%% *}"
  if [[ "$downloaded_sha" != "$archive_sha" ]]; then
    log "❌ COS 回下载文件 SHA-256 不一致"
    exit 1
  fi

  timeout --signal=TERM --kill-after=30s "${COS_TRANSFER_TIMEOUT_SECONDS}s" \
    "$COSCMD_BIN" -b "$COS_BUCKET" upload "$checksum" "$remote_checksum"
  timeout --signal=TERM --kill-after=30s "${COS_TRANSFER_TIMEOUT_SECONDS}s" \
    "$COSCMD_BIN" -b "$COS_BUCKET" download "$remote_checksum" "$downloaded_checksum"

  if ! cmp -s -- "$checksum" "$downloaded_checksum"; then
    log "❌ COS 回下载校验文件内容不一致"
    exit 1
  fi
  (cd "$verification_dir" && sha256sum -c -- "$checksum_name" >/dev/null)
  log "✅ COS 上传与回下载校验通过: cos://$COS_BUCKET/$remote_archive"
else
  log "本次未指定 --upload，仅保留本地备份"
fi

retention_list="$(mktemp "$BACKUP_DIR/.retention.XXXXXXXX")"
find "$BACKUP_DIR" -maxdepth 1 -type f \
  -name 'quantdinger-*.sql.gz' -mtime "+$RETENTION_DAYS" -print0 \
  >"$retention_list"
while IFS= read -r -d '' old_archive; do
  rm -f -- "${old_archive}.sha256"
  rm -f -- "$old_archive"
done <"$retention_list"
rm -f -- "$retention_list"
retention_list=""

log "PostgreSQL 备份流程结束: $archive"
