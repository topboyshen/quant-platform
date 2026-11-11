"""V3 入口启动烟雾测试。

目的：拦下"配置 → 启动 → /health"链路上最常见的 P0：
    1. health server 没起来（端口未监听） —— 2026-05 出过事故
    2. monitored_stocks == 0 或 != len(targets)（add_target 链路坏）
    3. 配置 schema 失败 / 子进程立即退出

跑法（默认 ~30~90s，看 ML/torch 冷启动）::

    pytest tests/test_v3_boot.py -v

不依赖外部网络（telegram 启动通知用占位 token，被 Telegram 拒绝后
TelegramNotifier.send 内部 try/except 会吞掉，不阻塞 health server）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def _drain(proc: subprocess.Popen) -> str:
    if proc.stdout is None:
        return ""
    try:
        data = proc.stdout.read()
    except Exception:
        return ""
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


@pytest.fixture
def smoke_config(tmp_path: Path) -> tuple[Path, dict, int]:
    """生成临时 config.json：覆盖 health.port、log/cache 隔离、telegram 用占位。"""
    src = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    # The tracked config intentionally has no targets: production keeps them in
    # the untracked config.local.json overlay. The boot test supplies its own.
    src["targets"] = [
        {"code": "600000", "name": "boot-fixture-1", "trigger_price": 10.0,
         "direction": "below", "ml_enabled": False, "rl_enabled": False},
        {"code": "000001", "name": "boot-fixture-2", "trigger_price": 11.0,
         "direction": "above", "ml_enabled": False, "rl_enabled": False},
        {"code": "600519", "name": "boot-fixture-3", "trigger_price": 1500.0,
         "direction": "below", "ml_enabled": False, "rl_enabled": False},
    ]

    free = _find_free_port()
    src.setdefault("health", {})["host"] = "127.0.0.1"
    src["health"]["port"] = free

    # token 必须 > 20 字符且不含 "REPLACE" 才能通过 TelegramNotifier 校验；
    # 真实发送会被 Telegram 拒（400/exception），TelegramNotifier.send
    # 内部 try/except 会捕获且只记 warning —— 不阻塞 health server / 主流程。
    src.setdefault("telegram", {})["bot_token"] = (
        "smoke_test_fake_bot_token_for_v3_boot_only"
    )
    src["telegram"]["chat_id"] = "0"

    # 隔离日志/缓存到 tmp，避免污染生产文件
    src.setdefault("radar", {})["log_file"] = str(tmp_path / "radar.log")
    src["radar"]["cache_file"] = str(tmp_path / "radar_cache.json")

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    return cfg, src, free


def test_v3_boot_health_and_targets(tmp_path: Path, smoke_config) -> None:
    """启动 V3 子进程 → /health 200 → monitored_stocks == len(targets) 且端口监听"""
    cfg_path, cfg, port = smoke_config
    expected_targets = len(cfg.get("targets", []))
    assert expected_targets > 0, (
        "config.json 没有 targets，无法验证 monitored_stocks 关联性"
    )

    env = os.environ.copy()
    # 隔离持久化文件（tests/conftest 只覆盖 BATTLEFIELD_STATE_DB 用于 pytest 进程，
    # 子进程要自己设环境变量）
    env["BATTLEFIELD_STATE_DB"] = str(tmp_path / "battlefield_state.db")
    env["RADAR_COOLDOWN_DB"] = str(tmp_path / "radar_cooldown.db")
    # 使用独立锁目录，避免与运行中的服务冲突
    env["RADAR_LOCK_DIR"] = str(tmp_path / "locks")
    # config.telegram.* 也通过环境变量传递，便于 TelegramNotifier 优先读取
    env["TELEGRAM_BOT_TOKEN"] = cfg["telegram"]["bot_token"]
    env["TELEGRAM_CHAT_ID"] = cfg["telegram"]["chat_id"]

    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "radar_monitor_v3.py"),
            "--config",
            str(cfg_path),
        ],
        cwd=str(ROOT),  # 项目根：让 ml/rl 默认相对路径（models/）能找到
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 90  # ML/torch 冷启动可能 30~60s
    health_url = f"http://127.0.0.1:{port}/health"
    last_payload: dict = {}
    last_err: Exception | None = None

    try:
        # 1) 等待端口监听
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"radar_monitor_v3 子进程提前退出 "
                    f"(rc={proc.returncode}) before port {port} listened\n"
                    f"--- subprocess output ---\n{_drain(proc)}"
                )
            if _port_listening(port):
                break
            time.sleep(0.5)
        else:
            pytest.fail(
                f"超时：health 端口 {port} 一直没有 listen\n"
                f"--- subprocess output ---\n{_drain(proc)}"
            )

        # 2) 等 /health 返回 monitored_stocks == len(targets)
        # add_target 在 main() 中循环调用，每次都更新 stock_count。
        # 若 main 顺序错乱（比如未调 add_target / 未连到 health monitor），此处会超时。
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"radar_monitor_v3 子进程提前退出 "
                    f"(rc={proc.returncode}) during /health polling\n"
                    f"--- subprocess output ---\n{_drain(proc)}"
                )
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    body = resp.read().decode("utf-8")
                    last_payload = json.loads(body)
                    if last_payload.get("monitored_stocks") == expected_targets:
                        break
            except (OSError, ValueError) as exc:
                last_err = exc
            time.sleep(0.5)
        else:
            pytest.fail(
                f"超时：/health monitored_stocks 未达到 {expected_targets}；"
                f"最后载荷={last_payload}, 最后错误={last_err}"
            )

        # 3) 显式断言（即使上面 break 出来后也再确认一遍，便于看错误现场）
        assert _port_listening(port), f"端口 {port} 不再监听"
        assert last_payload.get("monitored_stocks") == expected_targets, (
            f"monitored_stocks={last_payload.get('monitored_stocks')} "
            f"!= len(targets)={expected_targets}; payload={last_payload}"
        )
        # /health 返回结构契约
        assert "status" in last_payload, f"/health 缺 status 字段: {last_payload}"
        assert last_payload["status"] in {"healthy", "starting", "degraded", "closed"}, (
            f"/health status 异常: {last_payload['status']}"
        )

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
