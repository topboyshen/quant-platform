#!/bin/bash
# 系统清理脚本
# 清理旧日志、缓存和临时文件

echo "🧹 开始系统清理..."

# 清理旧日志
echo "清理旧日志..."
find /home/ubuntu/trading_radar -name "*.log.*" -mtime +7 -delete 2>/dev/null
find /tmp/mx_output -mtime +3 -exec rm -rf {} + 2>/dev/null

# 清理 Python 缓存
echo "清理 Python __pycache__..."
find /home/ubuntu/trading_radar -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null

# 清理旧备份
echo "清理旧备份..."
find /home/ubuntu/trading_radar/backup_* -mtime +7 -exec rm -rf {} + 2>/dev/null

# 缩小 radar.log
echo "压缩日志..."
LOG_FILE="/home/ubuntu/trading_radar/radar.log"
if [ -f "$LOG_FILE" ] && [ $(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 10485760 ]; then
    echo "日志文件超过10MB，进行切割..."
    tail -n 1000 "$LOG_FILE" > "${LOG_FILE}.tmp"
    mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

echo "✅ 清理完成"
