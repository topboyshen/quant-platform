#!/usr/bin/env python3
"""
训练/回测 Job 入口
职责：ML/RL模型训练、回测验证
运行方式：独立 Job，不常驻
"""

import os
import sys
import json
import logging
import argparse
from typing import List, Dict

# 训练相关导入（重依赖）
from radar.data.market_data import MarketDataFetcher, MarketTimeChecker
from radar.notify.telegram import TelegramNotifier
from radar.core.monitor_engine import TargetStock

# 可选导入（训练和回测时才需要）
try:
    from radar.ml.predictor import MLPredictor
    from radar.backtest.engine import BacktestEngine, PriceTriggerStrategy
    from rl_agent import RLAgent
    from rl_strategy import RLStrategy as FullRLStrategy
    TRAINING_DEPS_AVAILABLE = True
except ImportError as e:
    print(f"警告: 训练依赖未完全安装: {e}")
    TRAINING_DEPS_AVAILABLE = False


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    """配置日志"""
    logger = logging.getLogger('radar_trainer')
    logger.setLevel(level)
    
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def load_config(config_path: str = "config.json") -> Dict:
    """加载配置"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def train_ml_models(config: Dict, logger: logging.Logger):
    """训练ML模型"""
    if not TRAINING_DEPS_AVAILABLE:
        logger.error("ML训练依赖未安装，跳过")
        return
    
    logger.info("🤖 开始训练ML模型...")
    
    ml_config = config.get('alerts', {}).get('ml_prediction', {})
    if not ml_config.get('enabled', False):
        logger.info("ML预测功能未启用")
        return
    
    predictor = MLPredictor(model_dir="models", logger=logger)
    market = MarketDataFetcher(logger)
    
    min_samples = ml_config.get('min_training_samples', 100)
    prediction_window = ml_config.get('prediction_window', 5)
    
    results = []
    
    for target in config['targets']:
        try:
            code = target['code']
            name = target['name']
            direction = target.get('direction', 'below')
            trigger_price = float(target['trigger_price'])
            
            # 获取历史数据
            df = market.get_historical_data(code, days=120)
            
            if len(df) < min_samples:
                logger.warning(f"{name} 历史数据不足 ({len(df)} < {min_samples})，跳过训练")
                continue
            
            # 训练模型
            result = predictor.train(code, direction, df, trigger_price, prediction_window)
            
            if result.get('success'):
                logger.info(
                    f"✅ {name} 模型训练完成 - "
                    f"准确率: {result['accuracy']:.1%}, "
                    f"精确率: {result['precision']:.1%}"
                )
                results.append({'code': code, 'name': name, 'success': True, **result})
            else:
                logger.warning(f"⚠️ {name} 训练失败: {result.get('reason')}")
                results.append({'code': code, 'name': name, 'success': False})
                
        except Exception as e:
            logger.error(f"训练 {target.get('name', 'unknown')} 模型时出错: {e}")
    
    logger.info(f"🤖 ML模型训练完成: {len([r for r in results if r.get('success')])}/{len(results)}")
    
    # 发送通知
    notifier = TelegramNotifier(
        config['telegram']['bot_token'],
        str(config['telegram']['chat_id']),
        logger
    )
    
    msg = "🤖 ML模型训练报告\n\n"
    for r in results:
        if r.get('success'):
            msg += f"✅ {r['name']}: 准确率 {r['accuracy']:.1%}\n"
        else:
            msg += f"❌ {r['name']}: 训练失败\n"
    
    notifier.send("ML训练完成", msg)


def train_rl_models(config: Dict, logger: logging.Logger, days: int = 120, timesteps: int = 50000):
    """训练RL模型"""
    if not TRAINING_DEPS_AVAILABLE:
        logger.error("RL训练依赖未安装，跳过")
        return
    
    logger.info(f"🧠 开始训练RL模型（历史 {days} 天, {timesteps} steps）...")
    
    rl_config = config.get('alerts', {}).get('rl_strategy', {})
    if not rl_config.get('enabled', False):
        logger.info("RL策略未启用")
        return
    
    agent = RLAgent(model_dir="rl_models", logger=logger)
    market = MarketDataFetcher(logger)
    
    # 准备训练数据
    datasets = []
    for target in config['targets']:
        try:
            code = target['code']
            df = market.get_historical_data(code, days=days)
            if len(df) >= 30:
                datasets.append({
                    'code': code,
                    'name': target['name'],
                    'trigger_price': float(target['trigger_price']),
                    'direction': target.get('direction', 'below'),
                    'df': df
                })
        except Exception as e:
            logger.warning(f"准备 {target['code']} 数据失败: {e}")
    
    if not datasets:
        logger.error("没有足够的数据进行RL训练")
        return
    
    logger.info(f"准备训练数据: {len(datasets)} 只股票")
    
    # 训练 below 和 above 方向的通用模型
    results = []
    
    for direction in ['below', 'above']:
        try:
            direction_datasets = [
                (d['df'], d['trigger_price'], d['code'])
                for d in datasets if d['direction'] == direction
            ]
            
            if not direction_datasets:
                logger.warning(f"没有 {direction} 方向的数据")
                continue
            
            logger.info(f"训练 {direction} 方向通用模型...")
            result = agent.train_universal(
                direction=direction,
                datasets=direction_datasets,
                total_timesteps=timesteps,
                eval_freq=timesteps // 5,
                patience=2
            )
            
            results.append({
                'direction': direction,
                **result
            })
            
            logger.info(
                f"✅ {direction} 模型训练完成 - "
                f"验证夏普: {result['best_val_sharpe']:.2f}, "
                f"步数: {result['timesteps']}"
            )
            
        except Exception as e:
            logger.error(f"训练 {direction} 模型失败: {e}")
    
    # 发送通知
    notifier = TelegramNotifier(
        config['telegram']['bot_token'],
        str(config['telegram']['chat_id']),
        logger
    )
    
    msg = "🧠 RL模型训练报告\n\n"
    for r in results:
        msg += (
            f"📈 方向: {r['direction']}\n"
            f"   验证夏普: {r['best_val_sharpe']:.2f}\n"
            f"   训练步数: {r['timesteps']}\n\n"
        )
    
    notifier.send("RL训练完成", msg)


def run_backtest(config: Dict, logger: logging.Logger, days: int = 60, use_rl: bool = False):
    """运行回测"""
    if not TRAINING_DEPS_AVAILABLE:
        logger.error("回测依赖未安装，跳过")
        return
    
    bt_type = "RL" if use_rl else "传统"
    logger.info(f"📊 开始{bt_type}策略回测（最近 {days} 天）...")
    
    bt_config = config.get('backtest', {})
    if not bt_config.get('enabled', False):
        logger.info("回测功能未启用")
        return
    
    engine = BacktestEngine(
        initial_capital=bt_config.get('initial_capital', 100000),
        commission_rate=bt_config.get('commission_rate', 0.0003),
        slippage=bt_config.get('slippage', 0.001),
        logger=logger
    )
    
    market = MarketDataFetcher(logger)
    results = []
    
    for target in config['targets']:
        try:
            code = target['code']
            name = target['name']
            
            df = market.get_historical_data(code, days=days)
            if len(df) < 30:
                logger.warning(f"{name} 数据不足，跳过回测")
                continue
            
            # 创建策略
            if use_rl:
                strategy = FullRLStrategy(
                    code=code,
                    direction=target.get('direction', 'below'),
                    trigger_price=float(target['trigger_price']),
                    position_pct=bt_config.get('position_size', 0.2),
                    universal=True
                )
            else:
                strategy = PriceTriggerStrategy(
                    trigger_price=float(target['trigger_price']),
                    direction=target.get('direction', 'below'),
                    stop_loss_pct=bt_config.get('stop_loss_pct', 0.05),
                    take_profit_pct=bt_config.get('take_profit_pct', 0.10)
                )
            
            # 运行回测
            result = engine.run(df, strategy, position_size=bt_config.get('position_size', 0.2))
            
            results.append({
                'name': name,
                'code': code,
                'result': result
            })
            
            logger.info(f"\n{'='*40}\n{name} 回测结果:\n{engine.generate_report(result)}")
            
        except Exception as e:
            logger.error(f"回测 {target.get('name', 'unknown')} 时出错: {e}")
    
    # 发送汇总报告
    notifier = TelegramNotifier(
        config['telegram']['bot_token'],
        str(config['telegram']['chat_id']),
        logger
    )
    
    msg = f"📊 {bt_type}策略回测汇总\n\n"
    for r in results:
        result = r['result']
        msg += (
            f"📈 {r['name']} ({r['code']})\n"
            f"   总收益: {result.total_return:+.2%} | "
            f"胜率: {result.win_rate:.0%} | "
            f"交易: {result.total_trades}次\n"
            f"   最大回撤: {result.max_drawdown:.2%} | "
            f"夏普: {result.sharpe_ratio:.2f}\n\n"
        )
    
    notifier.send(f"{bt_type}回测报告", msg)


def main():
    parser = argparse.ArgumentParser(description='Stock Radar Trainer - 训练/回测Job')
    parser.add_argument('--train-ml', action='store_true', help='训练ML模型')
    parser.add_argument('--train-rl', action='store_true', help='训练RL模型')
    parser.add_argument('--backtest', action='store_true', help='运行传统策略回测')
    parser.add_argument('--rl-backtest', action='store_true', help='运行RL策略回测')
    parser.add_argument('--days', type=int, default=120, help='训练/回测天数')
    parser.add_argument('--timesteps', type=int, default=50000, help='RL训练步数')
    parser.add_argument('--config', type=str, default='config.json', help='配置文件路径')
    args = parser.parse_args()
    
    # 加载配置
    config = load_config(args.config)
    
    # 设置日志
    log_file = config.get('radar', {}).get('log_file', 'radar_trainer.log')
    logger = setup_logging(log_file, logging.INFO)
    
    logger.info(f"Radar Trainer 启动: args={args}")
    
    # 执行对应任务
    if args.train_ml:
        train_ml_models(config, logger)
    elif args.train_rl:
        train_rl_models(config, logger, days=args.days, timesteps=args.timesteps)
    elif args.backtest:
        run_backtest(config, logger, days=args.days, use_rl=False)
    elif args.rl_backtest:
        run_backtest(config, logger, days=args.days, use_rl=True)
    else:
        parser.print_help()
        sys.exit(1)
    
    logger.info("Radar Trainer 任务完成")


if __name__ == '__main__':
    main()
