"""
妙想 API 日调用配额（本地计数，与官方「每日约 500 次」对齐，可配置）。

说明：
- 计数在每次 ``MXDataClient.query`` 发起请求前原子递增，防止并发打穿。
- 默认在「硬上限 − 预留」处停止，避免最后一两次触发服务端拒答。
- 设置 ``MX_QUOTA_DISABLED=1`` 可关闭本地限制（不推荐生产环境）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

try:
    import pytz

    _SH_TZ = pytz.timezone("Asia/Shanghai")
except Exception:  # pragma: no cover
    _SH_TZ = None

_lock = threading.RLock()
_log = logging.getLogger(__name__)


class MXQuotaExceeded(RuntimeError):
    """本日妙想调用次数已达本地配置上限。"""


def mx_quota_tracking_enabled() -> bool:
    return os.getenv("MX_QUOTA_DISABLED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _today_key() -> str:
    if _SH_TZ is not None:
        return datetime.now(_SH_TZ).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def quota_state_path() -> Path:
    custom = os.getenv("MX_QUOTA_STATE_FILE", "").strip()
    if custom:
        return Path(custom)
    return Path(".radar_mx_quota.json")


def _limits() -> tuple[int, int]:
    limit = int(os.getenv("MX_DAILY_CALL_LIMIT", "500"))
    reserve = int(os.getenv("MX_QUOTA_SOFT_RESERVE", "10"))
    return limit, max(0, reserve)


def _load(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"date": "", "count": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "count": 0}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)


def get_mx_quota_snapshot() -> Dict[str, Any]:
    """当前计数快照（不扣减），便于监控或日志。"""
    limit, reserve = _limits()
    effective = max(0, limit - reserve)
    path = quota_state_path()
    today = _today_key()
    with _lock:
        st = _load(path)
        if st.get("date") != today:
            used = 0
        else:
            used = int(st.get("count", 0))
    return {
        "date": today,
        "used": used,
        "effective_limit": effective,
        "hard_limit": limit,
        "soft_reserve": reserve,
        "remaining": max(0, effective - used),
        "tracking": mx_quota_tracking_enabled(),
        "state_file": str(path.resolve()) if path.exists() else str(path),
    }


def acquire_mx_call() -> Dict[str, Any]:
    """
    在发起一次妙想 HTTP 请求前调用：若超限则抛出 ``MXQuotaExceeded``。
    返回本次扣减后的统计 dict。
    """
    if not mx_quota_tracking_enabled():
        return {"used": -1, "effective_limit": -1, "remaining": -1, "disabled": True}

    limit, reserve = _limits()
    effective = max(0, limit - reserve)
    path = quota_state_path()
    today = _today_key()

    with _lock:
        st = _load(path)
        if st.get("date") != today:
            st = {"date": today, "count": 0}
        used = int(st.get("count", 0))
        if used >= effective:
            raise MXQuotaExceeded(
                f"妙想 API 本日调用已达本地上限 {used}/{effective} "
                f"(硬上限 {limit}, 预留 {reserve})。"
                f" 请明日再试，或调整环境变量 MX_DAILY_CALL_LIMIT / MX_QUOTA_SOFT_RESERVE，"
                f" 或临时 MX_QUOTA_DISABLED=1（不推荐）。"
            )
        used += 1
        st["count"] = used
        _save(path, st)

    if effective:
        ratio = used / effective
        if ratio >= 0.9:
            _log.warning(
                "妙想 API 今日已用 %s/%s 次（≈%.0f%%），接近配额",
                used,
                effective,
                ratio * 100,
            )
        elif ratio >= 0.8:
            _log.info(
                "妙想 API 今日已用 %s/%s 次（≈%.0f%%）",
                used,
                effective,
                ratio * 100,
            )

    return {
        "used": used,
        "effective_limit": effective,
        "remaining": max(0, effective - used),
        "hard_limit": limit,
    }
