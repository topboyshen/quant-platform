#!/usr/bin/env python3
"""
监控核心服务入口 V2 - 增强版
职责：实时监控、告警、通知
特点：使用 V2 数据层和引擎，性能提升 3-5x
"""

import json
import logging
import os
import sys
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from radar.config_schema import ConfigSchemaError
from radar.core.monitor_engine_v2 import MonitorEngine, MonitorConfig, TargetStock


def _read_config_file(config_path: str) -> dict:
    with open(config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    from radar.secrets import merge_env_into_config_data
    return merge_env_into_config_data(data)


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
                if key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
                    old = os.environ.get(key, '')
                    os.environ[key] = val.strip()
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


class _JsonLogFormatter(logging.Formatter):
    """单行 JSON，便于 Loki / ELK 等采集"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "func": record.funcName,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    """配置日志"""
    logger = logging.getLogger('radar_monitor')
    logger.setLevel(level)
    
    if logger.handlers:
        return logger
    
    use_json = os.getenv("RADAR_LOG_JSON", "").strip() in ("1", "true", "yes")
    if use_json:
        formatter: logging.Formatter = _JsonLogFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
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
    targets = []
    for t in data['targets']:
        targets.append(TargetStock(
            code=t['code'],
            name=t['name'],
            trigger_price=float(t['trigger_price']),
            direction=t.get('direction', 'below'),
            ml_enabled=t.get('ml_enabled', True),
            rl_enabled=t.get('rl_enabled', True),
            strategy_tag=str(t.get('strategy_tag', '') or ''),
        ))
    return targets


def load_monitor_bundle(config_path: str = "config.json"):
    """一次读盘，返回 (MonitorConfig, targets)。"""
    data = _read_validated_config(config_path)
    return config_from_dict(data), targets_from_dict(data)


def load_config(config_path: str = "config.json") -> MonitorConfig:
    """加载配置并转换为 MonitorConfig"""
    return config_from_dict(_read_validated_config(config_path))


def load_targets(config_path: str = "config.json") -> list:
    """加载监控目标列表"""
    return targets_from_dict(_read_validated_config(config_path))


def main():
    # 在最早阶段修复 systemd 环境变量换行符问题
    _fix_systemd_env()
    
    parser = argparse.ArgumentParser(description='Stock Radar Monitor V2 - 增强版监控服务')
    parser.add_argument('--config', type=str, default='config.json', help='配置文件路径')
    parser.add_argument('--daemon', action='store_true', help='后台模式运行')
    args = parser.parse_args()
    
    try:
        config, targets = load_monitor_bundle(args.config)
        
        # 设置日志
        logger = setup_logging(config.log_file, logging.INFO)
        
        logger.info(">>> Stock Radar Monitor V2 启动 <<<")
        logger.info(f"监控目标: {len(targets)} 只股票")
        logger.info(f"扫描间隔: {config.scan_interval}秒")
        logger.info(f"冷却时间: {config.cooldown_minutes}分钟")
        if config.health_port:
            logger.info(f"健康检查: http://{config.health_host}:{config.health_port}/health")
        
        # 创建引擎（V2）
        engine = MonitorEngine(config, logger)
        
        # 添加监控目标
        for target in targets:
            engine.add_target(target)
            logger.info(f"添加监控: {target.name} ({target.code}) - 触发价: {target.trigger_price}")
        
        # 启动监控
        engine.run()
        
    except KeyboardInterrupt:
        print("\n监控已停止")
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
