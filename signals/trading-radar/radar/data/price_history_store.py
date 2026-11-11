"""
价格历史持久化存储模块
支持多种后端：内存、文件、Redis、InfluxDB
解决 Pod 重启后价格历史丢失问题
"""

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


class PriceHistoryStore(ABC):
    """价格历史存储抽象基类"""

    @abstractmethod
    def append(self, code: str, price: float, volume: float = 0, timestamp: float = None) -> None:
        """追加价格记录"""
        pass

    @abstractmethod
    def get_history(self, code: str, n: int = 100) -> List[Tuple[float, float, float]]:
        """
        获取最近N条历史记录
        :return: [(timestamp, price, volume), ...]
        """
        pass

    @abstractmethod
    def get_price_series(self, code: str, n: int = 100) -> List[float]:
        """获取价格序列"""
        pass

    @abstractmethod
    def clear(self, code: str = None) -> None:
        """清除历史（code=None 清除所有）"""
        pass

    @abstractmethod
    def close(self) -> None:
        """关闭连接"""
        pass


class InMemoryPriceStore(PriceHistoryStore):
    """内存存储（默认，开发测试用）"""

    def __init__(self, max_history: int = 500):
        self.max_history = max_history
        self._data: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
        self._lock = threading.RLock()

    def append(self, code: str, price: float, volume: float = 0, timestamp: float = None) -> None:
        if timestamp is None:
            timestamp = time.time()

        with self._lock:
            self._data[code].append((timestamp, price, volume))
            # 限制历史长度
            if len(self._data[code]) > self.max_history:
                self._data[code] = self._data[code][-self.max_history:]

    def get_history(self, code: str, n: int = 100) -> List[Tuple[float, float, float]]:
        with self._lock:
            return self._data.get(code, [])[-n:]

    def get_price_series(self, code: str, n: int = 100) -> List[float]:
        with self._lock:
            history = self._data.get(code, [])
            return [h[1] for h in history[-n:]]

    def clear(self, code: str = None) -> None:
        with self._lock:
            if code:
                self._data[code] = []
            else:
                self._data.clear()

    def close(self) -> None:
        self.clear()


class FilePriceStore(PriceHistoryStore):
    """
    文件存储（适合单 Pod 部署）
    使用 JSON Lines 格式，每天一个文件
    """

    def __init__(self, data_dir: str = "data/price_history", max_days: int = 7):
        self.data_dir = data_dir
        self.max_days = max_days
        self._cache: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
        self._lock = threading.RLock()
        self._logger = logging.getLogger(__name__)

        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)

        # 加载今日历史
        self._load_today()

    def _get_today_file(self) -> str:
        today = datetime.now().strftime("%Y%m%d")
        return os.path.join(self.data_dir, f"prices_{today}.jsonl")

    def _load_today(self) -> None:
        """加载今日历史数据"""
        filepath = self._get_today_file()
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        code = record['code']
                        self._cache[code].append((
                            record['timestamp'],
                            record['price'],
                            record.get('volume', 0)
                        ))
                    except (json.JSONDecodeError, KeyError) as e:
                        self._logger.warning(f"解析历史记录失败: {e}")
        except Exception as e:
            self._logger.error(f"加载历史数据失败: {e}")

    def append(self, code: str, price: float, volume: float = 0, timestamp: float = None) -> None:
        if timestamp is None:
            timestamp = time.time()

        record = {
            'timestamp': timestamp,
            'code': code,
            'price': price,
            'volume': volume
        }

        # 写入文件
        filepath = self._get_today_file()
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
        except Exception as e:
            self._logger.error(f"写入历史数据失败: {e}")

        # 更新缓存
        with self._lock:
            self._cache[code].append((timestamp, price, volume))
            # 限制内存缓存大小
            if len(self._cache[code]) > 500:
                self._cache[code] = self._cache[code][-500:]

    def get_history(self, code: str, n: int = 100) -> List[Tuple[float, float, float]]:
        with self._lock:
            return self._cache.get(code, [])[-n:]

    def get_price_series(self, code: str, n: int = 100) -> List[float]:
        with self._lock:
            history = self._cache.get(code, [])
            return [h[1] for h in history[-n:]]

    def clear(self, code: str = None) -> None:
        with self._lock:
            if code:
                self._cache[code] = []
            else:
                self._cache.clear()

    def close(self) -> None:
        self._cleanup_old_files()
        self.clear()

    def _cleanup_old_files(self) -> None:
        """清理过期文件"""
        try:
            cutoff = datetime.now() - timedelta(days=self.max_days)
            for filename in os.listdir(self.data_dir):
                if not filename.startswith('prices_') or not filename.endswith('.jsonl'):
                    continue
                try:
                    date_str = filename[7:15]  # prices_YYYYMMDD.jsonl
                    file_date = datetime.strptime(date_str, "%Y%m%d")
                    if file_date < cutoff:
                        os.remove(os.path.join(self.data_dir, filename))
                        self._logger.debug(f"删除过期历史文件: {filename}")
                except ValueError:
                    continue
        except Exception as e:
            self._logger.warning(f"清理历史文件失败: {e}")


