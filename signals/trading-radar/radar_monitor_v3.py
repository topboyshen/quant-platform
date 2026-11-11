#!/usr/bin/env python3
"""
监控核心服务入口 V3 - 终极优化版
职责：实时监控、告警、通知
特点：
- 数据源管理器 V3（智能降级+缓存）
- 策略引擎 V3（异步执行+懒加载）
- 告警管理器 V3（去重+限流+分级）
- 扫描调度器 V3（自适应间隔）
"""

import argparse
import json
import logging
import os
import sys

from radar.config_schema import ConfigSchemaError
from radar.core.monitor_engine_v2 import MonitorConfig, TargetStock
from radar.core.monitor_engine_v3 import MonitorEngineV3
from radar.observability import build_log_formatter, configure_tracing, install_signal_handlers
from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError


def _read_config_file(config_path: str) -> dict:
    from radar.config_loader import load_config

    return load_config(config_path, validate_monitor=False)


# 与 radar.secrets.merge_env_into_config_data 中识别的环境变量保持一致 ——
# systemd 给任何一个塞了带 \n 的值，os.getenv 都会读到尾部换行符，进而让
# Telegram bot_token 或 LLM API key 携带非法字符触发 401/400。
_SYSTEMD_TRIM_VARS = (
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHAT_ID',
    'LLM_API_KEY',
    'LLM_BASE_URL',
    'LLM_MODEL',
)


def _fix_systemd_env():
    """systemd 255 的 Environment 值末尾会带换行符，导致 os.getenv 读取异常。"""
    try:
        with open('/proc/self/environ', 'rb') as f:
            raw = f.read()
        env_pairs = raw.split(b'\x00')
        fixed = []
        for e in env_pairs:
            if not e:
                continue
            try:
                key, val = e.decode('utf-8', errors='replace').split('=', 1)
                if key in _SYSTEMD_TRIM_VARS:
                    old = os.environ.get(key, '')
                    os.environ[key] = val.strip()
                    if len(old) != len(os.environ[key]):
                        fixed.append(f'{key}: {len(old)}->{len(os.environ[key])}')
            except ValueError:
                continue
        if fixed:
            print(f'[_fix_systemd_env] 修复环境变量: {", ".join(fixed)}', file=sys.stderr)
    except Exception as ex:
        print(f'[_fix_systemd_env] 异常: {ex}', file=sys.stderr)


def _read_validated_config(config_path: str) -> dict:
    data = _read_config_file(config_path)
    from radar.config_schema import validate_monitor_config_dict
    validate_monitor_config_dict(data)
    return data

def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    """配置日志"""
    logger = logging.getLogger('radar_monitor')
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = build_log_formatter()

    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def config_from_dict(data: dict) -> MonitorConfig:
    """由已解析的 config JSON 构建 MonitorConfig"""
    health_port = int(os.getenv('HEALTH_PORT', data.get('health', {}).get('port', 8080)))
    health_host = os.getenv('HEALTH_HOST', data.get('health', {}).get('host', '0.0.0.0'))
    return MonitorConfig(
        bot_token=data['telegram']['bot_token'],
        chat_id=str(data['telegram']['chat_id']),
        cooldown_minutes=data['radar']['cooldown_minutes'],
        scan_interval=data['radar']['scan_interval_seconds'],
        cache_file=data['radar']['cache_file'],
        log_file=data['radar']['log_file'],
        market_hours=data['market'],
        ml_threshold=data.get('alerts', {}).get('ml_prediction', {}).get('probability_threshold', 0.7),
        ml_min_history_bars=data.get('alerts', {}).get('ml_prediction', {}).get('min_history_bars', 30),
        ml_enabled=data.get('alerts', {}).get('ml_prediction', {}).get('enabled', True),
        rl_enabled=data.get('alerts', {}).get('rl_strategy', {}).get('enabled', True),
        ml_model_dir=data.get('ml_model_dir', 'models'),
        rl_model_dir=data.get('rl_model_dir', 'rl_models'),
        rl_mode=data.get('rl_mode', 'confirm'),
        initial_cash=data.get('initial_cash', 100000.0),
        health_host=health_host,
        health_port=health_port,
        batch_alerts=data.get('alerts', {}).get('batch_alerts', True),
        alert_priorities=data.get('alerts', {}).get('priorities', None),
        data_sources=data.get('data_sources', None)
    )


def targets_from_dict(data: dict) -> list:
    """由已解析的 config JSON 构建目标列表"""
    alerts_cfg = data.get('alerts', {}) if isinstance(data.get('alerts', {}), dict) else {}
    global_ml_enabled = alerts_cfg.get('ml_prediction', {}).get('enabled', True)
    global_rl_enabled = alerts_cfg.get('rl_strategy', {}).get('enabled', True)

    targets = []
    for t in data['targets']:
        targets.append(TargetStock(
            code=t['code'],
            name=t['name'],
            trigger_price=float(t['trigger_price']),
            direction=t.get('direction', 'below'),
            ml_enabled=bool(global_ml_enabled and t.get('ml_enabled', True)),
            rl_enabled=bool(global_rl_enabled and t.get('rl_enabled', global_rl_enabled)),
            strategy_tag=str(t.get('strategy_tag', '') or ''),
            market_cap=float(t.get('market_cap', 0) or 0),
            tags=list(t.get('tags', []) or []),
        ))
    return targets


def load_monitor_bundle(config_path: str = "config.json"):
    """一次读盘，返回 (MonitorConfig, targets)。"""
    data = _read_validated_config(config_path)
    return config_from_dict(data), targets_from_dict(data)


def main():
    # 在最早阶段修复 systemd 环境变量换行符问题
    _fix_systemd_env()

    parser = argparse.ArgumentParser(description='Stock Radar Monitor V3 - 终极优化版')
    parser.add_argument('--config', type=str, default='config.json', help='配置文件路径')
    parser.add_argument('--daemon', action='store_true', help='后台模式运行')
    args = parser.parse_args()

    try:
        config, targets = load_monitor_bundle(args.config)

        _lock = SingleInstanceLock("radar-monitor-v3").acquire()

        # 设置日志
        logger = setup_logging(config.log_file, logging.INFO)

        logger.info(">>> Stock Radar Monitor V3 启动 <<<")
        logger.info(f"监控目标: {len(targets)} 只股票")
        logger.info("扫描间隔: 自适应")
        logger.info(f"冷却时间: {config.cooldown_minutes}分钟")
        configure_tracing("trading-radar-monitor", logger)

        # 软校验：占位符 / 缺省字段 / 占位 LLM 凭据，告警但不退出
        try:
            from radar.config_schema import log_optional_field_audit

            with open(args.config, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            merged = _read_config_file(args.config)
            log_optional_field_audit(merged, logger, raw_data=raw)
        except Exception as audit_err:
            logger.debug(f"配置软校验跳过: {audit_err}")

        # 创建引擎（V3）
        engine = MonitorEngineV3(config, logger)
        install_signal_handlers(engine, logger)

        # 添加监控目标
        for target in targets:
            engine.add_target(target)

        # 启动监控
        engine.run()

    except KeyboardInterrupt:
        print("\n监控已停止")
    except SingleInstanceLockError as e:
        print(f"监控已在运行，拒绝重复启动: {e}")
        sys.exit(2)
    except ConfigSchemaError as e:
        print(f"配置校验失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
