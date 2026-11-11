"""
增强型监控引擎 - MonitorEngine V2
核心改进：
1. 使用 market_data_v2 + cooldown_v2 + concurrent_v2
2. 扫描性能指标采集（fetch_time, process_time）
3. 批量数据处理优化（减少 Battlefield 写频率）
4. 内存泄漏防护（定期清理、对象计数）
5. 优雅退出增强（try/finally 保证状态保存）
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from radar.alert.batcher import AlertItem
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
from radar.data.market_data_concurrent_v2 import ConcurrentMarketDataFetcher
from radar.data.market_data_v2 import MarketTimeChecker
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
    strategy_tag: str = ""
    market_cap: float = 0.0  # 流通市值（亿元），0 表示未设置
    tags: list = None  # 标签列表，如 ['growth', 'resource', 'small_cap']


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
    ml_min_history_bars: int = 30
    ml_enabled: bool = True
    rl_enabled: bool = True
    ml_model_dir: str = "models"
    rl_model_dir: str = "rl_models"
    rl_mode: str = "confirm"
    initial_cash: float = 100000.0
    health_host: str = "127.0.0.1"
    health_port: int = 8080
    batch_alerts: bool = True
    alert_priorities: Optional[Dict] = None
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
    监控引擎 V2 - 增强版实时监控

    核心改进：
    - 数据源降级链重构（v2 数据层）
    - 冷却管理器内存缓存层（v2）
    - 批量缓存行级索引（O(1) 查找）
    - 性能指标自动采集
    - Battlefield 写频率优化（每 N 次扫描写一次）
    """

    # Battlefield 写回频率（每 N 次扫描写一次）
    BATTLEFIELD_WRITE_INTERVAL = 5

    def __init__(self, config: MonitorConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.running = True

        # 初始化各层组件（使用 V2 版本）
        self.cooldown = AlertCooldownManager(config.cache_file, logger)
        self.notifier = TelegramNotifier(config.bot_token, config.chat_id, logger)
        self.market = ConcurrentMarketDataFetcher(
            logger=logger,
            max_workers=5,
            timeout=15.0,
            on_circuit_open=self._on_datasource_circuit_open,
            on_circuit_close=self._on_datasource_circuit_close
        )
        self.time_checker = MarketTimeChecker(config.market_hours, logger)

        # 监控目标列表
        self.targets: List[TargetStock] = []

        # 策略实例缓存
        self._price_strategies: Dict[str, PriceTriggerStrategy] = {}
        self._ml_strategies: Dict[str, MLStrategy] = {}
        self._rl_strategies: Dict[str, RLStrategyV2] = {}

        # 组合持仓追踪
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

        # 健康检查服务器
        self._health_server = self._init_health_server()

        # 告警批处理器
        self._alert_batcher = self._init_alert_batcher()
        self._target_pipeline: Tuple[Any, ...] = TARGET_PROCESS_STEPS

        # 阵地状态持久化
        self._battlefield = BattlefieldStateManager(logger=self.logger)
        self._battlefield.restore_all(self)
        self.logger.info("[MonitorEngine] 阵地状态已恢复（如有）")

        # Battlefield 写计数器
        self._scan_count_since_save = 0

        # 事件驱动模式
        self._event_bus: Optional[Any] = None
        self._push_adapter: Optional[Any] = None
        self._event_mode = False

        # 性能指标
        self._perf_stats = {
            'total_scans': 0,
            'total_fetch_time': 0.0,
            'total_process_time': 0.0,
            'max_fetch_time': 0.0,
            'max_process_time': 0.0,
        }

    def _load_real_portfolio(self, portfolio_path: str = "portfolio.json") -> None:
        """从配置文件加载真实持仓"""
        load_portfolio_into_tracker(self._portfolio, portfolio_path, self.logger)

    def add_target(self, target: TargetStock) -> None:
        """添加监控目标"""
        self.targets.append(target)
        self._create_strategies_for_target(target)
        self._health.update_stock_count(len(self.targets))

    def process_target(
        self, target: TargetStock, data: Optional[Dict[str, Any]] = None
    ) -> List[AlertItem]:
        """处理单个目标"""
        if data is None:
            data = self.market.fetch_single(target.code, target.name)

        if not data:
            self.logger.warning(f"无法获取 {target.name} 数据")
            return []

        # Pydantic 校验（可选）
        if os.getenv("RADAR_SKIP_LIVE_QUOTE_PYDANTIC", "").strip().lower() not in (
            "1", "true", "yes",
        ):
            try:
                from pydantic import ValidationError

                from radar.schemas.live_quote import validate_live_quote_dict
                data = validate_live_quote_dict(data)
            except ValidationError as e:
                self.logger.warning(
                    "实盘行情未通过 Pydantic 校验 %s %s: %s",
                    target.code, target.name, e,
                )
                return []

        ctx = TargetProcessContext(
            target=target,
            data=data,
            current_price=data["price"],
        )
        return run_target_pipeline(self, ctx, steps=self._target_pipeline)

    def _fetch_all_data(self) -> Tuple[Dict[str, Any], int]:
        """并发获取所有目标股票数据"""
        stocks = [(t.code, t.name) for t in self.targets]
        try:
            all_data = self.market.fetch_multiple(stocks)
            return all_data, len(all_data)
        except Exception as e:
            self.logger.error(f"获取行情数据异常: {e}")
            return {}, 0

    def _handle_total_failure(self) -> None:
        """处理全部数据获取失败"""
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
        """串行处理所有目标"""
        has_errors = False
        for target in self.targets:
            try:
                data = all_data.get(target.code)
                if data:
                    alerts = self.process_target(target, data)
                    for alert in alerts:
                        self._alert_batcher.add(alert)
                else:
                    self.logger.warning(f"{target.name}({target.code}) 数据缺失，跳过")
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

        self._reset_failure_state()

        # 2. 串行处理告警判断
        process_start = time.time()
        has_errors = self._process_targets_batch(all_data)
        process_time = time.time() - process_start
        self._perf_stats['total_process_time'] += process_time
        self._perf_stats['max_process_time'] = max(self._perf_stats['max_process_time'], process_time)

        # 3. 发送合并后的批次告警
        self._flush_alert_batch()

        # 4. 阵地状态持久化（按频率写回）
        self._scan_count_since_save += 1
        if self._scan_count_since_save >= self.BATTLEFIELD_WRITE_INTERVAL:
            self._scan_count_since_save = 0
            if hasattr(self, '_battlefield'):
                try:
                    self._battlefield.save_all(self)
                except Exception as e:
                    self.logger.warning(f"[MonitorEngine] 阵地状态保存失败: {e}")

        # 5. 可观测性刷新
        try:
            from radar.core.trading_observability import refresh_trading_observability
            refresh_trading_observability(self)
        except (RuntimeError, OSError):
            pass

        # 6. 记录健康状态
        self._health.record_scan(
            stock_count=len(self.targets),
            success=not has_errors
        )

        # 7. 慢扫描告警（fetch + process > 5s）
        total_time = time.time() - scan_start
        if total_time > 5.0:
            self.logger.warning(
                f"[SlowScan] 本次扫描耗时 {total_time:.1f}s "
                f"(fetch={fetch_time:.1f}s, process={process_time:.1f}s)"
            )

    def run(self) -> None:
        """主循环"""
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

        # 传统轮询模式
        self._run_poll_mode()

    def _on_datasource_circuit_open(self, source_name: str, failure_count: int) -> None:
        """数据源熔断回调"""
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
        """数据源恢复回调"""
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
        """停止监控（优雅退出）"""
        self.running = False

        # 停止事件驱动组件
        if self._event_mode:
            if self._push_adapter:
                self._push_adapter.stop()
            if self._event_bus:
                try:
                    self._event_bus.shutdown()
                except (RuntimeError, OSError):
                    pass

        # 强制保存阵地状态
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

        # 关闭并发行情获取器
        if hasattr(self.market, 'shutdown'):
            self.market.shutdown()

        # 关闭健康检查服务器（dispatch 回后台线程的 loop 才能正确 cleanup aiohttp 资源）
        if self._health_server:
            try:
                self._health_server.stop_in_background()
            except Exception as e:
                self.logger.error(f"关闭健康检查服务器失败: {e}")

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
