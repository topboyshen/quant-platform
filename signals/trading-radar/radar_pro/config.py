"""
Radar Pro — 配置加载与数据模型。

从 radar_pro.py 拆分出来的配置模块，包含 TargetStock / RadarConfig 数据类
和 load_config / load_portfolio / setup_logging 工具函数。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    """配置日志：文件 + 控制台双输出。"""
    logger = logging.getLogger("radar")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


@dataclass
class TargetStock:
    code: str
    name: str
    trigger_price: float
    direction: str
    ml_enabled: bool = True


@dataclass
class RadarConfig:
    bot_token: str
    chat_id: str
    cooldown_minutes: int
    scan_interval: int
    cache_file: str
    log_file: str
    data_history_file: str
    targets: List[TargetStock]
    market_hours: Dict
    alerts: Dict
    backtest: Dict


def load_config(config_path: str = "config.json") -> RadarConfig:
    """从 JSON 文件加载雷达配置。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件格式错误: {e}")

    from radar.secrets import merge_env_into_config_data

    data = merge_env_into_config_data(data)

    targets = [
        TargetStock(
            code=t["code"],
            name=t["name"],
            trigger_price=float(t["trigger_price"]),
            direction=t.get("direction", "below"),
            ml_enabled=t.get("ml_enabled", True),
        )
        for t in data["targets"]
    ]

    return RadarConfig(
        bot_token=data["telegram"]["bot_token"],
        chat_id=str(data["telegram"]["chat_id"]),
        cooldown_minutes=data["radar"]["cooldown_minutes"],
        scan_interval=data["radar"]["scan_interval_seconds"],
        cache_file=data["radar"]["cache_file"],
        log_file=data["radar"]["log_file"],
        data_history_file=data["radar"]["data_history_file"],
        targets=targets,
        market_hours=data["market"],
        alerts=data.get("alerts", {}),
        backtest=data.get("backtest", {}),
    )


def load_portfolio(portfolio_path: str = "portfolio.json") -> Dict:
    """加载真实持仓配置。"""
    if not os.path.exists(portfolio_path):
        return {"positions": []}
    try:
        with open(portfolio_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"positions": []}
