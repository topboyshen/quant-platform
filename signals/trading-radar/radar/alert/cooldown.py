"""
告警层 - 冷却管理模块
职责：管理告警频率，防止刷屏

优化：使用 SQLite 替代 JSON，更好的原子性和异常安全
"""

import logging
import sqlite3
import threading
import time
from typing import Dict


class AlertCooldownManager:
    """告警冷却管理器 - SQLite版

特点：
1. 原子性写入（事务）
2. 服务重启不会丢失冷却状态
3. 支持高并发读取
    """

    def __init__(self, cache_file: str, logger: logging.Logger):
        # 将 .json 转为 .db
        if cache_file.endswith('.json'):
            self.db_file = cache_file.replace('.json', '_cooldown.db')
        else:
            self.db_file = cache_file + '.db'
        self.logger = logger
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            # WAL 模式提高并发性能
            self._local.conn.execute('PRAGMA journal_mode=WAL')
            self._local.conn.execute('PRAGMA synchronous=NORMAL')
        return self._local.conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_file)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS cooldown (
                code TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                last_alert REAL NOT NULL,
                alert_count INTEGER DEFAULT 1,
                PRIMARY KEY (code, alert_type)
            )
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_last_alert ON cooldown(last_alert)
        ''')
        conn.commit()
        conn.close()

    def is_in_cooldown(self, code: str, alert_type: str, cooldown_minutes: int) -> bool:
        """检查是否处于冷却期"""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                'SELECT last_alert FROM cooldown WHERE code = ? AND alert_type = ?',
                (code, alert_type)
            )
            row = cursor.fetchone()
            if row:
                last_alert = row[0]
                return (time.time() - last_alert) <= (cooldown_minutes * 60)
            return False
        except Exception as e:
            self.logger.error(f"冷却检查失败: {e}")
            # 出错时返回 False，避免 SQLite 故障导致永久静默
            return False

    def record_alert(self, code: str, alert_type: str) -> None:
        """记录告警时间"""
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute('''
                INSERT INTO cooldown (code, alert_type, last_alert, alert_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(code, alert_type) DO UPDATE SET
                    last_alert = ?,
                    alert_count = alert_count + 1
            ''', (code, alert_type, now, now))
            conn.commit()
        except Exception as e:
            self.logger.error(f"记录告警失败: {e}")

    def clear_cache(self) -> None:
        """清空缓存"""
        try:
            conn = self._get_conn()
            conn.execute('DELETE FROM cooldown')
            conn.commit()
            self.logger.info("告警冷却缓存已清空")
        except Exception as e:
            self.logger.error(f"清空缓存失败: {e}")

    def get_stats(self) -> Dict:
        """获取统计信息"""
        try:
            conn = self._get_conn()
            cursor = conn.execute('SELECT COUNT(*), SUM(alert_count) FROM cooldown')
            row = cursor.fetchone()
            return {
                'total_stocks': row[0] or 0,
                'total_alerts': row[1] or 0
            }
        except Exception as e:
            self.logger.error(f"获取统计失败: {e}")
            return {'total_stocks': 0, 'total_alerts': 0}
