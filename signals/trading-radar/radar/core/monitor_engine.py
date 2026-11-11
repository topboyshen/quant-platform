"""
监控核心引擎 - 轻量化实时监控
职责：行情监控、告警触发、通知发送
不依赖：PyTorch/scikit-learn/backtrader 等训练重依赖

通过 Mixin 拆分：
- HealthServerMixin: 健康检查服务器
- EventModeMixin: 事件驱动模式
- PollModeMixin: 传统轮询模式
- AlertDispatchMixin: 告警分发与批处理
- CleanupMixin: 退出清理与状态持久化
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from radar.alert.batcher import AlertItem, AlertPriority
from radar.alert.cooldown_v2 import AlertCooldownManager
from radar.core.alert_dispatch_mixin import AlertDispatchMixin
from radar.core.battlefield_state import BattlefieldStateManager
from radar.core.cleanup_mixin import CleanupMixin
from radar.core.event_mode_mixin import EventModeMixin
from radar.core.health_server import get_health_monitor

# Mixin 导入
from radar.core.health_server_mixin import HealthServerMixin
from radar.core.poll_mode_mixin import PollModeMixin
from radar.core.portfolio_loader import load_portfolio_into_tracker
from radar.core.strategy_mixin import StrategyMixin
from radar.core.target_process_pipeline import (
    TARGET_PROCESS_STEPS,
    TargetProcessContext,
    run_target_pipeline,
)
from radar.data.market_data import MarketTimeChecker

# V2 增强模块（V1/V2 行为已收敛；旧模块 radar.alert.cooldown /
# radar.data.market_data_concurrent 仍保留供 battlefield_state 等独立模块使用）
from radar.data.market_data_concurrent_v2 import ConcurrentMarketDataFetcher
from radar.notify.telegram import TelegramNotifier
from radar.strategy.base import PriceTriggerStrategy
from radar.strategy.ml_strategy import MLStrategy
from radar.strategy.rl_strategy_v2 import PortfolioTracker, RLStrategyV2


@dataclass
class TargetStock:
    """监控目标"""
    code: str
    name: str
    trigger_price: float
    direction: str
    ml_enabled: bool = True
    rl_enabled: bool = True
    # Grafana / Prometheus 分组：如 Strategy_Grid_T0、Strategy_MACD_Breakout
    strategy_tag: str = ""


@dataclass
class MonitorConfig:
    """监控配置"""
    bot_token: str
    chat_id: str
    cooldown_minutes: int
    scan_interval: int
    cache_file: str
    log_file: str
    market_hours: Dict
    ml_threshold: float = 0.7
    # ML 推理前至少保留的价量 bar 数（不低于特征工程下限 30，见 ml_strategy）
    ml_min_history_bars: int = 30
    ml_model_dir: str = "models"
    rl_model_dir: str = "rl_models"
    # RL 模式: 'confirm' = 价格触发后辅助确认（推荐）
    #          'independent' = 独立信号（需配合真实账户）
    rl_mode: str = "confirm"
    initial_cash: float = 100000.0  # 模拟初始资金
    health_host: str = "0.0.0.0"
    health_port: int = 8080  # 0 表示关闭健康检查服务器
    batch_alerts: bool = True  # 是否启用告警批处理合并
    # 告警优先级: high=立即发送, medium=合并发送, low=只记录日志
    alert_priorities: Optional[Dict[str, Union[str, AlertPriority]]] = None
    # 数据源配置（可空）
    data_sources: Optional[Dict] = None


class MonitorEngine(
    HealthServerMixin,
    EventModeMixin,
    PollModeMixin,
    AlertDispatchMixin,
    CleanupMixin,
    StrategyMixin,
):
    """
    监控引擎 - 轻量级实时监控
    只进行推理，不训练

    通过 Mixin 拆分职责：
    - HealthServerMixin: 健康检查服务器管理
    - EventModeMixin: 事件驱动模式
    - PollModeMixin: 传统轮询模式
    - AlertDispatchMixin: 告警分发与批处理
    - CleanupMixin: 退出清理与状态持久化
    """

    def __init__(self, config: MonitorConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.running = True

        # 数据层使用 V2 实现（内存缓存层 / 批量缓存索引），V1 fallback 已删除
        self.cooldown = AlertCooldownManager(config.cache_file, logger)
        self.notifier = TelegramNotifier(config.bot_token, config.chat_id, logger)
        self.market = ConcurrentMarketDataFetcher(
            logger=logger,
            max_workers=5,
            timeout=15.0,
            on_circuit_open=self._on_datasource_circuit_open,
            on_circuit_close=self._on_datasource_circuit_close,
        )
        self.time_checker = MarketTimeChecker(config.market_hours, logger)

        # 监控目标列表
        self.targets: List[TargetStock] = []

        # 策略实例缓存
        self._price_strategies: Dict[str, PriceTriggerStrategy] = {}
        self._ml_strategies: Dict[str, MLStrategy] = {}
        self._rl_strategies: Dict[str, RLStrategyV2] = {}

        # 组合持仓追踪（用于 RL 真实账户状态）
        self._portfolio = PortfolioTracker(initial_cash=config.initial_cash)
        self._load_real_portfolio()

        # 健康状态监控
        self._health = get_health_monitor()
        self._health.update_stock_count(len(self.targets))

        # 连续失败计数
        self._consecutive_failures = 0
        self._consecutive_failure_threshold = 3
        self._failure_alert_sent = False

        # 数据源熔断通知开关
        ds_cfg = config.data_sources or {}
        self._datasource_circuit_alert = ds_cfg.get("circuit_alert_enabled", True)

        # 健康检查服务器（由 HealthServerMixin 初始化）
        self._health_server = self._init_health_server()

        # 告警批处理器（由 AlertDispatchMixin 初始化）
        self._alert_batcher = self._init_alert_batcher()
        self._target_pipeline: Tuple[Any, ...] = TARGET_PROCESS_STEPS

        # 阵地状态持久化（恢复运行时状态）
        self._battlefield = BattlefieldStateManager(logger=self.logger)
        self._battlefield.restore_all(self)
        self.logger.info("[MonitorEngine] 阵地状态已恢复（如有）")

        # Battlefield 写频率优化（每 N 次扫描写一次）
        self._scan_count_since_save = 0
        self.BATTLEFIELD_WRITE_INTERVAL = 5

        # 性能指标采集
        self._perf_stats = {
            'total_scans': 0,
            'total_fetch_time': 0.0,
            'total_process_time': 0.0,
            'max_fetch_time': 0.0,
            'max_process_time': 0.0,
        }

        # 事件驱动模式（可选，替代轮询）
        self._event_bus: Optional[Any] = None
        self._push_adapter: Optional[Any] = None
        self._event_mode = False

    def _load_real_portfolio(self, portfolio_path: str = "portfolio.json") -> None:
        """从配置文件加载真实持仓到 PortfolioTracker"""
        load_portfolio_into_tracker(self._portfolio, portfolio_path, self.logger)

    def add_target(self, target: TargetStock) -> None:
        """添加监控目标"""
        self.targets.append(target)

        # 创建策略实例
        self._create_strategies_for_target(target)

        self._health.update_stock_count(len(self.targets))

    # 策略方法已提取到 StrategyMixin

    def process_target(
        self, target: TargetStock, data: Optional[Dict[str, Any]] = None
    ) -> List[AlertItem]:
        """
        处理单个目标（支持传入预获取的数据）
        返回该股票触发的所有告警（用于批处理合并）
        """
        if data is None:
            data = self.market.fetch_single(target.code, target.name)

        if not data:
            self.logger.warning(f"无法获取 {target.name} 数据")
            return []

        if os.getenv("RADAR_SKIP_LIVE_QUOTE_PYDANTIC", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            try:
                from pydantic import ValidationError

                from radar.schemas.live_quote import validate_live_quote_dict

                data = validate_live_quote_dict(data)
            except ValidationError as e:
                self.logger.warning(
                    "实盘行情未通过 Pydantic 校验 %s %s: %s",
                    target.code,
                    target.name,
                    e,
                )
                return []

        ctx = TargetProcessContext(
            target=target,
            data=data,
            current_price=data["price"],
        )
        return run_target_pipeline(self, ctx, steps=self._target_pipeline)

    def _fetch_all_data(self) -> Tuple[Dict[str, Any], int]:
        """并发获取所有目标股票数据，返回 (data_dict, success_count)"""
        stocks = [(t.code, t.name) for t in self.targets]
        try:
            all_data = self.market.fetch_multiple(stocks)
            return all_data, len(all_data)
        except Exception as e:
            self.logger.error(f"获取行情数据异常: {e}")
            return {}, 0

    def _handle_total_failure(self) -> None:
        """处理全部数据获取失败的情况"""
        self._consecutive_failures += 1
        self.logger.warning(
            f"本次扫描未获取到任何股票数据"
            f"（连续失败: {self._consecutive_failures}/{self._consecutive_failure_threshold}）"
        )

        if (self._consecutive_failures >= self._consecutive_failure_threshold
     and not self._failure_alert_sent):
            try:
                self.notifier.send_system_alert(
                    "数据获取异常",
                    f"连续 {self._consecutive_failures} 次扫描失败，可能是东方财富API异常\n"
                    f"请检查网络或数据源状态"
                )
                self._failure_alert_sent = True
            except Exception as e:
                self.logger.error(f"发送失败告警失败: {e}")

        self._health.record_scan(stock_count=len(self.targets), success=False)

    def _reset_failure_state(self) -> None:
        """重置连续失败状态"""
        if self._consecutive_failures > 0:
            self.logger.info(
                f"数据获取恢复，重置连续失败计数"
                f"（之前: {self._consecutive_failures}次）"
            )
            self._consecutive_failures = 0
            self._failure_alert_sent = False

    def _process_targets_batch(self, all_data: Dict[str, Any]) -> bool:
        """串行处理所有目标，返回是否有错误"""
        has_errors = False
        for target in self.targets:
            try:
                data = all_data.get(target.code)
                if data:
                    alerts = self.process_target(target, data)
                    for alert in alerts:
                        self._alert_batcher.add(alert)
                else:
                    self.logger.warning(f"{target.name}({target.code}) 数据缺失，跳过本次扫描")
                    has_errors = True
            except Exception as e:
                self.logger.error(f"处理 {target.name} 出错: {e}")
                has_errors = True
        return has_errors

    def process_all_targets_concurrent(self) -> None:
        """
        并发处理所有目标（V2 优化版）
        - 采集 fetch/process 性能指标
        - 优化 Battlefield 写频率
        """
        if not self.targets:
            self._health.record_scan(0, success=False)
            return

        self._perf_stats['total_scans'] += 1
        scan_start = time.time()

        # 1. 并发获取所有股票数据
        fetch_start = time.time()
        all_data, success_count = self._fetch_all_data()
        fetch_time = time.time() - fetch_start
        self._perf_stats['total_fetch_time'] += fetch_time
        self._perf_stats['max_fetch_time'] = max(self._perf_stats['max_fetch_time'], fetch_time)

        self.logger.debug(
            f"并发获取 {len(self.targets)} 只股票数据耗时: {fetch_time:.2f}s, "
            f"成功: {success_count}/{len(self.targets)}"
        )

        # 如果全部失败
        if success_count == 0:
            self._handle_total_failure()
            return

        # 有成功获取，重置失败计数
        self._reset_failure_state()

        # 2. 串行处理告警判断
        process_start = time.time()
        has_errors = self._process_targets_batch(all_data)
        process_time = time.time() - process_start
        self._perf_stats['total_process_time'] += process_time
        self._perf_stats['max_process_time'] = max(self._perf_stats['max_process_time'], process_time)

        # 3. 发送合并后的批次告警
        self._flush_alert_batch()

        # 4. 持久化阵地状态（按频率写回，减少 I/O）
        self._scan_count_since_save += 1
        if self._scan_count_since_save >= self.BATTLEFIELD_WRITE_INTERVAL:
            self._scan_count_since_save = 0
            if hasattr(self, '_battlefield'):
                try:
                    self._battlefield.save_all(self)
                except Exception as e:
                    self.logger.warning(f"[MonitorEngine] 阵地状态保存失败: {e}")

        try:
            from radar.core.trading_observability import refresh_trading_observability
            refresh_trading_observability(self)
        except (RuntimeError, OSError):
            pass

        # 5. 记录健康状态
        self._health.record_scan(
            stock_count=len(self.targets),
            success=not has_errors
        )

        # 6. 慢扫描告警
        total_time = time.time() - scan_start
        if total_time > 5.0:
            self.logger.warning(
                f"[SlowScan] 本次扫描耗时 {total_time:.1f}s "
                f"(fetch={fetch_time:.1f}s, process={process_time:.1f}s)"
            )

    def get_perf_stats(self) -> Dict[str, Any]:
        """获取性能统计"""
        scans = self._perf_stats['total_scans']
        if scans == 0:
            return {'scans': 0}
        return {
            'scans': scans,
            'avg_fetch_ms': round(self._perf_stats['total_fetch_time'] / scans * 1000, 1),
            'avg_process_ms': round(self._perf_stats['total_process_time'] / scans * 1000, 1),
            'max_fetch_ms': round(self._perf_stats['max_fetch_time'] * 1000, 1),
            'max_process_ms': round(self._perf_stats['max_process_time'] * 1000, 1),
        }

    def run(self) -> None:
        """主循环 - 支持事件驱动或传统轮询模式

        事件驱动模式（推荐）：
        - 启用 enable_event_mode() 后，run() 启动 PushAdapter 并等待事件
        - 无 time.sleep 阻塞，收到行情变化立即触发策略
        - 自适应频率（1-30秒），交易活跃时自动加速

        传统轮询模式（兼容）：
        - 未启用事件模式时，保持原有 time.sleep 轮询逻辑
        """
        self.logger.info(">>> Stock Radar Monitor V2 已启动 <<<")
        self.logger.info(f"监控股票: {len(self.targets)} 只")

        # 启动健康检查服务器
        if self._health_server:
            try:
                self._health_server.start_in_background()
            except Exception as e:
                self.logger.error(f"启动健康检查服务器失败: {e}")

        self.notifier.send_system_alert(
            "监控启动",
            f"Radar Monitor V2 已上线\n• 监控标的: {len(self.targets)} 只\n"
            f"• 模式: {'事件驱动' if self._event_mode else '轮询'}\n"
            f"• 功能: 价格触发 | ML预测 | RL策略"
        )

        # 事件驱动模式
        if self._event_mode and self._push_adapter:
            self._run_event_mode()
            return

        # 传统轮询模式（兼容旧代码）
        self._run_poll_mode()

    def _on_datasource_circuit_open(self, source_name: str, failure_count: int) -> None:
        """数据源熔断回调 - 发送异常告警"""
        self.logger.error(f"数据源 [{source_name}] 熔断，连续失败 {failure_count} 次")
        if not self._datasource_circuit_alert_enabled():
            return
        try:
            self.notifier.send_datasource_alert(
                source_name=source_name,
                failure_count=failure_count,
                status="abnormal"
            )
        except Exception as e:
            self.logger.error(f"发送数据源异常告警失败: {e}")

    def _on_datasource_circuit_close(self, source_name: str) -> None:
        """数据源恢复回调 - 发送恢复通知"""
        self.logger.info(f"数据源 [{source_name}] 恢复，熔断器关闭")
        if not self._datasource_circuit_alert_enabled():
            return
        try:
            self.notifier.send_datasource_alert(
                source_name=source_name,
                failure_count=0,
                status="recovered"
            )
        except Exception as e:
            self.logger.error(f"发送数据源恢复通知失败: {e}")

    def _datasource_circuit_alert_enabled(self) -> bool:
        return self._datasource_circuit_alert

    def stop(self) -> None:
        """停止监控（优雅退出，保存状态）"""
        self.running = False

        # 停止事件驱动组件
        if self._event_mode:
            if self._push_adapter:
                self._push_adapter.stop()
            if self._event_bus:
                try:
                    self._event_bus.shutdown()
                except (RuntimeError, OSError):
                    # 关闭阶段事件总线清理失败可忽略  # 安全忽略
                    pass

        # 保存阵地状态
        if hasattr(self, '_battlefield'):
            try:
                self._battlefield.save_all(self)
                self.logger.info("[MonitorEngine] stop() 阵地状态已保存")
            except Exception as e:
                self.logger.warning(f"[MonitorEngine] stop() 保存状态失败: {e}")

        # 强制刷新冷却状态
        if hasattr(self, 'cooldown') and hasattr(self.cooldown, 'flush'):
            try:
                self.cooldown.flush()
            except Exception as e:
                self.logger.warning(f"冷却状态 flush 失败: {e}")

        # 关闭并发行情获取器的线程池
        if hasattr(self.market, 'shutdown'):
            self.market.shutdown()

        # 关闭健康检查服务器（dispatch 回后台线程的 loop 才能正确 cleanup aiohttp 资源）
        if self._health_server:
            try:
                self._health_server.stop_in_background()
            except Exception as e:
                self.logger.error(f"关闭健康检查服务器失败: {e}")

        # 关闭战场状态管理器
        if hasattr(self, '_battlefield') and self._battlefield:
            try:
                self._battlefield.close_all_connections()
            except Exception as e:
                self.logger.error(f"关闭战场状态管理器失败: {e}")

        # 输出性能统计
        if self._perf_stats['total_scans'] > 0:
            avg_fetch = self._perf_stats['total_fetch_time'] / self._perf_stats['total_scans']
            avg_process = self._perf_stats['total_process_time'] / self._perf_stats['total_scans']
            self.logger.info(
                f"[PerfStats] 扫描 {self._perf_stats['total_scans']} 次，"
                f"avg_fetch={avg_fetch:.2f}s, avg_process={avg_process:.2f}s, "
                f"max_fetch={self._perf_stats['max_fetch_time']:.2f}s, "
                f"max_process={self._perf_stats['max_process_time']:.2f}s"
            )
