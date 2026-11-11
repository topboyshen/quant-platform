#!/bin/bash
set -euo pipefail

# NO_BACKUP: owner policy includes manual and pre-release database copies.
# Installed at /home/ubuntu/scripts/backup_sqlite_to_cos.sh. No env override.
printf '%s\n' 'REFUSED: Quant NO_BACKUP policy; no archive, upload or retention executed.' >&2
exit 78

# ═══════════════════════════════════════════════════════
# Trading Radar SQLite → 腾讯云 COS 自动备份脚本
# 每天凌晨 2:30 执行 (crontab)
# ═══════════════════════════════════════════════════════

# ── 配置区 ────────────────────────────────────────────

# SQLite 数据库文件绝对路径（支持多个，空格分隔）
DB_PATHS=(
    "/home/ubuntu/trading-radar/battlefield_state.db"
    "/home/ubuntu/trading-radar/price_history.db"
    "/home/ubuntu/trading-radar/alert_accuracy.db"
    "/home/ubuntu/trading-radar/radar_cache_cooldown.db"
    "/home/ubuntu/trading-radar/data/swing_macro.db"
)

# COS 目标配置
COS_BUCKET="jiejiezi-backup-1259080705"
COS_REGION="ap-shanghai"
COS_REMOTE_DIR="backups/sqlite/trading-radar"

# 本地临时目录
TMP_DIR="/tmp/sqlite-backup"
RETENTION_DAYS=7

# coscmd 虚拟环境路径
COSCMD_VENV="/home/ubuntu/.coscmd-venv"

# 日志文件
LOG_FILE="/home/ubuntu/logs/backup_sqlite.log"

# ── 执行逻辑 ──────────────────────────────────────────

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HOSTNAME=$(hostname -s)
ARCHIVE_NAME="trading_radar_${HOSTNAME}_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="$TMP_DIR/$ARCHIVE_NAME"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 激活 coscmd
source "$COSCMD_VENV/bin/activate"

# 创建临时目录
mkdir -p "$TMP_DIR"

# 检查每个数据库是否存在
EXISTING_DBS=()
for db in "${DB_PATHS[@]}"; do
    if [[ -f "$db" ]]; then
        EXISTING_DBS+=("$db")
    else
        log "⚠️  数据库不存在，跳过: $db"
    fi
done

if [[ ${#EXISTING_DBS[@]} -eq 0 ]]; then
    log "❌ 没有可用的数据库文件，备份中止"
    exit 1
fi

# 打包压缩（保留相对路径结构）
log "开始打包 ${#EXISTING_DBS[@]} 个数据库文件..."
tar -czf "$ARCHIVE_PATH" -C / $(printf '%s ' "${EXISTING_DBS[@]}" | sed 's|^/||g')

ARCHIVE_SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
log "打包完成: $ARCHIVE_NAME ($ARCHIVE_SIZE)"

# 上传到 COS
log "开始上传到 cos://$COS_BUCKET/$COS_REMOTE_DIR/ ..."
if coscmd upload "$ARCHIVE_PATH" "$COS_REMOTE_DIR/$ARCHIVE_NAME"; then
    log "✅ 上传完成: cos://$COS_BUCKET/$COS_REMOTE_DIR/$ARCHIVE_NAME"
else
    log "❌ 上传失败，保留本地文件: $ARCHIVE_PATH"
    exit 1
fi

# 删除本地临时压缩包
rm -f "$ARCHIVE_PATH"
log "本地临时文件已清理"

# 清理本地旧备份
if [[ "$RETENTION_DAYS" -gt 0 ]]; then
    DELETED=$(find "$TMP_DIR" -name "trading_radar_*.tar.gz" -mtime +$RETENTION_DAYS -print -delete 2>/dev/null | wc -l)
    [[ "$DELETED" -gt 0 ]] && log "清理了 $DELETED 个过期本地备份"
fi

# 清理旧日志（保留30天）
find "$(dirname "$LOG_FILE")" -name "$(basename "$LOG_FILE")*" -mtime +30 -delete 2>/dev/null || true

log "备份流程结束"
