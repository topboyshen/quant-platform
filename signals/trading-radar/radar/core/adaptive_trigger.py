"""AdaptiveTrigger - 动态触发价调整系统

解决静态触发价一旦跌破后反复告警、无法跟随趋势下移的问题。

核心机制：
1. 阶梯式下调：首次跌破后触发价自动下调一档（默认 -5%）
2. 冷却期：每次下调后有 24h 冷却，防止连续下调
3. 下限保护：触发价最低不低于原始触发价的 -20%
4. 反弹重置：价格反弹超过原始触发价后，恢复原始触发价

持久化：动态触发价存储在 battlefield_state.db 的 strategy_state 表中
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_STEP_PCT = 0.05      # 每次下调 5%
DEFAULT_COOLDOWN_HOURS = 24  # 下调后 24h 冷却
DEFAULT_MAX_ADJUST_PCT = 0.20  # 最大下调幅度 20%（即最低为原始价的 80%）


@dataclass
class TriggerState:
    """单只股票的动态触发价状态"""
    code: str
    original_trigger: float      # 原始触发价（来自 config.json）
    current_trigger: float       # 当前有效触发价
    direction: str               # below / above
    adjustment_count: int = 0    # 已调整次数
    last_adjusted_at: float = 0.0
    created_at: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "original_trigger": self.original_trigger,
            "current_trigger": self.current_trigger,
            "direction": self.direction,
            "adjustment_count": self.adjustment_count,
            "last_adjusted_at": self.last_adjusted_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "TriggerState":
        return cls(
            code=data.get("code", ""),
            original_trigger=data.get("original_trigger", 0.0),
            current_trigger=data.get("current_trigger", 0.0),
            direction=data.get("direction", "below"),
            adjustment_count=data.get("adjustment_count", 0),
            last_adjusted_at=data.get("last_adjusted_at", 0.0),
            created_at=data.get("created_at", 0.0),
        )


class AdaptiveTriggerManager:
    """动态触发价管理器

    集成点：
    - MonitorEngineV3._scan_once_impl() 扫描前调用 sync()
    - PriceTriggerStrategy 使用 current_trigger 而非原始 trigger_price
    - 告警消息中同时显示原始价和当前有效价
    """

    _TABLE_NAME = "adaptive_trigger"
    _STRATEGY_TYPE = "adaptive_trigger"

    def __init__(
        self,
        db_path: str = "battlefield_state.db",
        step_pct: float = DEFAULT_STEP_PCT,
        cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
        max_adjust_pct: float = DEFAULT_MAX_ADJUST_PCT,
        logger_: Optional[logging.Logger] = None,
    ):
        self.db_path = db_path
        self.step_pct = step_pct
        self.cooldown_sec = cooldown_hours * 3600
        self.max_adjust_pct = max_adjust_pct
        self.logger = logger_ or logger

        # 内存缓存
        self._states: Dict[str, TriggerState] = {}
        self._init_db()

    def _init_db(self) -> None:
        """初始化 adaptive_trigger 表（WAL 模式兼容）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._TABLE_NAME} (
                code TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def load_state(self, code: str) -> Optional[TriggerState]:
        """从数据库加载单只股票状态"""
        if code in self._states:
            return self._states[code]

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                f"SELECT state_json FROM {self._TABLE_NAME} WHERE code = ?",
                (code,),
            )
            row = cursor.fetchone()
            if row:
                state = TriggerState.from_dict(json.loads(row[0]))
                self._states[code] = state
                return state
        except Exception as e:
            self.logger.debug(f"[AdaptiveTrigger] 加载 {code} 状态失败: {e}")
        finally:
            conn.close()
        return None

    def save_state(self, state: TriggerState) -> None:
        """保存单只股票状态到数据库"""
        conn = self._get_conn()
        try:
            conn.execute(
                f"""
                INSERT INTO {self._TABLE_NAME} (code, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (state.code, json.dumps(state.to_dict()), time.time()),
            )
            conn.commit()
            self._states[state.code] = state
        except Exception as e:
            self.logger.warning(f"[AdaptiveTrigger] 保存 {state.code} 状态失败: {e}")
        finally:
            conn.close()

    def sync(
        self,
        code: str,
        original_trigger: float,
        direction: str,
        current_price: float,
    ) -> float:
        """同步动态触发价，返回当前有效触发价

        逻辑：
        1. 加载已有状态（或新建）
        2. 检查是否反弹超过原始触发价 → 重置
        3. 检查是否跌破当前触发价 → 阶梯下调
        4. 保存并返回当前有效触发价
        """
        state = self.load_state(code)
        if state is None:
            state = TriggerState(
                code=code,
                original_trigger=original_trigger,
                current_trigger=original_trigger,
                direction=direction,
                created_at=time.time(),
            )
            self.save_state(state)

        # 如果 config 中的原始触发价变了，同步更新
        if abs(state.original_trigger - original_trigger) > 0.001:
            state.original_trigger = original_trigger
            # 如果当前触发价高于原始价，也同步下调
            if direction == "below" and state.current_trigger > original_trigger:
                state.current_trigger = original_trigger
                state.adjustment_count = 0

        # 方向为 below 时的逻辑（A股做空受限，主要是监控下跌）
        if direction == "below":
            # 反弹重置：价格反弹超过原始触发价
            if current_price >= state.original_trigger:
                if state.adjustment_count > 0:
                    self.logger.info(
                        f"[AdaptiveTrigger] {code} 价格反弹至 ¥{current_price:.2f}，"
                        f"触发价从 ¥{state.current_trigger:.2f} 重置为原始价 ¥{state.original_trigger:.2f}"
                    )
                    state.current_trigger = state.original_trigger
                    state.adjustment_count = 0
                    state.last_adjusted_at = time.time()
                return state.current_trigger

            # 跌破当前触发价 → 考虑下调
            if current_price <= state.current_trigger:
                now = time.time()
                # 检查冷却期
                if now - state.last_adjusted_at < self.cooldown_sec:
                    self.logger.debug(
                        f"[AdaptiveTrigger] {code} 跌破触发价但冷却中，"
                        f"当前触发价 ¥{state.current_trigger:.2f}"
                    )
                    return state.current_trigger

                # 检查是否已达最大下调幅度
                min_trigger = state.original_trigger * (1 - self.max_adjust_pct)
                if state.current_trigger <= min_trigger:
                    self.logger.debug(
                        f"[AdaptiveTrigger] {code} 已达最大下调幅度，"
                        f"当前触发价 ¥{state.current_trigger:.2f}"
                    )
                    return state.current_trigger

                # 执行阶梯下调
                new_trigger = state.current_trigger * (1 - self.step_pct)
                # 不低于下限
                new_trigger = max(new_trigger, min_trigger)

                state.adjustment_count += 1
                state.last_adjusted_at = now
                old_trigger = state.current_trigger
                state.current_trigger = new_trigger

                self.logger.warning(
                    f"[AdaptiveTrigger] {code} 触发价下调 "
                    f"¥{old_trigger:.2f} → ¥{new_trigger:.2f} "
                    f"(第 {state.adjustment_count} 次调整，现价 ¥{current_price:.2f})"
                )
                self.save_state(state)
                return new_trigger

        # 方向为 above 时的逻辑（买入监控）
        elif direction == "above":
            # 如果价格回落到原始触发价以下，重置
            if current_price <= state.original_trigger:
                if state.adjustment_count > 0:
                    self.logger.info(
                        f"[AdaptiveTrigger] {code} 价格回落至 ¥{current_price:.2f}，"
                        f"触发价重置为原始价 ¥{state.original_trigger:.2f}"
                    )
                    state.current_trigger = state.original_trigger
                    state.adjustment_count = 0
                    state.last_adjusted_at = time.time()
                return state.current_trigger

            # 突破当前触发价 → 考虑上调（追涨逻辑）
            if current_price >= state.current_trigger:
                now = time.time()
                if now - state.last_adjusted_at < self.cooldown_sec:
                    return state.current_trigger

                max_trigger = state.original_trigger * (1 + self.max_adjust_pct)
                if state.current_trigger >= max_trigger:
                    return state.current_trigger

                new_trigger = state.current_trigger * (1 + self.step_pct)
                new_trigger = min(new_trigger, max_trigger)

                state.adjustment_count += 1
                state.last_adjusted_at = now
                old_trigger = state.current_trigger
                state.current_trigger = new_trigger

                self.logger.warning(
                    f"[AdaptiveTrigger] {code} 触发价上调 "
                    f"¥{old_trigger:.2f} → ¥{new_trigger:.2f} "
                    f"(第 {state.adjustment_count} 次调整，现价 ¥{current_price:.2f})"
                )
                self.save_state(state)
                return new_trigger

        return state.current_trigger

    def get_current_trigger(self, code: str, original_trigger: float) -> float:
        """获取当前有效触发价（不触发调整逻辑）"""
        state = self.load_state(code)
        if state is None:
            return original_trigger
        return state.current_trigger

    def reset(self, code: str) -> None:
        """手动重置某只股票的触发价"""
        state = self.load_state(code)
        if state:
            state.current_trigger = state.original_trigger
            state.adjustment_count = 0
            state.last_adjusted_at = time.time()
            self.save_state(state)
            self.logger.info(f"[AdaptiveTrigger] {code} 已手动重置为原始触发价")

    def get_all_states(self) -> Dict[str, TriggerState]:
        """获取所有动态触发价状态"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(f"SELECT code, state_json FROM {self._TABLE_NAME}")
            for code, state_json in cursor.fetchall():
                if code not in self._states:
                    try:
                        self._states[code] = TriggerState.from_dict(json.loads(state_json))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            conn.close()
        return dict(self._states)
