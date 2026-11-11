#!/usr/bin/env python3
"""
Stock Radar 服务监控脚本
每小时检查一次，发现问题自动修复并通知
"""

import subprocess
import re
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ================= 🔴 配置（勿写死 Token；见仓库根目录 .env.example）=================
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 与主项目一致；亦可使用 RADAR_WATCHDOG_* 专用于本脚本。
TG_BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("RADAR_WATCHDOG_TELEGRAM_BOT_TOKEN")
    or ""
).strip()
TG_CHAT_ID = (
    os.environ.get("TELEGRAM_CHAT_ID")
    or os.environ.get("RADAR_WATCHDOG_TELEGRAM_CHAT_ID")
    or ""
).strip()
SERVICE_NAME = os.environ.get("RADAR_WATCHDOG_SERVICE_NAME", "stock-radar").strip()
CHECK_WINDOW_MINUTES = int(os.environ.get("RADAR_WATCHDOG_CHECK_WINDOW_MINUTES", "10"))

# ================= 📡 通知模块 =================
def send_telegram_alert(title: str, message: str) -> None:
    """发送 Telegram 警报"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[⚠️] 未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（或 RADAR_WATCHDOG_*），跳过 Telegram 通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": f"🚨 *{title}*\n\n{message}\n\n⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[✅] Telegram 通知已发送: {title}")
    except Exception as e:
        print(f"[❌] Telegram 发送失败: {e}")

# ================= 🔍 检查模块 =================
def get_service_status() -> str:
    """获取服务当前状态"""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', SERVICE_NAME],
            capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception as e:
        return f"error: {e}"

def get_recent_logs(minutes: int = 10) -> str:
    """获取最近 N 分钟的日志"""
    try:
        since = (datetime.now() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')
        result = subprocess.run(
            ['journalctl', '-u', SERVICE_NAME, '--since', since, '--no-pager'],
            capture_output=True, text=True
        )
        return result.stdout
    except Exception:
        return ""

def check_price_fetched(logs: str) -> bool:
    """检查日志中是否有成功获取股价的记录"""
    # 查找成功获取数据的标志，如 "现价:" 或 "非交易时段"（表示正常运行）
    success_patterns = [
        r'现价:\s*\d+',           # 成功获取价格
        r'非交易时段，静默中',      # 非交易时间正常
        r'✅ 已发送',              # 发送了警报
        r'⏳ 屏蔽中',              # 屏蔽期内（表示在运行）
    ]
    
    for pattern in success_patterns:
        if re.search(pattern, logs):
            return True
    return False

def check_errors(logs: str) -> list[str]:
    """检查日志中是否有错误（排除服务启动初期的错误）"""
    error_patterns = [
        r'运行异常',
        r'发送失败',
        r'获取行情失败',
    ]
    
    # 只关注特定业务错误，忽略启动时的依赖错误
    critical_errors: list[str] = []
    lines = logs.split('\n')
    
    for line in lines:
        # 检查是否是业务错误
        for pattern in error_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                # 找到时间戳，确认是最近的错误
                critical_errors.append(line.strip())
    
    return critical_errors

# ================= 🔧 修复模块 =================
def restart_service() -> bool:
    """重启服务"""
    try:
        subprocess.run(['sudo', 'systemctl', 'restart', SERVICE_NAME], check=True)
        print(f"[✅] 服务 {SERVICE_NAME} 已重启")
        return True
    except Exception as e:
        print(f"[❌] 重启服务失败: {e}")
        return False

def clear_cache() -> bool:
    """清除雷达 JSON 缓存（与 AlertCooldown 的 SQLite 无关）"""
    cache_file = str(Path(__file__).resolve().parent / "radar_cache.json")
    try:
        if os.path.exists(cache_file):
            os.remove(cache_file)
            print(f"[✅] 缓存文件已清除")
        return True
    except Exception as e:
        print(f"[⚠️] 清除缓存失败: {e}")
        return False

# ================= 🎯 主逻辑 =================
def main():
    print(f"\n{'='*50}")
    print(f"🔍 Stock Radar 监控检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")
    
    issues = []
    actions_taken = []
    
    # 1. 检查服务状态
    status = get_service_status()
    print(f"[1] 服务状态: {status}")
    
    if status != "active":
        issues.append(f"服务状态异常: {status}")
        print(f"    ⚠️ 服务未运行，尝试重启...")
        if restart_service():
            actions_taken.append("重启服务（服务未运行）")
        else:
            issues.append("重启服务失败")
    
    # 2. 获取最近日志
    logs = get_recent_logs(CHECK_WINDOW_MINUTES)
    print(f"[2] 最近 {CHECK_WINDOW_MINUTES} 分钟日志长度: {len(logs)} 字符")
    
    # 3. 检查是否有错误
    errors = check_errors(logs)
    if errors:
        error_summary = '\n'.join(errors[:3])  # 只显示前3个错误
        issues.append(f"检测到错误:\n```\n{error_summary}\n```")
        print(f"    ⚠️ 发现 {len(errors)} 个错误")
    else:
        print(f"    ✅ 未发现错误")
    
    # 4. 检查是否成功获取股价
    price_fetched = check_price_fetched(logs)
    
    # 如果在交易时间但没有获取到股价，说明有问题
    now = datetime.now()
    is_weekday = now.weekday() < 5
    current_time = now.time()
    morning_session = (9, 30) <= (current_time.hour, current_time.minute) <= (11, 30)
    afternoon_session = (13, 0) <= (current_time.hour, current_time.minute) <= (15, 0)
    is_trading_time = is_weekday and (morning_session or afternoon_session)
    
    if is_trading_time and not price_fetched and status == "active":
        issues.append(f"交易时间内连续 {CHECK_WINDOW_MINUTES} 分钟未获取到股价数据")
        print(f"    ⚠️ 交易时间内未检测到股价获取")
    elif not is_trading_time:
        print(f"    ℹ️ 当前非交易时间，跳过股价检查")
    else:
        print(f"    ✅ 数据获取正常")
    
    # 5. 如果有问题，尝试修复
    if issues:
        print(f"\n{'='*50}")
        print(f"🚨 发现 {len(issues)} 个问题，开始自动修复...")
        print(f"{'='*50}\n")
        
        # 若仅因服务未运行而已重启过一次，避免无意义地再次清缓存并重启
        only_inactive = (
            len(issues) == 1
            and issues[0].startswith("服务状态异常")
            and actions_taken
            and any("服务未运行" in a for a in actions_taken)
        )
        if not only_inactive:
            print("[修复] 清除缓存...")
            clear_cache()

            print("[修复] 重启服务...")
            if restart_service():
                actions_taken.append("重启服务（修复异常）")
        
        # 发送通知
        issues_text = '\n\n'.join([f"{i+1}. {issue}" for i, issue in enumerate(issues)])
        actions_text = '\n'.join([f"- {action}" for action in actions_taken]) if actions_taken else "自动修复失败"
        
        message = f"*检测到问题:*\n\n{issues_text}\n\n*已执行修复:*\n{actions_text}\n\n请检查服务状态确认是否恢复正常。"
        send_telegram_alert("Stock Radar 异常警报", message)
        
        return 1
    else:
        print(f"\n{'='*50}")
        print(f"✅ 所有检查通过，服务运行正常")
        print(f"{'='*50}\n")
        return 0

if __name__ == "__main__":
    sys.exit(main())
