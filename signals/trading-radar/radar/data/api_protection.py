"""
API 保护模块 - 重试、限流、断路器
保护东方财富等非官方接口的稳定性
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class CircuitState(Enum):
    CLOSED = "closed"      # 正常状态，允许请求
    OPEN = "open"          # 熔断状态，拒绝请求
    HALF_OPEN = "half_open"  # 半开状态，尝试恢复


@dataclass
class CircuitBreakerConfig:
    """断路器配置"""
    failure_threshold: int = 5        # 连续失败阈值
    recovery_timeout: float = 30.0    # 熔断后恢复等待时间（秒）
    half_open_max_calls: int = 3      # 半开状态最大尝试次数


class APICircuitBreaker:
    """
    简单断路器实现
    - 连续失败超过阈值 → OPEN（熔断）
    - OPEN 持续 recovery_timeout → HALF_OPEN（试探）
    - HALF_OPEN 成功 → CLOSED（恢复）
    - HALF_OPEN 失败 → OPEN（继续熔断）
    """

    def __init__(self, name: str, config: CircuitBreakerConfig = None,
                 logger: logging.Logger = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.logger = logger or logging.getLogger(__name__)

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
        self.half_open_calls = 0

        # 告警相关
        self._alert_sent = False  # 是否已发送熔断告警
        self._recovery_alert_sent = False  # 是否已发送恢复告警
        self._on_open_callback: Optional[Callable] = None
        self._on_close_callback: Optional[Callable] = None

    def set_callbacks(self, on_open: Callable = None, on_close: Callable = None) -> None:
        """设置状态变化回调"""
        self._on_open_callback = on_open
        self._on_close_callback = on_close

    def can_execute(self) -> bool:
        """判断当前是否允许执行请求"""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time >= self.config.recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls < self.config.half_open_max_calls:
                return True
            return False

        return True

    def record_success(self) -> None:
        """记录成功"""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 2:  # 半开状态连续2次成功则恢复
                self._transition_to(CircuitState.CLOSED)
        else:
            self.failure_count = 0
            self.success_count = 0

    def record_failure(self) -> None:
        """记录失败"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN)
        elif self.state == CircuitState.CLOSED:
            if self.failure_count >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """状态转换"""
        old_state = self.state
        if old_state == new_state:
            return

        self.state = new_state
        self.logger.warning(f"断路器 [{self.name}] 状态变更: {old_state.value} -> {new_state.value}")

        if new_state == CircuitState.OPEN:
            self._alert_sent = False
            self._recovery_alert_sent = False
            if self._on_open_callback and not self._alert_sent:
                try:
                    self._on_open_callback(self.name, self.failure_count)
                    self._alert_sent = True
                except Exception as e:
                    self.logger.error(f"熔断告警回调失败: {e}")
            # Prometheus 熔断计数
            try:
                from radar.core.prometheus_extra import observe_api_circuit_open
                observe_api_circuit_open()
            except (ImportError, AttributeError):
                # Prometheus 指标上报失败不应影响熔断逻辑  # 安全忽略
                # 旧版误用 requests.RequestException 但 module 未 import requests，
                # 一旦 prometheus_extra 不可用会触发 NameError 套娃。
                pass

        elif new_state == CircuitState.CLOSED:
            self.failure_count = 0
            self.success_count = 0
            self.half_open_calls = 0
            if self._on_close_callback and not self._recovery_alert_sent:
                try:
                    self._on_close_callback(self.name)
                    self._recovery_alert_sent = True
                except Exception as e:
                    self.logger.error(f"恢复告警回调失败: {e}")

        elif new_state == CircuitState.HALF_OPEN:
            self.half_open_calls = 0
            self.success_count = 0

    def execute(self, func, *args, **kwargs) -> None:
        """
        在断路器保护下执行函数
        :return: 函数返回值，或断路器打开/异常时返回 None
        """
        if not self.can_execute():
            self.logger.warning(f"断路器 [{self.name}] 已熔断，请求被拒绝")
            return None

        if self.state == CircuitState.HALF_OPEN:
            self.half_open_calls += 1

        try:
            result = func(*args, **kwargs)
            if result is not None:
                self.record_success()
            else:
                self.record_failure()
            return result
        except Exception as e:
            # 检查是否为 HTTP 429 限频
            err_str = str(e)
            if "429" in err_str or "Too Many Requests" in err_str:
                try:
                    from radar.core.prometheus_extra import observe_api_rate_limit_429
                    observe_api_rate_limit_429()
                except (ImportError, AttributeError):
                    # Prometheus 指标上报失败不应影响熔断计数  # 安全忽略
                    pass
            # 检查是否为 HTTP 5xx
            elif any(code in err_str for code in ("500", "502", "503", "504")):
                try:
                    from radar.core.prometheus_extra import observe_api_error_5xx
                    observe_api_error_5xx()
                except (ImportError, AttributeError):
                    # Prometheus 指标上报失败不应影响熔断计数  # 安全忽略
                    pass
            self.record_failure()
            self.logger.warning(f"断路器保护函数执行异常: {e}")
            return None

    def get_status(self) -> dict:
        """获取断路器状态"""
        return {
            'name': self.name,
            'state': self.state.value,
            'failure_count': self.failure_count,
            'success_count': self.success_count,
            'last_failure_time': self.last_failure_time,
            'seconds_since_last_failure': time.time() - self.last_failure_time if self.last_failure_time > 0 else None,
        }