class RedisPriceStore(PriceHistoryStore):
    """
    Redis 存储（推荐，多 Pod 共享）
    使用 Redis List 存储时序数据
    """

    def __init__(self, redis_url: str = None, max_history: int = 500,
                 key_prefix: str = "radar:price:", ttl: int = 86400):
        self.max_history = max_history
        self.key_prefix = key_prefix
        self.ttl = ttl
        self._logger = logging.getLogger(__name__)

        # 延迟导入 redis
        try:
            import redis
            self._redis = redis.from_url(redis_url or os.getenv('REDIS_URL', 'redis://localhost:6379/0'))
            self._redis.ping()
            self._logger.info("Redis 连接成功")
        except ImportError:
            self._logger.error("redis 包未安装，请运行: pip install redis")
            raise
        except Exception as e:
            self._logger.error(f"Redis 连接失败: {e}")
            raise

    def _get_key(self, code: str) -> str:
        return f"{self.key_prefix}{code}"

    def append(self, code: str, price: float, volume: float = 0, timestamp: float = None) -> None:
        if timestamp is None:
            timestamp = time.time()

        key = self._get_key(code)
        # 使用 Redis List，格式: "timestamp,price,volume"
        value = f"{timestamp},{price},{volume}"

        pipe = self._redis.pipeline()
        pipe.lpush(key, value)
        pipe.ltrim(key, 0, self.max_history - 1)  # 只保留最近 N 条
        pipe.expire(key, self.ttl)  # 设置过期时间
        pipe.execute()

    def get_history(self, code: str, n: int = 100) -> List[Tuple[float, float, float]]:
        key = self._get_key(code)
        values = self._redis.lrange(key, 0, n - 1)

        result = []
        for v in values:
            try:
                parts = v.decode('utf-8').split(',')
                result.append((float(parts[0]), float(parts[1]), float(parts[2])))
            except (ValueError, IndexError):
                continue

        # Redis 返回是倒序（最新在前），需要反转
        return list(reversed(result))

    def get_price_series(self, code: str, n: int = 100) -> List[float]:
        history = self.get_history(code, n)
        return [h[1] for h in history]

    def clear(self, code: str = None) -> None:
        if code:
            key = self._get_key(code)
            self._redis.delete(key)
        else:
            # 删除所有匹配的 key
            pattern = f"{self.key_prefix}*"
            for key in self._redis.scan_iter(match=pattern):
                self._redis.delete(key)

    def close(self) -> None:
        self._redis.close()


