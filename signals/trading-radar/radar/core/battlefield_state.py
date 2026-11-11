"""
阵地状态持久化模块 (Battlefield State Persistence)

职责：
1. 持久化 MonitorEngine 运行时状态（持仓、策略内部状态、冷却记录）
2. 服务重启后自动恢复，避免"失忆"
3. 与 portfolio.json 互补：portfolio.json 是"外部真实账户"，本模块是"内部运行时状态"

存储格式：SQLite（与 cooldown 一致，原子性 + WAL 模式）
表结构：
- battlefield_state: 全局状态（启动时间、扫描计数、连续失败计数）
- position_state: 持仓状态（PortfolioTracker 的 positions + cash）
- strategy_state: 策略内部状态（RL 的 _last_action、ML 的预测器缓存键等）
- alert_history: 告警历史（用于 Dashboard 展示和重启后去重）

恢复机制：
- MonitorEngine.__init__ 时调用 restore_all()
- 主循环每次扫描后调用 save_all()
- 优雅退出时（SIGTERM/SIGINT）调用 save_all()
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from radar.alert.cooldown import AlertCooldownManager
    from radar.core.monitor_engine import MonitorEngine
    from radar.strategy.rl_strategy_v2 import PortfolioTracker


DEFAULT_DB_PATH = "battlefield_state.db"


def _resolve_db_path(explicit: Optional[str]) -> str:
    """优先使用显式路径，否则读环境变量 BATTLEFIELD_STATE_DB，最后默认文件名。"""
    if explicit is not None:
        return explicit
    return os.getenv("BATTLEFIELD_STATE_DB", DEFAULT_DB_PATH)


class BattlefieldStateManager:
    """
    阵地状态管理器

    单例模式（与 HealthMonitor 一致），确保多线程安全。
    """

    _instance: Optional["BattlefieldStateManager"] = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Optional[str] = None, logger: Optional[logging.Logger] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path: Optional[str] = None, logger: Optional[logging.Logger] = None):
        if self._initialized:
            return

        self.db_path = _resolve_db_path(db_path)
        self.logger = logger or logging.getLogger(__name__)
        self._local = threading.local()
        self._init_db()
        self._initialized = True
        atexit.register(self.close_all_connections)

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接（WAL 模式）"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def close_all_connections(self) -> None:
        """关闭当前线程持有的 SQLite 连接，用于对象销毁或系统退出。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
            self.logger.info("[Battlefield] 当前线程 SQLite 连接已关闭")

    def __del__(self):
        try:
            self.close_all_connections()
        except Exception:
            pass

    def _init_db(self) -> None:
        """初始化所有表"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        # 全局运行时状态
        conn.execute("""
            CREATE TABLE IF NOT EXISTS battlefield_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # 持仓状态（每只股票一行，JSON 存储完整 position dict）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_state (
                code TEXT PRIMARY KEY,
                shares INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0.0,
                last_price REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL
            )
        """)

        # 账户现金
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL DEFAULT 100000.0,
                initial_cash REAL NOT NULL DEFAULT 100000.0,
                updated_at REAL NOT NULL
            )
        """)

        # 策略内部状态（JSON blob，灵活存储各策略私有字段）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_state (
                code TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (code, strategy_type)
            )
        """)

        # 告警历史（用于 Dashboard 展示和重启后去重）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                price REAL,
                data_json TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_code_time
            ON alert_history(code, created_at)
        """)

        # 冷却状态镜像（与 AlertCooldownManager 的 cooldown 表结构一致，用于重启恢复）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldown_mirror (
                code TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                last_alert REAL NOT NULL,
                alert_count INTEGER DEFAULT 1,
                PRIMARY KEY (code, alert_type)
            )
        """)

        conn.commit()
        conn.close()
        self.logger.info(f"[Battlefield] 状态数据库初始化完成: {self.db_path}")

    # ───────────────────────────────────────────────
    # 保存接口
    # ───────────────────────────────────────────────

    def save_battlefield_state(self, state: Dict[str, Any]) -> None:
        """保存全局运行时状态"""
        conn = self._get_conn()
        now = time.time()
        for key, value in state.items():
            conn.execute(
                """
                INSERT INTO battlefield_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, default=str), now),
            )
        conn.commit()

    def save_portfolio(self, tracker: PortfolioTracker) -> None:
        """保存 PortfolioTracker 的持仓和现金状态"""
        conn = self._get_conn()
        now = time.time()

        # 保存现金
        conn.execute(
            """
            INSERT INTO account_state (id, cash, initial_cash, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cash = excluded.cash,
                initial_cash = excluded.initial_cash,
                updated_at = excluded.updated_at
            """,
            (tracker.cash, tracker.initial_cash, now),
        )

        # 保存持仓
        for code, pos in tracker.positions.items():
            conn.execute(
                """
                INSERT INTO position_state (code, shares, cost, last_price, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    shares = excluded.shares,
                    cost = excluded.cost,
                    last_price = excluded.last_price,
                    updated_at = excluded.updated_at
                """,
                (code, pos.get("shares", 0), pos.get("cost", 0.0), pos.get("last_price", 0.0), now),
            )

        # 清理已不存在的持仓
        codes = list(tracker.positions.keys())
        if codes:
            placeholders = ",".join("?" * len(codes))
            conn.execute(
                f"DELETE FROM position_state WHERE code NOT IN ({placeholders})",
                codes,
            )
        else:
            conn.execute("DELETE FROM position_state")

        conn.commit()
        self.logger.debug(f"[Battlefield] 持仓状态已保存: {len(tracker.positions)} 只股票")

    def save_strategy_state(self, code: str, strategy_type: str, state: Dict[str, Any]) -> None:
        """保存策略内部状态"""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO strategy_state (code, strategy_type, state_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code, strategy_type) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (code, strategy_type, json.dumps(state, default=str), now),
        )
        conn.commit()

    def save_alert(self, alert_type: str, code: str, name: str, price: float, data: Dict[str, Any]) -> None:
        """记录告警历史"""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO alert_history (alert_type, code, name, price, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (alert_type, code, name, price, json.dumps(data, default=str), now),
        )
        conn.commit()

    def mirror_cooldown(self, cooldown: AlertCooldownManager) -> None:
        """镜像冷却状态到 battlefield_state.db（用于重启恢复）

        优化：先对 cooldown rows 做指纹（hash），与上次镜像的指纹比较，
        无变更则直接跳过 DELETE+INSERT 全量重写。这把每次 save_all 一定
        发生的"跨 DB 全表删+插"双写降到只在状态真正变化时触发，对应
        诊断报告 6.5 的修复。"""
        conn = self._get_conn()
        try:
            # 从 cooldown 的 SQLite 读取
            cooldown_conn = sqlite3.connect(cooldown.db_file, check_same_thread=False)
            try:
                cursor = cooldown_conn.execute(
                    "SELECT code, alert_type, last_alert, alert_count FROM cooldown "
                    "ORDER BY code, alert_type"  # 排序保证 hash 稳定
                )
                rows = cursor.fetchall()
            finally:
                cooldown_conn.close()

            # 计算指纹：只对（code, alert_type, last_alert(秒级精度), alert_count）敏感
            # last_alert 是浮点 epoch，舍入到秒避免亚秒抖动触发无意义写入
            digest_input = "|".join(
                f"{c}:{t}:{int(la)}:{ac}" for c, t, la, ac in rows
            )
            digest = hashlib.md5(digest_input.encode("utf-8")).hexdigest()
            if digest == getattr(self, "_last_cooldown_digest", None):
                # 无变更，跳过双写
                return

            conn.execute("DELETE FROM cooldown_mirror")
            for code, alert_type, last_alert, alert_count in rows:
                conn.execute(
                    """
                    INSERT INTO cooldown_mirror (code, alert_type, last_alert, alert_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (code, alert_type, last_alert, alert_count),
                )
            conn.commit()
            self._last_cooldown_digest = digest
        except Exception as e:
            self.logger.warning(f"[Battlefield] 冷却镜像失败: {e}")

    def save_all(self, engine: MonitorEngine) -> None:
        """
        一键保存 MonitorEngine 的全部运行时状态。
        建议在每次扫描后或优雅退出时调用。
        """
        try:
            # 1. 全局状态
            self.save_battlefield_state({
                "scan_count": getattr(engine._health, "_scan_count", 0),
                "error_count": getattr(engine._health, "_error_count", 0),
                "consecutive_failures": getattr(engine, "_consecutive_failures", 0),
                "failure_alert_sent": getattr(engine, "_failure_alert_sent", False),
                "saved_at": time.time(),
            })

            # 2. 持仓（V3可能没有portfolio）
            if hasattr(engine, '_portfolio') and engine._portfolio is not None:
                self.save_portfolio(engine._portfolio)

            # 3. 策略状态（V3可能没有_rl_strategies）
            if hasattr(engine, '_rl_strategies'):
                for code, rl_strategy in engine._rl_strategies.items():
                    if hasattr(rl_strategy, '_last_action'):
                        self.save_strategy_state(code, "rl", {
                            "last_action": rl_strategy._last_action,
                        })

            # 4. 冷却镜像
            self.mirror_cooldown(engine.cooldown)

            self.logger.info("[Battlefield] 全部状态已持久化")
        except Exception as e:
            self.logger.error(f"[Battlefield] 状态保存失败: {e}")

    # ───────────────────────────────────────────────
    # 恢复接口
    # ───────────────────────────────────────────────

    def restore_battlefield_state(self) -> Dict[str, Any]:
        """恢复全局运行时状态"""
        conn = self._get_conn()
        cursor = conn.execute("SELECT key, value FROM battlefield_state")
        result = {}
        for key, value in cursor.fetchall():
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = value
        return result

    def restore_portfolio(self, tracker: PortfolioTracker) -> None:
        """恢复 PortfolioTracker 的持仓和现金"""
        conn = self._get_conn()

        # 恢复现金
        cursor = conn.execute("SELECT cash, initial_cash FROM account_state WHERE id = 1")
        row = cursor.fetchone()
        if row:
            tracker.cash = row[0]
            tracker.initial_cash = row[1]
            self.logger.info(f"[Battlefield] 恢复现金: {tracker.cash:.2f}")

        # 恢复持仓
        cursor = conn.execute("SELECT code, shares, cost, last_price FROM position_state")
        for code, shares, cost, last_price in cursor.fetchall():
            tracker.positions[code] = {
                "shares": shares,
                "cost": cost,
                "last_price": last_price,
            }
            self.logger.info(
                f"[Battlefield] 恢复持仓: {code} - {shares}股 @ {cost:.2f}"
            )

    def restore_strategy_state(self, engine: MonitorEngine) -> None:
        """恢复策略内部状态"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT code, strategy_type, state_json FROM strategy_state"
        )
        for code, strategy_type, state_json in cursor.fetchall():
            try:
                state = json.loads(state_json)
                if strategy_type == "rl" and code in engine._rl_strategies:
                    engine._rl_strategies[code]._last_action = state.get("last_action", 0)
                    self.logger.info(f"[Battlefield] 恢复 RL 状态: {code} last_action={state.get('last_action', 0)}")
            except Exception as e:
                self.logger.warning(f"[Battlefield] 恢复策略状态失败 {code}/{strategy_type}: {e}")

    def restore_cooldown(self, cooldown: AlertCooldownManager) -> None:
        """从镜像恢复冷却状态到 AlertCooldownManager"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT code, alert_type, last_alert, alert_count FROM cooldown_mirror")
            rows = cursor.fetchall()
            if not rows:
                return

            # 写入 cooldown 的数据库
            cooldown_conn = sqlite3.connect(cooldown.db_file, check_same_thread=False)
            for code, alert_type, last_alert, alert_count in rows:
                cooldown_conn.execute(
                    """
                    INSERT INTO cooldown (code, alert_type, last_alert, alert_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(code, alert_type) DO UPDATE SET
                        last_alert = excluded.last_alert,
                        alert_count = excluded.alert_count
                    """,
                    (code, alert_type, last_alert, alert_count),
                )
            cooldown_conn.commit()
            cooldown_conn.close()
            self.logger.info(f"[Battlefield] 恢复冷却状态: {len(rows)} 条记录")
        except Exception as e:
            self.logger.warning(f"[Battlefield] 恢复冷却状态失败: {e}")

    def restore_all(self, engine: MonitorEngine) -> None:
        """
        一键恢复 MonitorEngine 的全部运行时状态。
        建议在 MonitorEngine.__init__ 最后调用。
        """
        try:
            # 1. 全局状态
            state = self.restore_battlefield_state()
            if state:
                engine._consecutive_failures = state.get("consecutive_failures", 0)
                engine._failure_alert_sent = state.get("failure_alert_sent", False)
                self.logger.info(f"[Battlefield] 恢复全局状态: scan_count={state.get('scan_count', 0)}")

            # 2. 持仓（V3可能没有portfolio）
            if hasattr(engine, '_portfolio') and engine._portfolio is not None:
                self.restore_portfolio(engine._portfolio)

            # 3. 策略状态（V3可能没有）
            if hasattr(engine, '_rl_strategies'):
                self.restore_strategy_state(engine)

            # 4. 冷却状态
            self.restore_cooldown(engine.cooldown)

            self.logger.info("[Battlefield] 全部状态已恢复")
        except Exception as e:
            self.logger.error(f"[Battlefield] 状态恢复失败: {e}")

    # ───────────────────────────────────────────────
    # 查询接口（供 Dashboard 使用）；战局统筹类 Prometheus 指标见 `trading_observability.refresh_trading_observability`。
    # ───────────────────────────────────────────────

    def get_alert_history(self, code: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """获取告警历史"""
        conn = self._get_conn()
        if code:
            cursor = conn.execute(
                """
                SELECT alert_type, code, name, price, data_json, created_at
                FROM alert_history WHERE code = ? ORDER BY created_at DESC LIMIT ?
                """,
                (code, limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT alert_type, code, name, price, data_json, created_at
                FROM alert_history ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            )

        results = []
        for alert_type, code, name, price, data_json, created_at in cursor.fetchall():
            try:
                data = json.loads(data_json) if data_json else {}
            except json.JSONDecodeError:
                data = {}
            results.append({
                "alert_type": alert_type,
                "code": code,
                "name": name,
                "price": price,
                "data": data,
                "created_at": created_at,
            })
        return results

    def get_position_snapshot(self) -> Dict[str, Any]:
        """获取持仓快照（供 Dashboard /health 使用）"""
        conn = self._get_conn()
        cursor = conn.execute("SELECT cash, initial_cash FROM account_state WHERE id = 1")
        account = cursor.fetchone()

        cursor = conn.execute("SELECT code, shares, cost, last_price FROM position_state")
        positions = []
        for code, shares, cost, last_price in cursor.fetchall():
            pnl = 0.0
            if cost > 0:
                pnl = (last_price - cost) / cost * 100
            positions.append({
                "code": code,
                "shares": shares,
                "cost": cost,
                "last_price": last_price,
                "pnl_pct": pnl,
            })

        return {
            "cash": account[0] if account else 100000.0,
            "initial_cash": account[1] if account else 100000.0,
            "positions": positions,
            "position_count": len(positions),
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取持久化统计"""
        conn = self._get_conn()
        cursor = conn.execute("SELECT COUNT(*) FROM alert_history")
        alert_count = cursor.fetchone()[0]

        cursor = conn.execute("SELECT COUNT(*) FROM position_state")
        position_count = cursor.fetchone()[0]

        cursor = conn.execute("SELECT value FROM battlefield_state WHERE key = 'saved_at'")
        row = cursor.fetchone()
        last_saved = float(row[0]) if row else None

        return {
            "alert_count": alert_count,
            "position_count": position_count,
            "last_saved": last_saved,
            "db_path": self.db_path,
        }
