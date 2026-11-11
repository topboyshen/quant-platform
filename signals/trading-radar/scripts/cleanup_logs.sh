#!/bin/bash
set -euo pipefail
umask 077

PROJECT_DIR="/home/ubuntu/trading-radar"
LOG_DIR="$PROJECT_DIR/logs"
UNCOMPRESSED_MAX_MINUTES=10080
COMPRESSED_MAX_MINUTES=43200

DRY_RUN=0
if [[ $# -gt 1 ]]; then
    echo "用法: $0 [--dry-run]" >&2
    exit 2
fi
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    *)
        echo "用法: $0 [--dry-run]" >&2
        exit 2
        ;;
esac

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "项目目录不存在: $PROJECT_DIR" >&2
    exit 1
fi

# Avoid overlapping cron/manual cleanup runs.  A skipped overlapping run is
# successful because the active process already owns the cleanup work.
LOCK_FILE="/tmp/trading-radar-cleanup-${UID}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date): 另一个日志清理任务正在运行，本次跳过"
    exit 0
fi

CANDIDATE_FILE=$(mktemp)
trap 'rm -f -- "$CANDIDATE_FILE"' EXIT
would_delete=0
deleted=0
failed=0

delete_log() {
    local kind="$1"
    local file="$2"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "将删除过期${kind}日志: $file"
        would_delete=$((would_delete + 1))
    elif rm -f -- "$file"; then
        echo "已删除过期${kind}日志: $file"
        deleted=$((deleted + 1))
    else
        echo "删除失败: $file" >&2
        failed=$((failed + 1))
    fi
}

process_candidates() {
    local dir="$1"
    local kind="$2"
    local max_minutes="$3"
    local compressed="$4"

    : > "$CANDIDATE_FILE"
    if [[ "$compressed" -eq 1 ]]; then
        if ! find "$dir" -maxdepth 1 -type f \
            \( -name '*.log-*.gz' -o -name '*.log.*.gz' \) \
            -mmin +"$max_minutes" -print0 > "$CANDIDATE_FILE"; then
            echo "扫描日志目录失败: $dir" >&2
            failed=$((failed + 1))
            return
        fi
    else
        if ! find "$dir" -maxdepth 1 -type f \
            \( -name '*.log-*' -o -name '*.log.*' \) \
            ! -name '*.gz' -mmin +"$max_minutes" -print0 > "$CANDIDATE_FILE"; then
            echo "扫描日志目录失败: $dir" >&2
            failed=$((failed + 1))
            return
        fi
    fi

    while IFS= read -r -d '' file; do
        delete_log "$kind" "$file"
    done < "$CANDIDATE_FILE"
}

# logrotate owns rotation/compression; this script only removes rotated files.
# It supports current .log-YYYYMMDD[.gz] and legacy .log.TIMESTAMP[.gz] names.
for dir in "$PROJECT_DIR" "$LOG_DIR"; do
    if [[ ! -d "$dir" ]]; then
        echo "日志目录不存在，跳过: $dir" >&2
        continue
    fi
    process_candidates "$dir" "未压缩" "$UNCOMPRESSED_MAX_MINUTES" 0
    process_candidates "$dir" "压缩" "$COMPRESSED_MAX_MINUTES" 1
done

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "$(date): 日志清理预检完成，将删除 $would_delete 个历史文件，扫描失败 $failed 个"
else
    echo "$(date): 日志清理完成，删除 $deleted 个历史文件，失败 $failed 个"
fi
[[ "$failed" -eq 0 ]]