class InfluxDBPriceStore(PriceHistoryStore):
    """
    InfluxDB 存储（企业级时序数据库）
    适合大规模部署和数据分析
    """

    def __init__(self, url: str = None, token: str = None, org: str = None,
                 bucket: str = "stock_prices", measurement: str = "price"):
        self.bucket = bucket
        self.measurement = measurement
        self._logger = logging.getLogger(__name__)

        # 延迟导入 influxdb
        try:
            from influxdb_client import InfluxDBClient

            url = url or os.getenv('INFLUXDB_URL', 'http://localhost:8086')
            token = token or os.getenv('INFLUXDB_TOKEN', '')
            org = org or os.getenv('INFLUXDB_ORG', 'default')

            self._client = InfluxDBClient(url=url, token=token, org=org)
            self._write_api = self._client.write_api()
            self._query_api = self._client.query_api()
            self._logger.info("InfluxDB 连接成功")
        except ImportError:
            self._logger.error("influxdb-client 包未安装，请运行: pip install influxdb-client")
            raise
        except Exception as e:
            self._logger.error(f"InfluxDB 连接失败: {e}")
            raise

    def append(self, code: str, price: float, volume: float = 0, timestamp: float = None) -> None:
        from influxdb_client import Point

        if timestamp is None:
            timestamp = time.time()

        point = Point(self.measurement) \
            .tag("code", code) \
            .field("price", float(price)) \
            .field("volume", float(volume)) \
            .time(datetime.fromtimestamp(timestamp))

        self._write_api.write(bucket=self.bucket, record=point)

    def get_history(self, code: str, n: int = 100) -> List[Tuple[float, float, float]]:
        query = f'''
        from(bucket: "{self.bucket}")
            |> range(start: -7d)
            |> filter(fn: (r) => r._measurement == "{self.measurement}")
            |> filter(fn: (r) => r.code == "{code}")
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["_time"], desc: true)
            |> limit(n: {n})
        '''

        result = []
        tables = self._query_api.query(query)
        for table in tables:
            for record in table.records:
                ts = record.get_time().timestamp()
                price = record.values.get('price', 0)
                volume = record.values.get('volume', 0)
                result.append((ts, price, volume))

        # 倒序（最新在前），需要反转
        return list(reversed(result))

    def get_price_series(self, code: str, n: int = 100) -> List[float]:
        history = self.get_history(code, n)
        return [h[1] for h in history]

    def clear(self, code: str = None) -> None:

        delete_api = self._client.delete_api()
        start = "1970-01-01T00:00:00Z"
        stop = datetime.now().isoformat() + "Z"

        if code:
            delete_api.delete(start, stop, f'_measurement="{self.measurement}" AND code="{code}"', bucket=self.bucket, org=self._client.org)
        else:
            delete_api.delete(start, stop, f'_measurement="{self.measurement}"', bucket=self.bucket, org=self._client.org)

    def close(self) -> None:
        self._write_api.close()
        self._client.close()


class PriceStoreFactory:
    """价格存储工厂类"""

    _instance: Optional[PriceHistoryStore] = None
    _lock = threading.Lock()

    @classmethod
    def get_store(cls, store_type: str = None, **kwargs) -> PriceHistoryStore:
        """
        获取价格存储实例（单例）

        :param store_type: 存储类型 (memory, file, redis, influxdb)
                          默认从环境变量 PRICE_STORE_TYPE 读取
        """
        if cls._instance is not None:
            return cls._instance

        with cls._lock:
            if cls._instance is not None:
                return cls._instance

            store_type = store_type or os.getenv('PRICE_STORE_TYPE', 'memory')

            if store_type == 'redis':
                cls._instance = RedisPriceStore(**kwargs)
            elif store_type == 'file':
                cls._instance = FilePriceStore(**kwargs)
            elif store_type == 'influxdb':
                cls._instance = InfluxDBPriceStore(**kwargs)
            else:
                cls._instance = InMemoryPriceStore(**kwargs)

            logging.getLogger(__name__).info(f"价格存储初始化: {store_type}")
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置工厂（主要用于测试）"""
        with cls._lock:
            if cls._instance:
                cls._instance.close()
                cls._instance = None


def get_price_store() -> PriceHistoryStore:
    """获取全局价格存储实例"""
    return PriceStoreFactory.get_store()
