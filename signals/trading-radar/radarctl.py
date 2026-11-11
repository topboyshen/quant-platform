#!/usr/bin/env python3
"""
Radar CLI — trading_radar 统一入口工具

替代根目录下散落的多个入口脚本，提供单一命令行界面：

    radarctl monitor          启动监控服务（radar_monitor_v3.py）
    radarctl dashboard        启动 Dashboard（原 dashboard/app.py）
    radarctl train            训练 ML 模型（原 ml_training.py）
    radarctl train-rl         训练 RL 模型（原 train_base_models.py）
    radarctl backtest         运行回测（原 backtest_pro.py）
    radarctl benchmark        运行基准测试（原 run_benchmark.py）
    radarctl serve-ml         启动 ML 推理服务（原 serve_ml.py）
    radarctl status           查看系统状态
    radarctl logs             查看最新日志
    radarctl restart          重启 systemd 服务
    radarctl test-alert       发送测试告警
    radarctl backup           备份配置和数据
    radarctl health           健康检查报告

安装:
    ln -s $(pwd)/radarctl.py /usr/local/bin/radarctl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# 项目根目录
BASE_DIR = Path(__file__).parent.resolve()
VENV_PYTHON = str(BASE_DIR / "venv" / "bin" / "python")

# 原入口脚本 → 新命令映射
ENTRY_SCRIPTS = {
    "monitor": "radar_monitor_v3.py",
    "train": "ml_training.py",
    "train-rl": "train_base_models.py",
    "train-rl-external": "train_external_models.py",
    "train-rl-recovery": "train_recovery_20dim.py",
    "retrain-rl": "retrain_base_models.py",
    "backtest": "backtest_pro.py",
    "backtest-compare": "run_backtest_comparison.py",
    "benchmark": "run_benchmark.py",
    "serve-ml": "serve_ml.py",
    "monitor-script": "monitor.py",  # 服务监控脚本
}


def _run_script(script_name: str, args: list[str]) -> int:
    """运行项目根目录下的 Python 脚本。"""
    script_path = BASE_DIR / script_name
    if not script_path.exists():
        print(f"❌ 脚本不存在: {script_path}", file=sys.stderr)
        return 1
    cmd = [VENV_PYTHON, str(script_path)] + args
    return subprocess.call(cmd)


def _run_dashboard(args: list[str]) -> int:
    """启动 Flask Dashboard。"""
    app_path = BASE_DIR / "dashboard" / "app.py"
    if not app_path.exists():
        print("❌ Dashboard 未安装", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["FLASK_APP"] = str(app_path)
    port = args[0] if args else "8081"
    cmd = [VENV_PYTHON, "-m", "flask", "run", "--host=0.0.0.0", f"--port={port}"]
    return subprocess.call(cmd, cwd=str(BASE_DIR / "dashboard"), env=env)


def _load_config() -> dict:
    config_path = BASE_DIR / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _service_status(service: str = "stock-radar") -> str:
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def _health_check() -> dict:
    try:
        import requests
        resp = requests.get("http://localhost:8080/health", timeout=2)
        return resp.json()
    except Exception:
        return {}


def cmd_status(args: argparse.Namespace) -> int:
    """显示系统状态。"""
    config = _load_config()
    targets = config.get("targets", [])

    print("=" * 60)
    print("📊 Radar 系统状态报告")
    print("=" * 60)

    # 服务状态
    for svc in ["stock-radar", "stock-radar-dashboard"]:
        status = _service_status(svc)
        emoji = "✅" if status == "active" else "❌"
        print(f"\n{emoji} {svc}: {status}")

    # 监控目标
    print(f"\n📈 监控股票: {len(targets)} 只")
    for t in targets[:5]:
        print(f"   • {t['name']} ({t['code']}) - 触发价: {t['trigger_price']}")
    if len(targets) > 5:
        print(f"   ... 共 {len(targets)} 只")

    # 健康检查
    health = _health_check()
    if health:
        print("\n🏥 健康检查: http://localhost:8080/health")
        print(f"   状态: {health.get('status', 'unknown')}")
        print(f"   扫描次数: {health.get('scan_count', 0)}")
        print(f"   错误次数: {health.get('error_count', 0)}")
        uptime = health.get("uptime_seconds", 0)
        print(f"   运行时间: {uptime/3600:.1f} 小时")

    print("\n" + "=" * 60)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """查看最新日志。"""
    lines = args.lines if hasattr(args, "lines") else 20
    log_file = BASE_DIR / "radar.log"
    if not log_file.exists():
        print("📋 日志文件不存在")
        return 1
    result = subprocess.run(
        ["tail", "-n", str(lines), str(log_file)],
        capture_output=True, text=True
    )
    print(f"📋 最近 {lines} 行日志:\n")
    print(result.stdout)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    """重启服务。"""
    print("🔄 正在重启服务...")
    for svc in ["stock-radar", "stock-radar-dashboard"]:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", svc],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ {svc} 已重启")
        else:
            print(f"❌ {svc} 重启失败: {result.stderr}")
    time.sleep(2)
    return cmd_status(args)


def cmd_test_alert(args: argparse.Namespace) -> int:
    """发送测试告警。"""
    config = _load_config()
    tg = config.get("telegram", {})
    token = tg.get("bot_token")
    chat_id = tg.get("chat_id")
    if not token or not chat_id:
        print("❌ Telegram 未配置")
        return 1
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        message = (
            f"🧪 *Radar 系统测试*\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"测试消息发送成功 ✅"
        )
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        print("✅ 测试告警已发送" if resp.status_code == 200 else f"❌ 发送失败: {resp.status_code}")
        return 0 if resp.status_code == 200 else 1
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return 1


def cmd_backup(args: argparse.Namespace) -> int:
    """备份配置和数据。"""
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"radar_backup_{timestamp}.tar.gz"
    files = ["config.json", "portfolio.json", "radar_cache_cooldown.db", "price_history.db"]
    cmd = ["tar", "-czf", str(backup_file), "-C", str(BASE_DIR)]
    for f in files:
        if (BASE_DIR / f).exists():
            cmd.append(f)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✅ 备份完成: {backup_file}")
        return 0
    print(f"❌ 备份失败: {result.stderr}")
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    """运行原入口脚本。"""
    command = args.command
    if command in ENTRY_SCRIPTS:
        return _run_script(ENTRY_SCRIPTS[command], args.remainder)
    elif command == "dashboard":
        return _run_dashboard(args.remainder)
    else:
        print(f"❌ 未知命令: {command}", file=sys.stderr)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]

    # 内置命令（不依赖外部脚本）
    builtin_commands = {
        "status": cmd_status,
        "logs": cmd_logs,
        "restart": cmd_restart,
        "test-alert": cmd_test_alert,
        "backup": cmd_backup,
        "health": cmd_status,
    }

    # 外部脚本命令
    script_commands = list(ENTRY_SCRIPTS.keys()) + ["dashboard"]

    if not argv or argv[0] in ("--help", "-h", "help"):
        print("Radar CLI — trading_radar 统一入口工具")
        print("")
        print("用法: radarctl <command> [args...]")
        print("")
        print("内置命令:")
        for cmd, desc in [
            ("status", "查看系统状态"),
            ("logs [-n 20]", "查看最新日志"),
            ("restart", "重启 systemd 服务"),
            ("test-alert", "发送测试告警"),
            ("backup", "备份配置和数据"),
            ("health", "健康检查报告"),
        ]:
            print(f"  {cmd:20s} {desc}")
        print("")
        print("运行命令:")
        for cmd, desc in [
            ("monitor", "启动监控服务"),
            ("dashboard [port]", "启动 Dashboard"),
            ("train", "训练 ML 模型"),
            ("train-rl", "训练 RL 模型（15-dim）"),
            ("train-rl-external", "训练 RL 外部特征模型"),
            ("train-rl-recovery", "恢复训练 20-dim 模型"),
            ("retrain-rl", "重新训练基础 RL 模型"),
            ("backtest", "运行回测"),
            ("backtest-compare", "三向回测对比"),
            ("benchmark", "运行基准测试"),
            ("serve-ml", "启动 ML 推理服务"),
            ("monitor-script", "运行服务监控脚本"),
        ]:
            print(f"  {cmd:20s} {desc}")
        print("")
        print(f"项目目录: {BASE_DIR}")
        return 0

    command = argv[0]

    if command in builtin_commands:
        # 为内置命令构建简单参数解析
        parser = argparse.ArgumentParser(prog=f"radarctl {command}")
        if command == "logs":
            parser.add_argument("-n", "--lines", type=int, default=20, help="行数")
        args = parser.parse_args(argv[1:])
        args.remainder = []
        return builtin_commands[command](args)

    if command in script_commands:
        args = argparse.Namespace(command=command, remainder=argv[1:])
        return cmd_run(args)

    print(f"❌ 未知命令: {command}")
    print("\n可用命令:")
    all_cmds = sorted(set(list(builtin_commands.keys()) + script_commands))
    for cmd in all_cmds:
        print(f"  {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
