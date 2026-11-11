#!/usr/bin/env python3
"""
Stock Radar Pro - A股价格监控雷达 (专业版)
支持：双向触发、成交量异常、技术指标、ML预测、回测系统

本文件 ``python radar_pro.py`` 保留兼容 CLI。

RL 相关依赖（gymnasium、SB3 等）在首次调用 RL 训练/推理/RL 回测时再导入，
便于在未装全量 RL 栈时仍能使用 --help 与仅 ML 训练。
"""

import time
import datetime
import json
import os
import sys
import logging
import argparse
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import pytz
import pandas as pd
import numpy as np
import requests

# 东方财富妙想 API 配置（从环境变量读取，不再硬编码）
MKT_API_KEY = os.getenv("MKT_API_KEY", "")

# ML / 回测 / RL 重依赖在用到时再导入，避免轻量环境无法查看 CLI 帮助。

# 从拆分模块导入（向后兼容）
from radar_pro.config import TargetStock, RadarConfig, load_config, load_portfolio, setup_logging
from radar_pro.market_fetcher import MarketDataFetcher
from radar_pro.cache import CacheManager

# ================= 📡 通知模块 =================
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, logger: logging.Logger):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logger
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send(self, title: str, content: str) -> bool:
        self.logger.info(f"[🔴 警报中] {title}")
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": f"⚠️ *{title}*\n\n{content}",
            "parse_mode": "Markdown"
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            self.logger.error(f"Telegram 发送失败: {e}")
            return False

# ================= 🕐 市场时间模块（北京时间） =================
class MarketTimeChecker:
    """A股交易时间检查器 - 使用北京时间"""
    
    def __init__(self, config: Dict, logger: logging.Logger):
        self.logger = logger
        self.beijing_tz = pytz.timezone('Asia/Shanghai')
        self.morning_start = self._parse_time(config['morning_start'])
        self.morning_end = self._parse_time(config['morning_end'])
        self.afternoon_start = self._parse_time(config['afternoon_start'])
        self.afternoon_end = self._parse_time(config['afternoon_end'])
    
    @staticmethod
    def _parse_time(time_str: str) -> datetime.time:
        hour, minute = map(int, time_str.split(':'))
        return datetime.time(hour, minute)
    
    def get_beijing_time(self) -> datetime.datetime:
        """获取当前北京时间"""
        utc_now = datetime.datetime.now(pytz.UTC)
        return utc_now.astimezone(self.beijing_tz)
    
    def is_open(self) -> bool:
        """判断当前是否为A股交易时间（北京时间）"""
        now = self.get_beijing_time()
        
        # 周末休市（周一=0, 周日=6）
        if now.weekday() >= 5:
            return False
        
        curr_time = now.time()
        morning = self.morning_start <= curr_time <= self.morning_end
        afternoon = self.afternoon_start <= curr_time <= self.afternoon_end
        
        is_trading = morning or afternoon
        
        # 调试日志
        if is_trading:
            self.logger.debug(f"北京时间 {now.strftime('%H:%M')} - 交易时段")
        
        return is_trading
    
    def get_time_info(self) -> str:
        """获取当前时间信息字符串"""
        now = self.get_beijing_time()
        utc_now = datetime.datetime.now(pytz.UTC)
        return f"北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (UTC: {utc_now.strftime('%H:%M:%S')})"