def make_api_request_with_retry(session, url, method='GET', logger=None, **kwargs) -> None:
    """
    使用 tenacity 做指数退避重试的请求封装

    重试策略：
    - 最多 3 次重试
    - 等待时间：1s, 2s, 4s（指数退避）
    - 只对网络错误和 5xx 状态码重试
    - 4xx 错误不重试（客户端错误）
    """
    import requests
    from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

    logger = logger or logging.getLogger(__name__)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
        )),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True
    )
    def _do_request() -> None:
        if method.upper() == 'GET':
            response = session.get(url, **kwargs)
        else:
            response = session.post(url, **kwargs)

        # 5xx 错误触发重试
        if response.status_code >= 500:
            response.raise_for_status()

        return response

    return _do_request()


class ProtectedMarketDataFetcher:
    """
    带保护的行情数据获取器包装
    在现有 MarketDataFetcher 基础上增加重试和断路器
    """

    def __init__(self, fetcher, logger: logging.Logger = None,
                 failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.fetcher = fetcher
        self.logger = logger or logging.getLogger(__name__)

        # 批量接口断路器
        self.batch_breaker = APICircuitBreaker(
            name="eastmoney_batch",
            config=CircuitBreakerConfig(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout
            ),
            logger=self.logger
        )

        # 单股接口断路器
        self.single_breaker = APICircuitBreaker(
            name="eastmoney_single",
            config=CircuitBreakerConfig(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout
            ),
            logger=self.logger
        )

    def set_alert_callbacks(self, on_open: Callable, on_close: Callable) -> None:
        """设置数据源异常告警回调"""
        self.batch_breaker.set_callbacks(on_open, on_close)
        self.single_breaker.set_callbacks(on_open, on_close)

    def fetch_all(self) -> None:
        """受保护的批量接口调用"""
        import requests

        if not self.batch_breaker.can_execute():
            return None

        try:
            result = self.fetcher.fetch_all()
            if result is not None and not result.empty:
                self.batch_breaker.record_success()
            else:
                self.batch_breaker.record_failure()
            return result
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self.batch_breaker.record_failure()
            self.logger.warning(f"批量接口网络异常: {e}")
            return None
        except Exception as e:
            self.batch_breaker.record_failure()
            self.logger.error(f"批量接口异常: {e}")
            return None

    def get_stock_data(self, code: str, name: str = None) -> None:
        """受保护的单股接口调用"""
        import requests

        if not self.single_breaker.can_execute():
            return None

        try:
            result = self.fetcher.get_stock_data(code, name)
            if result is not None:
                self.single_breaker.record_success()
            else:
                self.single_breaker.record_failure()
            return result
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self.single_breaker.record_failure()
            self.logger.warning(f"单股接口网络异常 [{code}]: {e}")
            return None
        except Exception as e:
            self.single_breaker.record_failure()
            self.logger.error(f"单股接口异常 [{code}]: {e}")
            return None

    def get_breaker_status(self) -> dict:
        """获取断路器状态"""
        return {
            'batch': self.batch_breaker.get_status(),
            'single': self.single_breaker.get_status(),
        }
