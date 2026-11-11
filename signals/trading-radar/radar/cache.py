"""缓存模块"""

import datetime
import json
import logging
import os
import time
from typing import Dict


class CacheManager:
    """缓存管理器"""
    def __init__(self, cache_file: str, logger: logging.Logger):
        self.cache_file = cache_file
        self.logger = logger
        self._cache = None

    def load(self) -> Dict:
        """从本地文件读取缓存"""
        if self._cache is not None:
            return self._cache

        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self._cache = json.load(f)
                    return self._cache
            except Exception as e:
                self.logger.error(f"读取缓存失败: {e}")

        self._cache = {}
        return self._cache

    def save(self, cache: Dict) -> None:
        """保存缓存到本地文件"""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            self._cache = cache
        except Exception as e:
            self.logger.error(f"保存缓存失败: {e}")

    def clear_if_needed(self) -> bool:
        """每天收盘后清理一次缓存"""
        now = datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        cache = self.load()
        last_clear = cache.get('_last_clear_date', '')

        if now.hour >= 16 and last_clear != today:
            new_cache = {'_last_clear_date': today}
            self.save(new_cache)
            self.logger.info("🧹 缓存已清理（每日一次）")
            return True
        return False

    def get_alert_key(self, code: str, alert_type: str) -> str:
        """生成报警缓存键"""
        return f"{code}_{alert_type}"

    def is_in_cooldown(self, code: str, alert_type: str, cooldown_minutes: int) -> bool:
        """检查是否在冷却期内"""
        cache = self.load()
        key = self.get_alert_key(code, alert_type)
        last_alert = cache.get(key, 0)
        return (time.time() - last_alert) <= (cooldown_minutes * 60)

    def record_alert(self, code: str, alert_type: str) -> None:
        """记录报警时间"""
        cache = self.load()
        key = self.get_alert_key(code, alert_type)
        cache[key] = time.time()
        self.save(cache)