# ================= 🤖 ML增强引擎 =================
class MLRadarEngine:
    """集成机器学习的雷达引擎"""
    
    def __init__(self, config: RadarConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        self.cache = CacheManager(config.cache_file, logger)
        self.notifier = TelegramNotifier(config.bot_token, config.chat_id, logger)
        self.market = MarketDataFetcher(logger)
        self.time_checker = MarketTimeChecker(config.market_hours, logger)
        
        # 初始化ML预测器
        ml_config = config.alerts.get('ml_prediction', {})
        self.ml_enabled = ml_config.get('enabled', False)
        self.ml_threshold = ml_config.get('probability_threshold', 0.7)
        if self.ml_enabled:
            from radar.ml.predictor import MLPredictor

            self.predictor = MLPredictor(model_dir="models", logger=logger)
        else:
            self.predictor = None
        
        # 价格历史缓存（用于ML预测）
        self._price_history: Dict[str, List[float]] = {}
        self._volume_history: Dict[str, List[float]] = {}
        self.min_history = 30
        
        self._rl_agent = None
        self.rl_enabled = config.alerts.get('rl_strategy', {}).get('enabled', True)
        
        # 加载真实持仓
        self.portfolio = load_portfolio("portfolio.json")
        self._portfolio_cache = {}  # 持仓缓存
        self._update_portfolio_cache()
        
        self.running = True

    def _ensure_rl_agent(self):
        """延迟加载 RLAgent（依赖 gymnasium / stable-baselines3）。"""
        if self._rl_agent is None:
            from rl_agent import RLAgent

            self._rl_agent = RLAgent(model_dir="rl_models", logger=self.logger)
        return self._rl_agent

    def _update_portfolio_cache(self):
        """更新持仓缓存"""
        self._portfolio_cache = {}
        for pos in self.portfolio.get("positions", []):
            self._portfolio_cache[pos["code"]] = {
                "shares": pos.get("shares", 0),
                "cost_basis": pos.get("cost_basis", 0),
                "avg_cost": pos.get("avg_cost", 0)
            }
    
    def get_real_position(self, code: str) -> tuple:
        """获取真实持仓 (shares, cost_basis)"""
        if code in self._portfolio_cache:
            pos = self._portfolio_cache[code]
            return pos["shares"], pos["cost_basis"]
        return 0, 0.0
    
    def collect_history(self, target: TargetStock, data: Dict):
        """收集历史数据用于ML预测"""
        code = target.code
        
        if code not in self._price_history:
            self._price_history[code] = []
            self._volume_history[code] = []
        
        self._price_history[code].append(data['price'])
        self._volume_history[code].append(data.get('volume', 0))
        
        # 保持最多100条
        if len(self._price_history[code]) > 100:
            self._price_history[code] = self._price_history[code][-100:]
            self._volume_history[code] = self._volume_history[code][-100:]
    
    def check_ml_prediction(self, target: TargetStock) -> Optional[Dict]:
        """检查ML预测"""
        if not self.ml_enabled or self.predictor is None:
            return None
        
        # 检查该股票是否启用ML预测（支持除权等特殊情况禁用）
        target_ml_enabled = getattr(target, 'ml_enabled', True)
        if not target_ml_enabled:
            return None
        
        code = target.code
        
        if code not in self._price_history or len(self._price_history[code]) < self.min_history:
            return None
        
        prediction = self.predictor.predict(
            code, target.direction,
            self._price_history[code],
            self._volume_history[code]
        )
        
        if prediction is None:
            return None
        
        # 高概率预警
        if prediction['probability'] >= self.ml_threshold:
            if not self.cache.is_in_cooldown(code, 'ml_prediction', self.config.cooldown_minutes):
                prob_pct = prediction['probability'] * 100
                conf_emoji = "🔴" if prediction['confidence'] == 'high' else "🟡"
                
                msg = (
                    f"标的：{target.name} ({code})\n"
                    f"预测概率：{prob_pct:.1f}% {conf_emoji}\n"
                    f"置信度：{prediction['confidence'].upper()}\n"
                    f"预测结果：将在未来触发 {target.direction} 信号\n"
                    f"触发价格：{target.trigger_price}\n"
                    f"\n💡 主要特征:\n"
                )
                
                # 添加重要特征
                features = prediction.get('features', {})
                top_features = sorted(features.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
                for feat_name, feat_val in top_features:
                    msg += f"• {feat_name}: {feat_val:.2f}\n"
                
                self.notifier.send("🤖 AI 预测预警", msg)
                self.cache.record_alert(code, 'ml_prediction')
                self.logger.info(f"[🤖 ML预测] {target.name} - 概率 {prob_pct:.1f}%")
                
                return prediction
        
        return None
    
    def check_price_trigger(self, target: TargetStock, data: Dict) -> Optional[str]:
        """检查价格触发（双向）"""
        current_price = data['price']
        
        triggered = False
        trigger_type = ""
        
        if target.direction == 'below' and current_price <= target.trigger_price:
            triggered = True
            trigger_type = "下破买入"
        elif target.direction == 'above' and current_price >= target.trigger_price:
            triggered = True
            trigger_type = "上破卖出"
        
        if not triggered:
            return None
        
        if self.cache.is_in_cooldown(target.code, 'price', self.config.cooldown_minutes):
            return None
        
        msg = (
            f"标的：{target.name} ({target.code})\n"
            f"现价：{current_price:.2f}\n"
            f"阈值：{target.trigger_price:.2f}\n"
            f"方向：{trigger_type}\n"
            f"状态：{'买入' if '买入' in trigger_type else '卖出'}信号触发！"
        )
        
        self.notifier.send(f"🎯 {trigger_type}信号", msg)
        self.cache.record_alert(target.code, 'price')
        self.logger.info(f"[✅ 价格触发] {target.name} - {trigger_type}")
        return trigger_type
    
    def _build_rl_observation(self, target: TargetStock) -> Optional[Any]:
        """构建 RL 观察状态"""
        code = target.code
        if code not in self._price_history or len(self._price_history[code]) < 30:
            return None
        
        from rl_features import RLObservationBuilder

        prices = self._price_history[code]
        volumes = self._volume_history.get(code, [0] * len(prices))
        df = pd.DataFrame({'price': prices, 'volume': volumes})

        obs_builder = RLObservationBuilder(df, target.trigger_price, cache_dir="rl_feature_cache")
        real_shares, real_cost = self.get_real_position(code)
        return obs_builder.get_observation(
            index=len(df) - 1,
            position_shares=real_shares,
            position_value=real_cost
        )
    
    @staticmethod
    def _format_rl_action(action: int) -> str:
        action_names = {
            0: "保持观望",
            1: "建仓",
            2: "加仓",
            3: "减仓",
            4: "清仓"
        }
        return action_names.get(action, f"未知({action})")
    
    def check_rl_signal(self, target: TargetStock, data: Dict):
        """检查 RL 策略信号并发送通知"""
        if not self.rl_enabled:
            return
        
        code = target.code
        
        # 冷却检查
        if self.cache.is_in_cooldown(code, 'rl_signal', self.config.cooldown_minutes):
            return
        
        try:
            obs = self._build_rl_observation(target)
            if obs is None:
                return
            
            action = self._ensure_rl_agent().predict(code, target.direction, obs, universal=True)
            if action is None or action == 0:
                return
            
            action_name = self._format_rl_action(action)
            msg = (
                f"标的：{target.name} ({code})\n"
                f"现价：{data['price']:.2f}\n"
                f"触发价：{target.trigger_price:.2f}\n"
                f"方向：{target.direction}\n"
                f"🧠 RL 建议动作：{action_name}\n"
            )
            
            self.notifier.send(f"🧠 RL 策略信号", msg)
            self.cache.record_alert(code, 'rl_signal')
            self.logger.info(f"[🧠 RL信号] {target.name} - 动作: {action_name}")
        
        except Exception as e:
            self.logger.error(f"检查 RL 信号时出错 {target.name}: {e}")
    
    def process_target(self, target: TargetStock):
        """处理单个目标"""
        data = self.market.get_stock_data(target.code, target.name)
        if data is None:
            return
        
        # 收集历史数据
        self.collect_history(target, data)
        
        # 1. ML预测
        self.check_ml_prediction(target)
        
        # 2. RL策略信号
        self.check_rl_signal(target, data)
        
        # 3. 价格触发
        self.check_price_trigger(target, data)
    
    def train_models(self):
        """为所有目标训练ML模型"""
        if not self.ml_enabled or self.predictor is None:
            self.logger.info("ML预测功能未启用")
            return
        
        self.logger.info("🤖 开始训练ML模型...")
        ml_config = self.config.alerts.get('ml_prediction', {})
        min_samples = ml_config.get('min_training_samples', 100)
        prediction_window = ml_config.get('prediction_window', 5)
        
        for target in self.config.targets:
            try:
                # 获取历史数据
                df = self.market.get_historical_data(target.code, days=120)
                
                if len(df) < min_samples:
                    self.logger.warning(f"{target.name} 历史数据不足 ({len(df)} < {min_samples})，跳过训练")
                    continue
                
                # 训练模型
                result = self.predictor.train(
                    target.code, target.direction, df,
                    target.trigger_price, prediction_window
                )
                
                if result.get('success'):
                    self.logger.info(
                        f"✅ {target.name} 模型训练完成 - "
                        f"准确率: {result['accuracy']:.1%}, "
                        f"精确率: {result['precision']:.1%}, "
                        f"样本: {result['samples']}"
                    )
                else:
                    self.logger.warning(f"⚠️ {target.name} 训练失败: {result.get('reason')}")
                    
            except Exception as e:
                self.logger.error(f"训练 {target.name} 模型时出错: {e}")
        
        self.logger.info("🤖 ML模型训练完成")
    
    def run_backtest(self, days: int = 60):
        """运行回测"""
        if not self.config.backtest.get('enabled', False):
            self.logger.info("回测功能未启用")
            return
        
        self.logger.info(f"📊 开始回测（最近 {days} 天）...")

        from radar.backtest.engine import BacktestEngine, PriceTriggerStrategy
        
        bt_config = self.config.backtest
        engine = BacktestEngine(
            initial_capital=bt_config.get('initial_capital', 100000),
            commission_rate=bt_config.get('commission_rate', 0.0003),
            slippage=bt_config.get('slippage', 0.001),
            logger=self.logger
        )
        
        results = []
        
        for target in self.config.targets:
            try:
                # 获取历史数据
                df = self.market.get_historical_data(target.code, days=days)
                
                if len(df) < 30:
                    self.logger.warning(f"{target.name} 数据不足，跳过回测")
                    continue
                
                # 创建策略
                strategy = PriceTriggerStrategy(
                    trigger_price=target.trigger_price,
                    direction=target.direction,
                    stop_loss_pct=bt_config.get('stop_loss_pct', 0.05),
                    take_profit_pct=bt_config.get('take_profit_pct', 0.10)
                )
                
                # 运行回测
                result = engine.run(
                    df, strategy,
                    position_size=bt_config.get('position_size', 0.2)
                )
                
                results.append({
                    'name': target.name,
                    'code': target.code,
                    'result': result
                })
                
                # 发送报告
                report = engine.generate_report(result)
                self.logger.info(f"\n{'='*40}\n{target.name} 回测结果:\n{'='*40}")
                self.logger.info(report)
                
            except Exception as e:
                self.logger.error(f"回测 {target.name} 时出错: {e}")
        
        # 汇总报告
        self._send_backtest_summary(results)
    
    def _send_backtest_summary(self, results: List[Dict]):
        """发送回测汇总"""
        if not results:
            return
        
        msg = "📊 策略回测汇总报告\n\n"
        
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
        
        self.notifier.send("📊 策略回测报告", msg)
    
    def train_rl_models(self, days: int = 120, timesteps: int = 50000):
        """训练 RL 模型（通用 Market-Agnostic 策略）"""
        if not self.rl_enabled:
            self.logger.info("RL策略未启用")
            return

        self.logger.info(f"🧠 开始训练通用 RL 模型（历史 {days} 天, {timesteps} steps）...")

        targets = [
            {'code': t.code, 'name': t.name, 'trigger_price': t.trigger_price, 'direction': t.direction}
            for t in self.config.targets
        ]

        results = self._ensure_rl_agent().train_all(
            targets=targets,
            market_fetcher=self.market,
            days=days,
            timesteps=timesteps,
            universal=True
        )

        if results:
            msg = "🧠 通用 RL 模型训练完成\n\n"
            for r in results:
                msg += (
                    f"📈 方向: {r['direction']}\n"
                    f"   平均收益: {r['avg_return']:.2%}\n"
                    f"   最佳验证夏普: {r['best_val_sharpe']:.2f}\n"
                    f"   训练步数: {r['timesteps']}\n\n"
                )
            self.notifier.send("🧠 RL 训练报告", msg)

        self.logger.info("🧠 RL 通用模型训练全部完成")
    
    def _patch_strategy_for_backtest(self, strategy, df):
        """为回测临时修复策略，注入 df 到 context"""
        original_enter = strategy.should_enter
        original_exit = strategy.should_exit
        
        def patched_enter(data, context):
            context['df'] = df
            return original_enter(data, context)
        
        def patched_exit(data, context, entry_price):
            context['df'] = df
            return original_exit(data, context, entry_price)
        
        strategy.should_enter = patched_enter
        strategy.should_exit = patched_exit
        return original_enter, original_exit
    
    def run_rl_backtest(self, days: int = 60):
        """运行 RL 策略回测"""
        if not self.config.backtest.get('enabled', False):
            self.logger.info("回测功能未启用")
            return
        
        self.logger.info(f"🧠 开始 RL 策略回测（最近 {days} 天）...")

        from radar.backtest.engine import BacktestEngine
        
        bt_config = self.config.backtest
        engine = BacktestEngine(
            initial_capital=bt_config.get('initial_capital', 100000),
            commission_rate=bt_config.get('commission_rate', 0.0003),
            slippage=bt_config.get('slippage', 0.001),
            logger=self.logger
        )
        
        results = []
        
        for target in self.config.targets:
            try:
                df = self.market.get_historical_data(target.code, days=days)
                if len(df) < 30:
                    self.logger.warning(f"{target.name} 数据不足，跳过 RL 回测")
                    continue

                from rl_strategy import RLStrategy

                strategy = RLStrategy(
                    code=target.code,
                    direction=target.direction,
                    trigger_price=target.trigger_price,
                    position_pct=bt_config.get('position_size', 0.2),
                    universal=True
                )
                
                # 临时修复策略以注入 df
                self._patch_strategy_for_backtest(strategy, df)
                
                result = engine.run(df, strategy)
                
                results.append({
                    'name': target.name,
                    'code': target.code,
                    'result': result
                })
                
                self.logger.info(f"\n{'='*40}\n{target.name} RL回测结果:\n{'='*40}")
                self.logger.info(engine.generate_report(result))
                
            except Exception as e:
                self.logger.error(f"RL 回测 {target.name} 时出错: {e}")
        
        self._send_backtest_summary(results)
    
    def run(self):
        """主循环"""
        self.logger.info(">>> Stock Radar Pro 已启动 <<<")
        self.logger.info(f"监控股票: {len(self.config.targets)} 只")
        self.logger.info(f"功能: 价格触发 | 成交量 | 技术指标 | ML预测 | RL策略 | 回测")
        
        self.notifier.send("🚀 Radar Pro 上线", 
            f"专业版雷达已启动\n"
            f"• 双向价格触发\n"
            f"• 成交量异常检测\n"
            f"• 技术指标分析\n"
            f"• 🤖 AI预测\n"
            f"• 🧠 RL策略\n"
            f"• 📊 回测系统"
        )
        
        while self.running:
            try:
                if not self.time_checker.is_open():
                    self.logger.debug("非交易时段...")
                    time.sleep(60)
                    continue
                
                self.logger.debug(f"扫描 {len(self.config.targets)} 个目标...")
                
                for target in self.config.targets:
                    try:
                        self.process_target(target)
                    except Exception as e:
                        self.logger.error(f"处理 {target.name} 出错: {e}")
                
                time.sleep(self.config.scan_interval)
                
            except KeyboardInterrupt:
                self.logger.info("正在停止...")
                self.running = False
            except Exception as e:
                self.logger.error(f"主循环异常: {e}")
                time.sleep(10)
        
        self.logger.info("雷达已停止")

# ================= 🚀 入口 =================
def _engine_from_args(config_path: str):
    config = load_config(config_path)
    logger = setup_logging(config.log_file, logging.INFO)
    return MLRadarEngine(config, logger)


def entry_monitor(argv: Optional[List[str]] = None) -> int:
    """实时监控（原默认模式）。推荐：python radar_pro_cli.py"""
    parser = argparse.ArgumentParser(description="Radar Pro — 实时监控")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    args = parser.parse_args(argv)
    try:
        engine = _engine_from_args(args.config)
        engine.run()
        return 0
    except Exception as e:
        print(f"启动失败: {e}")
        return 1


def entry_train(argv: Optional[List[str]] = None) -> int:
    """ML / RL 训练。推荐：python radar_pro_train.py"""
    parser = argparse.ArgumentParser(description="Radar Pro — 模型训练")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument(
        "--rl",
        action="store_true",
        help="训练通用 RL 模型（否则训练 ML）",
    )
    parser.add_argument("--rl-days", type=int, default=120, help="RL 训练历史天数")
    parser.add_argument("--rl-timesteps", type=int, default=50000, help="RL 训练步数")
    args = parser.parse_args(argv)
    try:
        engine = _engine_from_args(args.config)
        if args.rl:
            engine.train_rl_models(days=args.rl_days, timesteps=args.rl_timesteps)
        else:
            engine.train_models()
        return 0
    except Exception as e:
        print(f"训练失败: {e}")
        return 1


def entry_backtest(argv: Optional[List[str]] = None) -> int:
    """价格触发 / RL 回测。推荐：python radar_pro_backtest.py"""
    parser = argparse.ArgumentParser(description="Radar Pro — 回测")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument(
        "--rl",
        action="store_true",
        help="RL 策略回测（否则 PriceTrigger 回测）",
    )
    parser.add_argument("--days", type=int, default=60, help="回测天数")
    args = parser.parse_args(argv)
    try:
        engine = _engine_from_args(args.config)
        if args.rl:
            engine.run_rl_backtest(days=args.days)
        else:
            engine.run_backtest(days=args.days)
        return 0
    except Exception as e:
        print(f"回测失败: {e}")
        return 1


def main():
    """兼容入口：等价于旧版单文件 CLI。建议使用 radar_pro_cli / train / backtest。"""
    parser = argparse.ArgumentParser(
        description="Stock Radar Pro（全功能；新拆分为 radar_pro_cli|train|backtest）"
    )
    parser.add_argument("--train", action="store_true", help="训练ML模型")
    parser.add_argument("--backtest", action="store_true", help="运行回测")
    parser.add_argument("--backtest-days", type=int, default=60, help="回测天数")
    parser.add_argument("--train-rl", action="store_true", help="训练RL模型")
    parser.add_argument("--rl-backtest", action="store_true", help="运行RL策略回测")
    parser.add_argument("--rl-days", type=int, default=120, help="RL训练历史天数")
    parser.add_argument("--rl-timesteps", type=int, default=50000, help="RL训练步数")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--monitor", action="store_true", help="运行实时监控（默认）")
    args = parser.parse_args()

    try:
        engine = _engine_from_args(args.config)

        if args.train:
            engine.train_models()
        elif args.backtest:
            engine.run_backtest(days=args.backtest_days)
        elif args.train_rl:
            engine.train_rl_models(days=args.rl_days, timesteps=args.rl_timesteps)
        elif args.rl_backtest:
            engine.run_rl_backtest(days=args.backtest_days)
        else:
            engine.run()

    except Exception as e:
        print(f"启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
