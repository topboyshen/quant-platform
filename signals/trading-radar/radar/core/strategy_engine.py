"""
策略引擎 V3 - 异步策略执行
职责：
1. 策略并行执行（不阻塞主循环）
2. 模型懒加载
3. 预测结果缓存
4. 批量推理优化
"""

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from radar.strategy.base import PriceTriggerStrategy
from radar.strategy.ml_strategy import MLStrategy
from radar.strategy.rl_strategy_v2 import RLStrategyV2


@dataclass
class StrategyResult:
    """策略执行结果"""
    strategy_type: str  # price, ml, rl
    triggered: bool
    signal_info: Dict[str, Any]
    latency_ms: float
    cached: bool = False
    risk_blocked: bool = False  # 新增：是否被宏观风控拦截


class AsyncStrategyEngine:
    """
    异步策略引擎 V3

    核心改进：
    - 价格策略同步执行（简单，不耗时）
    - ML/RL策略异步执行（线程池，不阻塞扫描）
    - 模型懒加载（首次触发时才加载）
    - 预测结果缓存（30秒TTL）
    """

    PREDICTION_CACHE_TTL = 30.0  # 预测结果缓存30秒

    def __init__(self, config: Any, logger: logging.Logger, max_workers: int = 3):
        self.config = config
        self.logger = logger
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="strategy_")

        # 策略缓存
        self._price_strategies: Dict[str, PriceTriggerStrategy] = {}
        self._ml_strategies: Dict[str, MLStrategy] = {}
        self._rl_strategies: Dict[str, RLStrategyV2] = {}

        # 预测结果缓存
        self._prediction_cache: Dict[str, Dict] = {}
        self._cache_lock = False  # 简化：单线程访问

        # 模型加载状态
        self._models_loaded = False

        # 宏观风控状态缓存（避免每次扫描重复读取）
        self._risk_state: Optional[Dict] = None
        self._risk_state_ts: float = 0.0
        self._risk_state_ttl: float = 60.0  # 60秒缓存

        # 动态触发价管理器
        self._adaptive_trigger: Optional[Any] = None

    def _ensure_adaptive_trigger(self) -> None:
        """懒加载动态触发价管理器"""
        if self._adaptive_trigger is None:
            try:
                from radar.core.adaptive_trigger import AdaptiveTriggerManager
                self._adaptive_trigger = AdaptiveTriggerManager(logger_=self.logger)
                self.logger.info("[AdaptiveTrigger] 动态触发价管理器已启用")
            except Exception as e:
                self.logger.warning(f"[AdaptiveTrigger] 初始化失败: {e}")

    def _get_price_strategy(self, target: Any) -> PriceTriggerStrategy:
        """获取或创建价格策略（同步）"""
        key = target.code
        if key not in self._price_strategies:
            self._ensure_adaptive_trigger()
            self._price_strategies[key] = PriceTriggerStrategy(
                trigger_price=target.trigger_price,
                direction=target.direction,
                adaptive_manager=self._adaptive_trigger,
            )
        return self._price_strategies[key]

    def _get_ml_strategy(self, target: Any) -> Optional[Any]:
        """获取或创建ML策略（懒加载）"""
        if not target.ml_enabled:
            return None

        key = target.code
        if key not in self._ml_strategies:
            try:
                from radar.strategy.ml_strategy import MLStrategy
                self._ml_strategies[key] = MLStrategy(
                    code=target.code,
                    direction=target.direction,
                    trigger_price=target.trigger_price,
                    model_dir=self.config.ml_model_dir,
                    threshold=self.config.ml_threshold,
                    logger=self.logger
                )
                self.logger.debug(f"ML策略已加载: {target.name}")
            except Exception as e:
                self.logger.warning(f"ML策略加载失败 {target.name}: {e}")
                return None

        return self._ml_strategies[key]

    def _get_rl_strategy(self, target: Any) -> Optional[RLStrategyV2]:
        """获取或创建RL策略（懒加载）"""
        if not target.rl_enabled:
            return None

        key = target.code
        if key not in self._rl_strategies:
            try:
                self._rl_strategies[key] = RLStrategyV2(
                    code=target.code,
                    direction=target.direction,
                    trigger_price=target.trigger_price,
                    model_dir=self.config.rl_model_dir,
                    universal=True,
                    use_real_account=True,
                    logger=self.logger,
                )
                self.logger.debug(f"RL策略已加载: {target.name}")
            except Exception as e:
                self.logger.warning(f"RL策略加载失败 {target.name}: {e}")
                return None

        return self._rl_strategies[key]

    def _inject_real_account(self, strategy: RLStrategyV2, target: Any, data: Dict, portfolio: Any) -> bool:
        """Inject real portfolio/account state into RLStrategyV2 when available."""
        if portfolio is None:
            return False
        try:
            price = float(data.get('price', 0) or 0)
            if hasattr(portfolio, "update_price") and price > 0:
                portfolio.update_price(target.code, price)
            if hasattr(portfolio, "get_account_state"):
                account = portfolio.get_account_state(target.code)
                strategy.set_real_account(account)
                return True
        except Exception as e:
            self.logger.warning("注入 RL 真实账户状态失败 %s: %s", getattr(target, 'code', ''), e)
        return False

    # ------------------------------------------------------------------ 宏观风控集成
    def _load_risk_state(self) -> Optional[Dict]:
        """加载宏观风控状态（优先从文件读取，带缓存）"""
        now = time.time()
        if self._risk_state is not None and (now - self._risk_state_ts) < self._risk_state_ttl:
            return self._risk_state

        try:
            from radar.risk.macro_risk_control import MacroRiskControl
            state = MacroRiskControl.load_state_from_file()
            if state is not None:
                self._risk_state = state
                self._risk_state_ts = now
                return state
        except Exception as e:
            self.logger.debug("[RiskControl] 加载风控状态失败: %s", e)
        return None

    def _is_growth_stock(self, target: Any) -> bool:
        """判断标的是否属于'成长'或'高估值'标签

        优先级：
        1. target.tags 包含 'growth' / '高估值' / '创业板' / '科创板'
        2. 代码前缀：300/301/688 默认为成长股（但大盘蓝筹除外）
        3. 市盈率 > 50 视为高估值（如有数据）
        """
        # 标签匹配
        tags = getattr(target, 'tags', []) or []
        tag_set = {t.lower() if isinstance(t, str) else '' for t in tags}
        growth_tags = {
            'growth', '高估值', '创业板', '科创板', '科技',
            'ai', 'ai硬件', 'ai硬件链', 'cpo', '光模块', '算力', '通信', '硬科技',
        }
        if tag_set & growth_tags:
            return True

        name = str(getattr(target, 'name', '')).lower()
        ai_hardware_keywords = (
            'cpo', '光模块', 'ai硬件', '算力', '中际旭创', '新易盛',
            '天孚通信', '源杰科技', '剑桥科技', '光迅科技', '华工科技',
        )
        if any(keyword in name for keyword in ai_hardware_keywords):
            return True

        # 代码前缀匹配（创业板/科创板）
        code = str(getattr(target, 'code', ''))
        if code.startswith(('300', '301', '688')):
            # 大盘蓝筹例外：市值 >= 500亿 不算成长股（如东方财富、同花顺）
            cap = getattr(target, 'market_cap', 0)
            if cap > 0 and cap >= 500:
                return False
            return True

        # 市盈率判断（如有）
        pe = getattr(target, 'pe_ttm', None)
        if pe is not None and pe > 50:
            return True

        return False

    def _is_small_cap(self, target: Any) -> bool:
        """判断是否为小微盘股（流通市值 < 200亿）

        优先级：
        1. target.market_cap 已设置且 < 200亿
        2. target.tags 包含 'small_cap' / 'micro' / '微盘' / '小盘'
        3. 代码前缀：688/300/301/920/430/83/87/88 默认视为小盘/微盘
        4. 名称包含典型微盘特征词
        """
        # 1. 市值数据
        cap = getattr(target, 'market_cap', 0)
        if cap > 0 and cap < 200:
            return True

        # 2. 标签匹配
        tags = getattr(target, 'tags', []) or []
        tag_set = {t.lower() if isinstance(t, str) else '' for t in tags}
        small_tags = {'small_cap', 'micro', '微盘', '小盘', '小微', 'st', 'st股'}
        if tag_set & small_tags:
            return True

        # 3. 代码前缀（北交所/新三板/创业板/科创板 = 小盘/微盘特征）
        code = str(getattr(target, 'code', ''))
        if code.startswith(('688', '920', '430', '83', '87', '88')):
            return True
        # 创业板/科创板且市值未明确标注的，视为小盘
        if code.startswith(('300', '301')):
            # 有明确市值数据时，以市值为准（>=200亿不算小盘）
            cap = getattr(target, 'market_cap', 0)
            if cap > 0 and cap >= 200:
                return False
            return True

        # 4. 名称特征
        name = str(getattr(target, 'name', '')).lower()
        micro_keywords = ('st', '退市', '北交所', '新三板')
        if any(kw in name for kw in micro_keywords):
            return True

        return False

    def _is_resource_stock(self, target: Any) -> bool:
        """判断是否为周期资源类标的

        优先级：
        1. target.tags 包含 'resource' / '周期' / '资源' / '有色' / '黄金' / '煤炭' / '钢铁' / '化工'
        2. 名称包含资源类关键词
        """
        tags = getattr(target, 'tags', []) or []
        tag_set = {t.lower() if isinstance(t, str) else '' for t in tags}
        resource_tags = {
            'resource', '周期', '资源', '有色', '黄金', '煤炭', '钢铁',
            '化工', '石油', '天然气', '锂', '钴', '镍', '稀土', '铜', '铝',
            '矿业', '能源', '大宗商品', 'commodity',
        }
        if tag_set & resource_tags:
            return True

        name = str(getattr(target, 'name', '')).lower()
        resource_keywords = (
            '有色', '黄金', '煤炭', '钢铁', '化工', '石油', '天然气',
            '锂', '钴', '镍', '稀土', '铜', '铝', '矿业', '能源',
            '资源', '周期', '大宗商品',
        )
        if any(kw in name for kw in resource_keywords):
            return True

        return False

    def _is_buy_signal(self, result: StrategyResult, target: Any) -> bool:
        """判断触发结果是否代表新增买入/加仓。"""
        if result.strategy_type == 'price':
            return getattr(target, 'direction', '') == 'above'
        if result.strategy_type == 'ml':
            info = result.signal_info or {}
            prediction = str(info.get('prediction') or info.get('direction') or '').lower()
            if any(token in prediction for token in ('下破', 'below', '卖', '减仓')):
                return False
            if any(token in prediction for token in ('上破', 'above', '买', '加仓')):
                return True
            return getattr(target, 'direction', '') == 'above'
        if result.strategy_type == 'rl':
            action = str((result.signal_info or {}).get('action', '')).lower()
            return action in ('open', 'add', 'buy')
        return False

    def _apply_risk_filter(
        self,
        results: List[StrategyResult],
        target: Any,
        risk_state: Optional[Dict],
    ) -> List[StrategyResult]:
        """应用宏观风控过滤逻辑

        规则：
        1. CRITICAL (vix > 17.5): 拦截所有大仓位买入信号，或强制降级权重
        2. GROWTH_BLOCK (us_10y >= 4.55 或 crude_oil > 88.5): 拦截成长股/高估值标的买入
        3. OK: 放行所有信号
        """
        if risk_state is None:
            return results

        level = risk_state.get('level', 'ok')
        if level == 'ok':
            return results

        filtered = []
        for r in results:
            # 只处理触发买入的信号
            if not r.triggered:
                filtered.append(r)
                continue

            # 只拦截新增买入/加仓信号，卖出/减仓信号必须放行
            if not self._is_buy_signal(r, target):
                filtered.append(r)
                continue

            if level == 'critical':
                # 非线性风险熔断：拦截所有大仓位买入，或强制降级
                # 策略：将所有买入信号的 triggered 改为 False，标记为 risk_blocked
                self.logger.warning(
                    "[RiskControl] CRITICAL 拦截 %s 信号（VIX危机模式 %s）: %s",
                    r.strategy_type,
                    risk_state.get('vix', '?'),
                    getattr(target, 'code', '?'),
                )
                r.triggered = False
                r.risk_blocked = True
                r.signal_info = dict(r.signal_info)
                r.signal_info['risk_blocked'] = True
                r.signal_info['risk_level'] = 'critical'
                r.signal_info['risk_message'] = (
                    f"CRITICAL: VIX={risk_state.get('vix', '?')} > 17.5，"
                    "非线性风险熔断，暂停新增买入"
                )

            elif level == 'growth_blocked':
                # 估值天花板压制：拦截成长股/高估值标的 + 小微盘股 + 周期资源股
                blocked_reason = None
                if self._is_growth_stock(target):
                    blocked_reason = "成长股/高估值标的"
                elif self._is_small_cap(target):
                    blocked_reason = "小微盘股（流动性风险）"
                elif self._is_resource_stock(target):
                    blocked_reason = "周期资源股（大宗商品暴跌）"

                if blocked_reason:
                    self.logger.warning(
                        "[RiskControl] GROWTH_BLOCK 拦截 %s 信号（%s | %s）: %s",
                        r.strategy_type,
                        blocked_reason,
                        risk_state.get('message', '')[:50],
                        getattr(target, 'code', '?'),
                    )
                    r.triggered = False
                    r.risk_blocked = True
                    r.signal_info = dict(r.signal_info)
                    r.signal_info['risk_blocked'] = True
                    r.signal_info['risk_level'] = 'growth_blocked'
                    r.signal_info['risk_message'] = (
                        f"GROWTH_BLOCK ({blocked_reason}): {risk_state.get('message', '')[:80]}"
                    )
                else:
                    # 非成长股/非小盘/非资源，放行但附加风险提示
                    r.signal_info = dict(r.signal_info)
                    r.signal_info['risk_note'] = (
                        f"约束环境: {risk_state.get('message', '')[:60]}"
                    )

            filtered.append(r)

        return filtered

    # ------------------------------------------------------------------ 策略检查方法
    def check_price(self, target: Any, data: Dict) -> StrategyResult:
        """价格触发检查（同步）"""
        start = time.time()
        strategy = self._get_price_strategy(target)

        # 先用调整前的有效触发价判断，避免首次跌破时被同步下调吃掉告警
        current_price = float(data.get('price', 0) or 0)
        trigger_before_sync = strategy.trigger_price
        triggered = strategy.is_triggered(data, trigger_before_sync)
        info = strategy.get_signal_info(data, trigger_before_sync) if triggered else {}

        # 判断完成后再同步动态触发价，供下一轮扫描使用
        if current_price > 0:
            trigger_after_sync = strategy.sync_trigger(target.code, current_price)
            if info and trigger_after_sync != trigger_before_sync:
                info['next_trigger_price'] = trigger_after_sync

        return StrategyResult(
            strategy_type='price',
            triggered=triggered,
            signal_info=info,
            latency_ms=(time.time() - start) * 1000
        )

    def check_ml_async(self, target: Any, data: Dict) -> Future:
        """ML预测检查（异步）"""
        return self._executor.submit(self._check_ml_sync, target, data)

    def _check_ml_sync(self, target: Any, data: Dict) -> StrategyResult:
        """ML预测同步实现"""
        start = time.time()

        # 检查缓存
        cache_key = f"{target.code}:ml"
        cached = self._prediction_cache.get(cache_key)
        if cached and (time.time() - cached['time']) < self.PREDICTION_CACHE_TTL:
            return StrategyResult(
                strategy_type='ml',
                triggered=cached['triggered'],
                signal_info=cached['info'],
                latency_ms=(time.time() - start) * 1000,
                cached=True
            )

        strategy = self._get_ml_strategy(target)
        if strategy is None:
            return StrategyResult(
                strategy_type='ml',
                triggered=False,
                signal_info={},
                latency_ms=(time.time() - start) * 1000
            )

        try:
            info = strategy.should_alert(data)
            triggered = info is not None

            # 更新缓存
            self._prediction_cache[cache_key] = {
                'triggered': triggered,
                'info': info or {},
                'time': time.time()
            }

            return StrategyResult(
                strategy_type='ml',
                triggered=triggered,
                signal_info=info or {},
                latency_ms=(time.time() - start) * 1000
            )
        except Exception as e:
            self.logger.error(f"ML预测失败 {target.name}: {e}")
            return StrategyResult(
                strategy_type='ml',
                triggered=False,
                signal_info={'error': str(e)},
                latency_ms=(time.time() - start) * 1000
            )

    def check_rl_async(self, target: Any, data: Dict, portfolio: Any) -> Future:
        """RL策略检查（异步）"""
        return self._executor.submit(self._check_rl_sync, target, data, portfolio)

    def _check_rl_sync(self, target: Any, data: Dict, portfolio: Any) -> StrategyResult:
        """RL策略同步实现"""
        start = time.time()

        mode = getattr(self.config, 'rl_mode', 'confirm')
        if mode == 'off':
            return StrategyResult(
                strategy_type='rl',
                triggered=False,
                signal_info={},
                latency_ms=(time.time() - start) * 1000,
            )

        strategy = self._get_rl_strategy(target)
        if strategy is None:
            return StrategyResult(
                strategy_type='rl',
                triggered=False,
                signal_info={},
                latency_ms=(time.time() - start) * 1000
            )

        try:
            price = float(data.get('price', 0) or 0)
            volume = float(data.get('volume', 0) or 0)
            has_real_account = self._inject_real_account(strategy, target, data, portfolio)

            if mode == 'independent' and not has_real_account:
                return StrategyResult(
                    strategy_type='rl',
                    triggered=False,
                    signal_info={'real_account_required': True, 'mode': mode},
                    latency_ms=(time.time() - start) * 1000,
                )

            if mode == 'independent':
                info = strategy.check_signal_independent(price, volume)
            else:
                info = strategy.check_signal_confirm(
                    price=price,
                    trigger_price=target.trigger_price,
                    direction=target.direction,
                    volume=volume,
                )
            triggered = info is not None

            return StrategyResult(
                strategy_type='rl',
                triggered=triggered,
                signal_info=info or {},
                latency_ms=(time.time() - start) * 1000
            )
        except Exception as e:
            self.logger.error(f"RL评估失败 {target.name}: {e}")
            return StrategyResult(
                strategy_type='rl',
                triggered=False,
                signal_info={'error': str(e)},
                latency_ms=(time.time() - start) * 1000
            )

    def evaluate_all(self, target: Any, data: Dict, portfolio: Any) -> List[StrategyResult]:
        """
        评估所有策略
        价格策略同步，ML/RL异步
        最后应用宏观风控过滤
        """
        results = []

        # 1. 价格策略（同步）
        price_result = self.check_price(target, data)
        results.append(price_result)

        # 2. ML策略（异步）
        ml_future = None
        if target.ml_enabled:
            ml_future = self.check_ml_async(target, data)

        # 3. RL策略（异步，仅在价格触发时评估）
        rl_future = None
        if target.rl_enabled and price_result.triggered:
            rl_future = self.check_rl_async(target, data, portfolio)

        # 等待异步结果
        if ml_future:
            try:
                ml_result = ml_future.result(timeout=5.0)
                results.append(ml_result)
            except Exception as e:
                self.logger.warning(f"ML异步等待超时 {target.name}: {e}")
                results.append(StrategyResult(
                    strategy_type='ml', triggered=False,
                    signal_info={'timeout': True}, latency_ms=5000
                ))

        if rl_future:
            try:
                rl_result = rl_future.result(timeout=5.0)
                results.append(rl_result)
            except Exception as e:
                self.logger.warning(f"RL异步等待超时 {target.name}: {e}")
                results.append(StrategyResult(
                    strategy_type='rl', triggered=False,
                    signal_info={'timeout': True}, latency_ms=5000
                ))

        # 4. 宏观风控过滤（盘前/盘中拦截）
        risk_state = self._load_risk_state()
        if risk_state is not None and risk_state.get('level') != 'ok':
            results = self._apply_risk_filter(results, target, risk_state)

        return results

    def cleanup_cache(self):
        """清理过期缓存"""
        now = time.time()
        expired = [
            k for k, v in self._prediction_cache.items()
            if (now - v['time']) > self.PREDICTION_CACHE_TTL
        ]
        for k in expired:
            del self._prediction_cache[k]

        if expired:
            self.logger.debug(f"清理 {len(expired)} 条过期预测缓存")

    def shutdown(self):
        """关闭线程池"""
        self._executor.shutdown(wait=False)
        self.logger.info("策略引擎已关闭")
