"""
预警准确率追踪系统
记录每次ML/RL预警后的实际结果，用于评估模型真实表现
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class AlertRecord:
    """单次预警记录"""
    id: int = 0
    alert_type: str = ""  # ml, rl, price
    code: str = ""
    name: str = ""
    trigger_price: float = 0.0
    direction: str = ""
    alert_price: float = 0.0  # 预警时的价格
    predicted_direction: str = ""  # 预测方向: up, down
    confidence: float = 0.0
    created_at: float = 0.0
    # 验证结果
    verified: bool = False
    actual_result: str = ""  # correct, wrong, pending
    actual_price_after_1h: float = 0.0
    actual_price_after_1d: float = 0.0
    verified_at: float = 0.0


class AccuracyTracker:
    """预警准确率追踪器"""

    def __init__(self, db_path: str = "alert_accuracy.db", logger: logging.Logger = None):
        self.db_path = db_path
        self.logger = logger or logging.getLogger(__name__)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                trigger_price REAL,
                direction TEXT,
                alert_price REAL,
                predicted_direction TEXT,
                confidence REAL,
                created_at REAL,
                verified INTEGER DEFAULT 0,
                actual_result TEXT DEFAULT '',
                actual_price_after_1h REAL,
                actual_price_after_1d REAL,
                verified_at REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_code ON alert_records(code)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_created ON alert_records(created_at)
        """)
        conn.commit()
        conn.close()

    def record_alert(self, alert_type: str, code: str, name: str,
                     trigger_price: float, direction: str,
                     alert_price: float, predicted_direction: str,
                     confidence: float = 0.0) -> int:
        """记录一次预警"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """
            INSERT INTO alert_records
            (alert_type, code, name, trigger_price, direction, alert_price,
             predicted_direction, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (alert_type, code, name, trigger_price, direction, alert_price,
             predicted_direction, confidence, time.time())
        )
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        self.logger.info(f"记录预警: {name}({code}) {alert_type} 预测={predicted_direction}")
        return record_id

    def verify_alert(self, record_id: int, actual_price: float,
                     verification_window: str = "1d") -> None:
        """
        验证预警结果
        verification_window: "1h" 或 "1d"
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT * FROM alert_records WHERE id = ?", (record_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return

        # 解析记录
        alert_price = row[6]
        predicted_direction = row[7]
        trigger_price = row[4]
        direction = row[5]

        # 判断预测是否正确
        if verification_window == "1h":
            price_change = actual_price - alert_price
            price_change_pct = price_change / alert_price if alert_price > 0 else 0

            # ML预测的是未来5个周期（约5天）内是否触发
            # 1小时验证主要用于检查短期趋势
            if predicted_direction == "down":
                # 预测下跌：价格应该下降或接近触发价
                if direction == "below":
                    # 监控跌破：价格下降为正确
                    is_correct = price_change_pct < 0.005  # 允许0.5%误差
                else:
                    is_correct = price_change_pct < -0.005
            elif predicted_direction == "up":
                is_correct = price_change_pct > -0.005
            else:
                is_correct = False

            result = "correct" if is_correct else "wrong"

            conn.execute(
                """
                UPDATE alert_records
                SET verified = 1, actual_result = ?,
                    actual_price_after_1h = ?, verified_at = ?
                WHERE id = ?
                """,
                (result, actual_price, time.time(), record_id)
            )
        else:  # 1d
            # 1天验证：检查是否接近或达到触发价
            if direction == "below":
                # 监控跌破：价格应该 <= 触发价
                is_correct = actual_price <= trigger_price * 1.02  # 允许2%误差
            else:  # above
                is_correct = actual_price >= trigger_price * 0.98

            result = "correct" if is_correct else "wrong"

            conn.execute(
                """
                UPDATE alert_records
                SET verified = 1, actual_result = ?,
                    actual_price_after_1d = ?, verified_at = ?
                WHERE id = ?
                """,
                (result, actual_price, time.time(), record_id)
            )

        conn.commit()
        conn.close()
        self.logger.info(f"验证预警 #{record_id}: {result} (实际价格={actual_price:.2f})")

    def get_accuracy_stats(self, code: str = None, alert_type: str = None,
                           days: int = 30) -> Dict:
        """获取准确率统计"""
        conn = sqlite3.connect(self.db_path)

        since = time.time() - days * 86400

        # 构建查询
        where_clause = "WHERE created_at > ?"
        params = [since]
        if code:
            where_clause += " AND code = ?"
            params.append(code)
        if alert_type:
            where_clause += " AND alert_type = ?"
            params.append(alert_type)

        # 总预警数
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM alert_records {where_clause}",
            params
        )
        total = cursor.fetchone()[0]

        # 已验证数
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM alert_records {where_clause} AND verified = 1",
            params
        )
        verified = cursor.fetchone()[0]

        # 正确数
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM alert_records {where_clause} AND verified = 1 AND actual_result = 'correct'",
            params
        )
        correct = cursor.fetchone()[0]

        # 错误数
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM alert_records {where_clause} AND verified = 1 AND actual_result = 'wrong'",
            params
        )
        wrong = cursor.fetchone()[0]

        conn.close()

        accuracy = correct / (correct + wrong) if (correct + wrong) > 0 else 0

        return {
            "total_alerts": total,
            "verified": verified,
            "correct": correct,
            "wrong": wrong,
            "accuracy": accuracy,
            "pending_verification": total - verified,
            "period_days": days,
        }

    def get_recent_alerts(self, code: str = None, limit: int = 20) -> List[Dict]:
        """获取最近预警记录"""
        conn = sqlite3.connect(self.db_path)

        where_clause = ""
        params = []
        if code:
            where_clause = "WHERE code = ?"
            params.append(code)

        cursor = conn.execute(
            f"""
            SELECT alert_type, code, name, alert_price, predicted_direction,
                   confidence, created_at, verified, actual_result
            FROM alert_records
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params + [limit]
        )

        results = []
        for row in cursor.fetchall():
            results.append({
                "alert_type": row[0],
                "code": row[1],
                "name": row[2],
                "alert_price": row[3],
                "predicted_direction": row[4],
                "confidence": row[5],
                "created_at": row[6],
                "verified": bool(row[7]),
                "actual_result": row[8],
            })

        conn.close()
        return results

    def cleanup_old_records(self, days: int = 90) -> int:
        """清理旧记录"""
        conn = sqlite3.connect(self.db_path)
        since = time.time() - days * 86400

        cursor = conn.execute(
            "DELETE FROM alert_records WHERE created_at < ?",
            (since,)
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        self.logger.info(f"清理 {deleted} 条旧预警记录")
        return deleted
