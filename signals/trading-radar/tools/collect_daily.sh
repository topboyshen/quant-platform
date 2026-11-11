#!/bin/bash
# 每日数据收集脚本
# 收盘后自动运行，收集监控股票的资金流向和财务数据

set -e

LOG_FILE="/home/ubuntu/trading_radar/logs/daily_collect.log"
VENV_PYTHON="/home/ubuntu/trading_radar/venv/bin/python"
TOOLS_DIR="/home/ubuntu/trading_radar/tools"
mkdir -p "$(dirname "$LOG_FILE")"

echo "===== 每日数据收集开始 $(date) =====" >> "$LOG_FILE"

# 股票列表
STOCKS=(
    "东方财富"
    "同花顺"
    "蓝色光标"
    "平安银行"
)

# 循环查询每只股票
for stock in "${STOCKS[@]}"; do
    echo "[$stock] 查询资金流向..." >> "$LOG_FILE"
    $VENV_PYTHON "$TOOLS_DIR/mx_query.py" "$stock资金流向" --no-telegram >> "$LOG_FILE" 2>&1 || echo "查询失败: $stock" >> "$LOG_FILE"
    sleep 2
done

echo "===== 每日数据收集完成 $(date) =====" >> "$LOG_FILE"
