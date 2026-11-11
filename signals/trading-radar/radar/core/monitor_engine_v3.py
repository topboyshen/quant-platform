"""MonitorEngine V3 - 终极优化版（从 radar_monitor_v3.py 入口脚本拆出）

核心改进相对 V1/V2：
1. DataSourceManager: 智能数据源管理（降级链 + 缓存）
2. AsyncStrategyEngine: 异步策略执行（不阻塞扫描）
3. AlertManager: 智能告警（去重 + 限流 + 分级）
4. ScanScheduler: 自适应扫描（根据市场活跃度调整间隔）
5. 内存优化：批量数据获取后只保留目标股票

拆出原因：
- 入口脚本 radar_monitor_v3.py 之前混合了 class + main + helpers（约 570 行），
  其他模块/测试无法 ``from radar_monitor_v3 import MonitorEngineV3``（会触发整个
  入口脚本副作用 / sys.path hack）。现在引擎类下沉到包内，入口脚本只保留 wiring。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from radar.alert.alert_manager import AlertItem, AlertLevel
from radar.core.monitor_engine_v2 import MonitorConfig, TargetStock
from radar.core.portfolio_loader import load_portfolio_into_tracker
from radar.core.services import MonitorEngineServices
from radar.observability import trace_span
from radar.strategy.rl_strategy_v2 import PortfolioTracker


class MonitorEngineV3:
    """监控引擎 V3 - 终极优化版"""

    ALERT_FAILURE_BACKOFF = 30.0

    def __init__(
        self,
        config: MonitorConfig,
        logger: logging.Logger,
        services: Optional[MonitorEngineServices] = None,
    ):
        self.config = config
        self.logger = logger
        self.running = True

        self.services = services or MonitorEngineServices.build(config, logger)
        self.datasource = self.services.datasource
        self.strategy_engine = self.services.strategy_engine
        self.scheduler = self.services.scheduler
        self.notifier = self.services.notifier
        self.alert_manager = self.services.alert_manager
        self.cooldown = self.services.cooldown
        self._accuracy_tracker = self.services.accuracy_tracker
        self._health = self.services.health
        self._health_server = self.services.health_server

        self._risk_control = self.services.risk_control
        self._risk_state = None

        # 监控目标
        self.targets: list = []

        # 阵地状态
        self._battlefield = self.services.battlefield
        self._battlefield.restore_all(self)

        # 组合持仓（兼容 Battlefield 恢复）
        self._portfolio = PortfolioTracker(initial_cash=config.initial_cash)
        load_portfolio_into_tracker(self._portfolio, "portfolio.json", self.logger)

        # 性能指标
        self._perf_stats = {
            'total_scans': 0,
            'total_fetch_time': 0.0,
            'total_process_time': 0.0,
            'max_fetch_time': 0.0,
            'max_process_time': 0.0,
        }

        # 连续失败
        self._consecutive_failures = 0
        self._failure_alert_sent = False
        self._failure_alert_retry_after = 0.0
        self._macro_alert_retry_after = 0.0

    def add_target(self, target: TargetStock) -> None:
        """添加监控目标"""
        self.targets.append(target)
        self._health.update_stock_count(len(self.targets))
        self.logger.info(
            f"添加监控: {target.name} ({target.code}) - 触发价: {target.trigger_price}"
        )

    def _fetch_all_data(self) -> tuple:
        """获取所有目标股票数据"""
        stocks = [(t.code, t.name) for t in self.targets]
        start = time.time()
        try:
            all_data = self.datasource.fetch_multiple(stocks)
            fetch_time = time.time() - start
            return all_data, len(all_data), fetch_time
        except Exception as e:
            self.logger.error(f"获取行情数据异常: {e}")
            return {}, 0, 0.0

    def _process_target(self, target: TargetStock, data: dict) -> list:
        """处理单个目标"""
        alerts = []
        if self.cooldown.is_in_cooldown(target.code, 'price', self.config.cooldown_minutes):
            self.logger.debug(f"{target.name}({target.code}) price 告警冷却中，仍继续评估 ML/RL")

        try:
            price = float(data.get('price', 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            self.logger.warning(f"{target.name}({target.code}) 行情价格无效: {data.get('price')!r}，跳过策略评估")
            return alerts

        if data.get('fresh') is False or data.get('stale'):
            self.logger.warning(f"{target.name}({target.code}) 使用非实时缓存数据，跳过策略告警")
            return alerts

        results = self.strategy_engine.evaluate_all(target, data, self._portfolio)
        for result in results:
            if result.triggered:
                if (
                    result.strategy_type == 'price'
                    and self.cooldown.is_in_cooldown(target.code, 'price', self.config.cooldown_minutes)
                ):
                    continue
                level = (
                    AlertLevel.CRITICAL
                    if result.strategy_type == 'price'
                    else AlertLevel.WARNING
                )
                item = AlertItem(
                    level=level,
                    title=f"{target.name} {result.strategy_type.upper()} 触发",
                    content=str(result.signal_info),
                    stock_code=target.code,
                    stock_name=target.name,
                    alert_type=result.strategy_type,
                    signal_info=result.signal_info or {},
                    on_delivery=lambda delivered, code=target.code,
                    alert_type=result.strategy_type: self._record_business_cooldown(
                        code,
                        alert_type,
                        delivered,
                    ),
                )
                if self.alert_manager.submit(item):
                    alerts.append(item)
            elif getattr(result, 'risk_blocked', False):
                # 信号被宏观风控拦截，记录日志但不发送告警
                self.logger.info(
                    "[RiskControl] %s(%s) %s 信号被拦截: %s",
                    target.name, target.code, result.strategy_type,
                    result.signal_info.get('risk_message', '宏观风控拦截')
                )
        return alerts

    def _record_business_cooldown(
        self,
        code: str,
        alert_type: str,
        delivered: bool,
    ) -> None:
        """Persist business cooldown only after confirmed delivery."""
        if delivered:
            self.cooldown.record_alert(code, alert_type)
        else:
            self.logger.warning(
                "告警未送达，不写入业务冷却: %s/%s",
                code,
                alert_type,
            )

    def _process_all_targets(self, all_data: dict) -> bool:
        """处理所有目标，返回是否有错误"""
        has_errors = False
        for target in self.targets:
            try:
                data = all_data.get(target.code)
                if data:
                    self._process_target(target, data)
                else:
                    self.logger.warning(
                        f"{target.name}({target.code}) 数据缺失，跳过"
                    )
                    has_errors = True
            except Exception as e:
                self.logger.error(f"处理 {target.name} 出错: {e}")
                has_errors = True
        return has_errors

    def scan_once(self) -> None:
        """执行一次扫描"""
        if not self.targets:
            self._health.record_scan(0, success=False)
            return

        with trace_span("monitor.scan_once", target_count=len(self.targets)):
            self._scan_once_impl()

    def _scan_once_impl(self) -> None:
        # 同步市场状态到健康监控（用于 /health 端点和 Prometheus metrics）
        market_open = (
            self.scheduler.is_trading_day() and self.scheduler.is_trading_time()
        )
        self._health.set_market_open(market_open)

        # ---- 宏观风险检查 ----
        self._risk_state = self._risk_control.check_with_fetch()
        # 保存状态到文件供 Dashboard 读取
        self._risk_control.save_state(self._risk_state)
        if self._risk_state.level != "ok":
            self.logger.warning("[RiskControl] %s", self._risk_state.message)
            # 发送系统级告警（通过 notifier 直接发送，不经过 alert_manager 冷却）
            # 仅在交易时间推送，避免非交易时段打扰
            if (
                self._risk_control.should_alert_now(self._risk_state)
                and self.scheduler.is_trading_time()
                and time.time() >= self._macro_alert_retry_after
            ):
                try:
                    delivered = bool(
                        self.notifier.send_system_alert(
                            "宏观风险告警",
                            self._risk_state.message,
                        )
                    )
                    if delivered:
                        self._risk_control.mark_alert_sent(self._risk_state)
                        self._macro_alert_retry_after = 0.0
                    else:
                        self._macro_alert_retry_after = (
                            time.time() + self.ALERT_FAILURE_BACKOFF
                        )
                        self.logger.warning(
                            "宏观风险告警未送达，%.0fs 后再试",
                            self.ALERT_FAILURE_BACKOFF,
                        )
                except Exception as e:
                    self.logger.error(f"发送风险告警失败: {e}")
                    self._macro_alert_retry_after = (
                        time.time() + self.ALERT_FAILURE_BACKOFF
                    )

        self._perf_stats['total_scans'] += 1
        scan_start = time.time()

        all_data, success_count, fetch_time = self._fetch_all_data()
        self._perf_stats['total_fetch_time'] += fetch_time
        self._perf_stats['max_fetch_time'] = max(
            self._perf_stats['max_fetch_time'], fetch_time
        )

        if success_count == 0:
            self._handle_total_failure()
            return

        self._reset_failure_state()

        # 非交易时段：仅记录健康心跳，不评估策略，避免基于陈旧/过期数据触发告警
        process_start = time.time()
        if not market_open:
            self.logger.debug("[scan_once] 非交易时段，跳过策略评估，仅记录心跳")
            has_errors = False
        else:
            has_errors = self._process_all_targets(all_data)
        process_time = time.time() - process_start
        self._perf_stats['total_process_time'] += process_time
        self._perf_stats['max_process_time'] = max(
            self._perf_stats['max_process_time'], process_time
        )

        self.alert_manager.flush()

        # 阵地状态持久化（按频率写回，减少 I/O）
        self._scan_count_since_save = getattr(self, '_scan_count_since_save', 0) + 1
        if self._scan_count_since_save >= 5:
            self._scan_count_since_save = 0
            try:
                self._battlefield.save_all(self)
            except Exception as e:
                self.logger.warning(f"阵地状态保存失败: {e}")

        self._health.record_scan(
            stock_count=len(self.targets),
            success=not has_errors,
        )

        total_time = time.time() - scan_start
        if total_time > 5.0:
            self.logger.warning(
                f"[SlowScan] 本次扫描耗时 {total_time:.1f}s "
                f"(fetch={fetch_time:.1f}s, process={process_time:.1f}s)"
            )

        self.scheduler.update_activity(list(all_data.values()))
        self.scheduler.record_scan()

    def _handle_total_failure(self) -> None:
        """处理全部失败"""
        self._consecutive_failures += 1
        self.logger.warning(
            f"本次扫描未获取到任何股票数据"
            f"（连续失败: {self._consecutive_failures}/3）"
        )

        if (
            self._consecutive_failures >= 3
            and not self._failure_alert_sent
            and time.time() >= self._failure_alert_retry_after
        ):
            try:
                delivered = bool(
                    self.notifier.send_system_alert(
                        "数据获取异常",
                        f"连续 {self._consecutive_failures} 次扫描失败，请检查数据源",
                    )
                )
                if delivered:
                    self._failure_alert_sent = True
                    self._failure_alert_retry_after = 0.0
                else:
                    self._failure_alert_retry_after = (
                        time.time() + self.ALERT_FAILURE_BACKOFF
                    )
                    self.logger.warning(
                        "数据失败告警未送达，%.0fs 后再试",
                        self.ALERT_FAILURE_BACKOFF,
                    )
            except Exception as e:
                self.logger.error(f"发送失败告警失败: {e}")
                self._failure_alert_retry_after = (
                    time.time() + self.ALERT_FAILURE_BACKOFF
                )

        self._health.record_scan(stock_count=len(self.targets), success=False)

    def _reset_failure_state(self) -> None:
        """重置失败状态"""
        if self._consecutive_failures > 0:
            self.logger.info(
                f"数据获取恢复，重置连续失败计数（之前: {self._consecutive_failures}次）"
            )
            self._consecutive_failures = 0
            self._failure_alert_sent = False
            self._failure_alert_retry_after = 0.0

    def run(self) -> None:
        """主循环"""
        self.logger.info(">>> Stock Radar Monitor V3 已启动 <<<")
        self.logger.info(f"监控股票: {len(self.targets)} 只")
        self.logger.info(
            f"扫描间隔: 自适应 ({self.scheduler.INTERVAL_NORMAL}s 基准)"
        )
        self.logger.info(
            f"宏观风控: VIX>{self._risk_control.vix_critical} | "
            f"美债10Y>={self._risk_control.us_10y_block} | "
            f"原油>{self._risk_control.crude_oil_block}"
        )

        self.notifier.send_system_alert(
            "监控启动",
            f"Radar Monitor V3 已上线\n• 监控标的: {len(self.targets)} 只\n"
            f"• 模式: 自适应轮询\n• 功能: 价格触发 | ML预测 | RL策略 | 智能告警\n"
            f"• 风控: VIX/美债/原油宏观风险监控已启用",
        )

        try:
            while self.running:
                try:
                    if not self.scheduler.should_scan():
                        time.sleep(1)
                        continue

                    self.scan_once()

                    interval = self.scheduler.get_interval()
                    time.sleep(
                        max(1, interval - (time.time() - self.scheduler.last_scan_time))
                    )

                except KeyboardInterrupt:
                    self.logger.info("正在停止...")
                    self.running = False
                except Exception as e:
                    self.logger.error(f"主循环异常: {e}")
                    time.sleep(10)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """清理资源"""
        try:
            self._battlefield.save_all(self)
            self.logger.info("阵地状态已保存")
        except Exception as e:
            self.logger.warning(f"保存状态失败: {e}")

        try:
            self.cooldown.flush()
        except Exception as e:
            self.logger.debug(f"刷新冷却状态: {e}")

        try:
            self.alert_manager.flush()
        except Exception as e:
            self.logger.debug(f"刷新告警: {e}")

        try:
            self.datasource.shutdown()
        except Exception as e:
            self.logger.debug(f"关闭数据源: {e}")

        try:
            self.strategy_engine.shutdown()
        except Exception as e:
            self.logger.debug(f"关闭策略引擎: {e}")

        try:
            self.alert_manager.shutdown()
        except Exception as e:
            self.logger.debug(f"关闭告警管理器: {e}")

        # 停止健康检查 HTTP 服务（dispatch 回后台 loop 才能正确 cleanup aiohttp 资源）
        if self._health_server is not None:
            try:
                self._health_server.stop_in_background()
            except Exception as e:
                self.logger.debug(f"停止健康检查服务: {e}")

        if self._perf_stats['total_scans'] > 0:
            avg_fetch = self._perf_stats['total_fetch_time'] / self._perf_stats['total_scans']
            avg_process = self._perf_stats['total_process_time'] / self._perf_stats['total_scans']
            self.logger.info(
                f"[PerfStats] 扫描 {self._perf_stats['total_scans']} 次，"
                f"avg_fetch={avg_fetch:.2f}s, avg_process={avg_process:.2f}s"
            )

        self.logger.info("监控已停止")

    def stop(self) -> None:
        """停止监控"""
        self.running = False
