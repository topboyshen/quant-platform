"""
扫描调度器 V3 - 自适应扫描调度
职责：
1. 自适应扫描间隔（根据市场活跃度）
2. 开盘前预热
3. 节假日检测
4. 非交易时段智能休眠
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pytz


class HolidayCalendar:
    """A股交易日历：优先读取自动更新缓存，内置列表仅作兜底。"""

    DEFAULT_CACHE_FILE = (
        Path(__file__).resolve().parents[2] / "data" / "trading_calendar.json"
    )
    _cache_lock = threading.Lock()
    # (fingerprint, trading_dates, first_date, last_date) 作为单个不可变快照发布，
    # 避免多线程读到新旧字段混合的中间态。
    _cache_state = (None, frozenset(), None, None)

    # 当缓存不存在或不覆盖目标日期时，保留 2026 年静态兜底。
    HOLIDAYS_2026 = {
        "01-01",  # 元旦
        "01-02",
        "01-03",  # 元旦
        "02-15",
        "02-16",
        "02-17",
        "02-18",
        "02-19",
        "02-20",
        "02-21",
        "02-22",
        "02-23",  # 春节
        "04-04",
        "04-05",
        "04-06",  # 清明
        "05-01",
        "05-02",
        "05-03",
        "05-04",
        "05-05",  # 劳动节
        "06-19",
        "06-20",
        "06-21",  # 端午
        "09-25",
        "09-26",
        "09-27",  # 中秋
        "10-01",
        "10-02",
        "10-03",
        "10-04",
        "10-05",
        "10-06",
        "10-07",  # 国庆
    }

    @classmethod
    def _cache_file(cls) -> Path:
        configured = os.getenv("RADAR_TRADING_CALENDAR_FILE", "").strip()
        return Path(configured).expanduser() if configured else cls.DEFAULT_CACHE_FILE

    @classmethod
    def _load_cached_calendar(cls) -> tuple[frozenset[str], Optional[str], Optional[str]]:
        path = cls._cache_file()
        try:
            stat = path.stat()
            cache_key = (
                str(path),
                stat.st_ino,
                stat.st_ctime_ns,
                stat.st_mtime_ns,
                stat.st_size,
            )
            stat_error = None
        except OSError as exc:
            # 缺失也要有指纹，否则每次调用都会重复记录同一告警。
            cache_key = (str(path), None, None, None, None)
            stat_error = exc

        state = cls._cache_state
        if cache_key == state[0]:
            return state[1], state[2], state[3]

        with cls._cache_lock:
            state = cls._cache_state
            if cache_key == state[0]:
                return state[1], state[2], state[3]
            try:
                if stat_error is not None:
                    raise stat_error
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("交易日历根节点必须是对象")
                raw_dates = payload.get("trading_dates")
                if not isinstance(raw_dates, list) or len(raw_dates) < 100:
                    raise ValueError("trading_dates 缺失或样本过少")
                normalised_dates = {
                    datetime.strptime(str(item), "%Y-%m-%d").strftime("%Y-%m-%d")
                    for item in raw_dates
                }
                if len(normalised_dates) < 100:
                    raise ValueError("有效交易日样本过少")
                trading_dates = frozenset(normalised_dates)
                first_date = str(payload.get("min_date") or min(trading_dates))
                last_date = str(payload.get("max_date") or max(trading_dates))
                datetime.strptime(first_date, "%Y-%m-%d")
                datetime.strptime(last_date, "%Y-%m-%d")
                if first_date != min(trading_dates) or last_date != max(trading_dates):
                    raise ValueError("日期范围与交易日集合不一致")
                first_day = datetime.strptime(first_date, "%Y-%m-%d").date()
                last_day = datetime.strptime(last_date, "%Y-%m-%d").date()
                if first_day >= last_day:
                    raise ValueError("交易日历日期范围异常")
                if last_day > datetime.now().date() + timedelta(days=550):
                    raise ValueError("交易日历远期边界异常")
                recent_floor = (last_day - timedelta(days=60)).isoformat()
                recent_dates = sorted(
                    datetime.strptime(value, "%Y-%m-%d").date()
                    for value in trading_dates
                    if value >= recent_floor
                )
                if len(recent_dates) < 20:
                    raise ValueError("交易日历近期样本过于稀疏")
                if any(day.weekday() >= 5 for day in recent_dates):
                    raise ValueError("交易日历近期包含周末日期")
                if max(
                    (right - left).days
                    for left, right in zip(recent_dates, recent_dates[1:])
                ) > 14:
                    raise ValueError("交易日历近期断层异常")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logging.getLogger(__name__).warning(
                    "交易日历缓存不可用，保留最后有效快照或使用静态兜底: %s (%s)",
                    path,
                    exc,
                )
                previous_key = state[0]
                if (
                    isinstance(previous_key, tuple)
                    and previous_key
                    and previous_key[0] == str(path)
                ):
                    trading_dates, first_date, last_date = state[1], state[2], state[3]
                else:
                    trading_dates = frozenset()
                    first_date = None
                    last_date = None

            cls._cache_state = (cache_key, trading_dates, first_date, last_date)
            return trading_dates, first_date, last_date

    @classmethod
    def is_holiday(cls, date: datetime) -> bool:
        """检查是否为休市日；缓存覆盖范围内以实际交易日集合为准。"""
        day = date.strftime("%Y-%m-%d")
        trading_dates, first_date, last_date = cls._load_cached_calendar()
        if first_date and last_date and first_date <= day <= last_date:
            return day not in trading_dates

        if date.year == 2026:
            return date.strftime("%m-%d") in cls.HOLIDAYS_2026
        # 未覆盖年份不冒充休市，避免旧缓存让正常交易日长期降频。
        return False


class ScanScheduler:
    """
    扫描调度器 V3

    调度策略：
    - 交易时段（活跃）: 30秒间隔
    - 交易时段（平淡）: 60秒间隔（默认）
    - 接近开盘: 10秒间隔（预热）
    - 非交易时段: 300秒间隔
    - 周末/节假日: 600秒间隔
    """

    # 间隔配置（秒）
    INTERVAL_ACTIVE = 30      # 活跃期
    INTERVAL_NORMAL = 60      # 正常期
    INTERVAL_PRE_OPEN = 10    # 开盘前预热
    INTERVAL_IDLE = 300       # 非交易时段
    INTERVAL_HOLIDAY = 600    # 节假日

    # 活跃度阈值
    ACTIVITY_VOLUME_THRESHOLD = 1_000_000  # 成交量阈值（判断活跃）

    def __init__(self, market_hours: Dict, logger: logging.Logger):
        self.market_hours = market_hours
        self.logger = logger
        self.beijing_tz = pytz.timezone('Asia/Shanghai')

        # 解析交易时间
        self.morning_start = self._parse_time(market_hours['morning_start'])
        self.morning_end = self._parse_time(market_hours['morning_end'])
        self.afternoon_start = self._parse_time(market_hours['afternoon_start'])
        self.afternoon_end = self._parse_time(market_hours['afternoon_end'])

        # 状态
        self._last_scan_time = 0.0
        self._consecutive_scans = 0
        self._market_activity_score = 0.0  # 市场活跃度评分

    @property
    def last_scan_time(self) -> float:
        """最近一次 ``record_scan()`` 的 ``time.time()`` 时间戳；启动前为 0.0。

        提供给外部主循环计算"距上次扫描还差多久应该再调度"使用。
        替代直接访问私有属性 ``_last_scan_time``。
        """
        return self._last_scan_time

    def _parse_time(self, time_str: str) -> datetime.time:
        """解析时间字符串"""
        import datetime as dt
        hour, minute = map(int, time_str.split(':'))
        return dt.time(hour, minute)

    def get_beijing_now(self) -> datetime:
        """获取当前北京时间"""
        return datetime.now(pytz.UTC).astimezone(self.beijing_tz)

    def is_trading_day(self) -> bool:
        """检查是否为交易日"""
        now = self.get_beijing_now()

        # 周末
        if now.weekday() >= 5:
            return False

        # 节假日
        if HolidayCalendar.is_holiday(now):
            return False

        return True

    def is_trading_time(self) -> bool:
        """检查是否在交易时段"""
        now = self.get_beijing_now()
        curr_time = now.time()

        morning = self.morning_start <= curr_time <= self.morning_end
        afternoon = self.afternoon_start <= curr_time <= self.afternoon_end

        return morning or afternoon

    def is_pre_open(self, window_minutes: int = 5) -> bool:
        """检查是否在开盘前预热窗口"""
        import datetime as dt
        now = self.get_beijing_now()

        # 计算距离开盘的时间
        morning_open = dt.datetime.combine(now.date(), self.morning_start)
        morning_open = self.beijing_tz.localize(morning_open)

        afternoon_open = dt.datetime.combine(now.date(), self.afternoon_start)
        afternoon_open = self.beijing_tz.localize(afternoon_open)

        now_naive = now.replace(tzinfo=None)
        morning_open_naive = morning_open.replace(tzinfo=None)
        afternoon_open_naive = afternoon_open.replace(tzinfo=None)

        morning_diff = (morning_open_naive - now_naive).total_seconds() / 60
        afternoon_diff = (afternoon_open_naive - now_naive).total_seconds() / 60

        return 0 < morning_diff <= window_minutes or 0 < afternoon_diff <= window_minutes

    def get_next_open_time(self) -> Optional[datetime]:
        """获取下次开盘时间（跳过周末和节假日）"""
        import datetime as dt
        now = self.get_beijing_now()
        curr_time = now.time()

        # 今天是否还有交易时段
        if curr_time < self.morning_start and self.is_trading_day():
            return dt.datetime.combine(now.date(), self.morning_start)
        elif curr_time < self.afternoon_start and self.is_trading_day():
            return dt.datetime.combine(now.date(), self.afternoon_start)

        # 找下一个交易日
        next_day = now + timedelta(days=1)
        for _ in range(30):  # 最多找30天，防止无限循环
            if next_day.weekday() < 5 and not HolidayCalendar.is_holiday(next_day):
                return dt.datetime.combine(next_day.date(), self.morning_start)
            next_day += timedelta(days=1)

        return None

    def update_activity(self, scan_results: list):
        """根据扫描结果更新市场活跃度"""
        if not scan_results:
            return

        # 计算平均成交量
        total_volume = sum(r.get('volume', 0) for r in scan_results if isinstance(r, dict))
        avg_volume = total_volume / len(scan_results)

        # 活跃度评分 (0-1)
        self._market_activity_score = min(1.0, avg_volume / self.ACTIVITY_VOLUME_THRESHOLD)

    def get_interval(self) -> float:
        """获取当前扫描间隔"""
        # 节假日/周末
        if not self.is_trading_day():
            return self.INTERVAL_HOLIDAY

        # 非交易时段
        if not self.is_trading_time():
            # 开盘前预热
            if self.is_pre_open():
                return self.INTERVAL_PRE_OPEN
            return self.INTERVAL_IDLE

        # 交易时段
        if self._market_activity_score > 0.7:
            return self.INTERVAL_ACTIVE

        return self.INTERVAL_NORMAL

    def should_scan(self) -> bool:
        """检查是否应该扫描"""
        interval = self.get_interval()
        elapsed = time.time() - self._last_scan_time
        return elapsed >= interval

    def record_scan(self):
        """记录扫描时间"""
        self._last_scan_time = time.time()
        self._consecutive_scans += 1

    def get_status(self) -> Dict[str, Any]:
        """获取调度器状态"""
        now = self.get_beijing_now()
        return {
            'is_trading_day': self.is_trading_day(),
            'is_trading_time': self.is_trading_time(),
            'is_pre_open': self.is_pre_open(),
            'current_interval': self.get_interval(),
            'market_activity_score': round(self._market_activity_score, 2),
            'beijing_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'next_open': self.get_next_open_time().strftime('%Y-%m-%d %H:%M:%S') if self.get_next_open_time() else None,
        }
