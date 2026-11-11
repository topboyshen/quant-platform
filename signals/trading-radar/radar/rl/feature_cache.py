#!/usr/bin/env python3
"""
RL 特征缓存模块
基于 parquet 的特征缓存，以 (df_hash, trigger_price) 为键
"""

import fcntl
import hashlib
import json
import os
import tempfile

import pandas as pd


class FeatureCache:
    """基于 parquet 的特征缓存，以 (df_hash, trigger_price) 为键"""

    def __init__(self, cache_dir: str = "rl_feature_cache"):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    @staticmethod
    def _hash(df: pd.DataFrame, trigger_price: float) -> str:
        """生成数据指纹"""
        arr = df.to_numpy(copy=False)
        h = hashlib.md5()
        h.update(arr.tobytes())
        h.update(str(df.columns.tolist()).encode())
        h.update(str(len(df)).encode())
        h.update(str(trigger_price).encode())
        return h.hexdigest()

    def load(self, df: pd.DataFrame, trigger_price: float) -> pd.DataFrame:
        """尝试从缓存加载原始特征；失败返回 None"""
        key = self._hash(df, trigger_price)
        feat_path = os.path.join(self.cache_dir, f"{key}_features.parquet")
        meta_path = os.path.join(self.cache_dir, f"{key}_meta.json")

        if not (os.path.exists(feat_path) and os.path.exists(meta_path)):
            return None

        # 使用共享读锁，避免读取正在被写入的缓存文件
        lock_path = os.path.join(self.cache_dir, f"{key}.lock")
        try:
            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    with open(meta_path, "r") as f:
                        meta = json.load(f)
                    if meta.get("columns") != df.columns.tolist() or meta.get("len") != len(df):
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                        return None
                    return pd.read_parquet(feat_path)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            return None

    def save(self, df: pd.DataFrame, trigger_price: float, features: pd.DataFrame) -> None:
        """将原始特征原子写入缓存（进程安全）"""
        key = self._hash(df, trigger_price)
        feat_path = os.path.join(self.cache_dir, f"{key}_features.parquet")
        meta_path = os.path.join(self.cache_dir, f"{key}_meta.json")
        lock_path = os.path.join(self.cache_dir, f"{key}.lock")
        try:
            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    # 若已存在则跳过写入
                    if os.path.exists(feat_path) and os.path.exists(meta_path):
                        return
                    # 原子写入 parquet
                    fd, tmp_feat = tempfile.mkstemp(
                        dir=self.cache_dir, suffix=".parquet.tmp"
                    )
                    try:
                        features.to_parquet(tmp_feat, index=False)
                        os.close(fd)
                        os.replace(tmp_feat, feat_path)
                    except Exception:
                        os.close(fd)
                        raise
                    # 原子写入 meta
                    fd2, tmp_meta = tempfile.mkstemp(
                        dir=self.cache_dir, suffix=".json.tmp"
                    )
                    try:
                        meta = {
                            "columns": df.columns.tolist(),
                            "len": len(df),
                            "trigger_price": trigger_price,
                        }
                        with os.fdopen(fd2, "w") as f:
                            json.dump(meta, f)
                        os.replace(tmp_meta, meta_path)
                    except Exception:
                        os.close(fd2)
                        raise
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            # 缓存写入失败不应中断主流程
            pass
