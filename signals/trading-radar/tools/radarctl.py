#!/usr/bin/env python3
"""
Radar 系统管理控制台
一站式管理 trading_radar 系统的各种功能

用法:
    ./tools/radarctl.py status          # 查看系统状态
    ./tools/radarctl.py restart         # 重启服务
    ./tools/radarctl.py logs            # 查看最新日志
    ./tools/radarctl.py test-alert      # 发送测试告警
    ./tools/radarctl.py query "xxx"     # 查询金融数据
    ./tools/radarctl.py backup          # 备份配置和数据
    ./tools/radarctl.py health          # 健康检查报告
"""

import os
import sys
import json
import time
import subprocess
import requests
from datetime import datetime
from pathlib import Path

# 配置
BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
VENV_PYTHON = str(BASE_DIR / "venv" / "bin" / "python")
SERVICE_NAME = "stock-radar"

def load_config() -> dict:
    """加载配置"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def run_systemctl(action: str) -> tuple[bool, str, str]:
    """运行 systemctl 命令"""
    result = subprocess.run(
        ['sudo', 'systemctl', action, SERVICE_NAME],
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stdout, result.stderr

def get_service_status() -> str:
    """获取服务状态"""
    result = subprocess.run(
        ['systemctl', 'is-active', SERVICE_NAME],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

def get_process_info() -> dict | None:
    """获取进程信息"""
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True
        )
        for line in result.stdout.split('\n'):
            if 'grep' in line:
                continue
            if not any(token in line for token in (
                'radar_monitor_v3.py', 'radar_monitor_v2.py', 'radar_monitor.py',
            )):
                continue
            parts = line.split()
            if len(parts) >= 6:
                return {
                    'pid': parts[1],
                    'cpu': parts[2],
                    'mem': parts[3],
                    'rss': int(parts[5]) // 1024,  # MB
                    'uptime': ' '.join(parts[9:11]) if len(parts) > 10 else 'unknown'
                }
    except (OSError, IndexError, ValueError):
        pass
    return None

def get_disk_usage() -> dict | None:
    """获取磁盘使用情况"""
    result = subprocess.run(
        ['df', '-h', str(BASE_DIR)],
        capture_output=True,
        text=True
    )
    lines = result.stdout.strip().split('\n')
    if len(lines) > 1:
        parts = lines[1].split()
        return {
            'total': parts[1],
            'used': parts[2],
            'available': parts[3],
            'percent': parts[4]
        }
    return None

def get_recent_logs(lines: int = 20) -> list[str]:
    """获取最近日志"""
    log_file = BASE_DIR / "radar.log"
    if not log_file.exists():
        return ["日志文件不存在"]
    
    result = subprocess.run(
        ['tail', '-n', str(lines), str(log_file)],
        capture_output=True,
        text=True
    )
    return result.stdout.strip().split('\n')

def send_telegram_test(config: dict) -> tuple[bool, str]:
    """发送 Telegram 测试消息"""
    tg = config.get('telegram', {})
    token = tg.get('bot_token')
    chat_id = tg.get('chat_id')
    
    if not token or not chat_id:
        return False, "未配置 Telegram"
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        message = (
            f"🧪 *Radar 系统测试*\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"服务状态: {get_service_status()}\n"
            f"测试消息发送成功 ✅"
        )
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        return resp.status_code == 200, "已发送"
    except Exception as e:
        return False, str(e)

def backup_data() -> tuple[bool, str]:
    """备份数据"""
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = backup_dir / f"radar_backup_{timestamp}.tar.gz"
    
    # 备份配置文件和重要数据
    files_to_backup = [
        "config.json",
        "portfolio.json",
        "radar_cache_cooldown.db",
        "price_history.db"
    ]
    
    cmd = ['tar', '-czf', str(backup_file), '-C', str(BASE_DIR)]
    for f in files_to_backup:
        if (BASE_DIR / f).exists():
            cmd.append(f)
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        return True, str(backup_file)
    return False, result.stderr

def show_status() -> None:
    """显示系统状态"""
    config = load_config()
    targets = config.get('targets', [])
    
    print("=" * 60)
    print("📊 Radar 系统状态报告")
    print("=" * 60)
    
    # 服务状态
    status = get_service_status()
    status_emoji = "✅" if status == "active" else "❌"
    print(f"\n{status_emoji} 服务状态: {status}")
    
    # 进程信息
    proc = get_process_info()
    if proc:
        print(f"   PID: {proc['pid']}")
        print(f"   CPU: {proc['cpu']}% | 内存: {proc['mem']}% ({proc['rss']}MB)")
    
    # 监控目标
    print(f"\n📈 监控股票: {len(targets)} 只")
    for t in targets:
        print(f"   • {t['name']} ({t['code']}) - 触发价: {t['trigger_price']}")
    
    # 磁盘使用
    disk = get_disk_usage()
    if disk:
        print(f"\n💾 磁盘使用: {disk['used']} / {disk['total']} ({disk['percent']})")
    
    # 健康检查端点
    try:
        resp = requests.get("http://localhost:8080/health", timeout=2)
        health = resp.json()
        print(f"\n🏥 健康检查: http://localhost:8080/health")
        print(f"   状态: {health.get('status', 'unknown')}")
        print(f"   扫描次数: {health.get('scan_count', 0)}")
        print(f"   错误次数: {health.get('error_count', 0)}")
        print(f"   运行时间: {health.get('uptime_seconds', 0)/3600:.1f} 小时")
    except (OSError, ConnectionError, ValueError):
        print(f"\n🏥 健康检查: 无法连接")
    
    print("\n" + "=" * 60)

def show_logs(lines=20):
    """显示日志"""
    print(f"📋 最近 {lines} 行日志:\n")
    for line in get_recent_logs(lines):
        print(line)

def restart_service():
    """重启服务"""
    print("🔄 正在重启服务...")
    success, stdout, stderr = run_systemctl("restart")
    if success:
        time.sleep(2)
        status = get_service_status()
        if status == "active":
            print("✅ 服务已重启并运行")
        else:
            print(f"⚠️ 服务状态: {status}")
    else:
        print(f"❌ 重启失败: {stderr}")

def show_help():
    """显示帮助"""
    print(__doc__)

def main():
    if len(sys.argv) < 2:
        show_help()
        return
    
    cmd = sys.argv[1]
    
    if cmd == "status":
        show_status()
    
    elif cmd == "logs":
        lines = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_logs(lines)
    
    elif cmd == "restart":
        restart_service()
    
    elif cmd == "test-alert":
        print("🧪 发送测试告警...")
        config = load_config()
        success, msg = send_telegram_test(config)
        print(f"{'✅' if success else '❌'} {msg}")
    
    elif cmd == "query":
        if len(sys.argv) < 3:
            print("用法: radarctl.py query \"股票查询内容\"")
            return
        query = sys.argv[2]
        mx_script = BASE_DIR / "tools" / "mx_query.py"
        os.system(f"{VENV_PYTHON} {mx_script} \"{query}\"")
    
    elif cmd == "backup":
        print("💾 正在备份...")
        success, msg = backup_data()
        print(f"{'✅' if success else '❌'} {msg}")
    
    elif cmd == "health":
        show_status()
    
    else:
        print(f"未知命令: {cmd}")
        show_help()

if __name__ == "__main__":
    main()
