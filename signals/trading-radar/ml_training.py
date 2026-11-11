#!/usr/bin/env python3
"""ML训练 (已迁移至 radar/ml/training_compat.py，本文件为向后兼容代理)"""
import radar.ml.training_compat as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)

if __name__ == "__main__":
    train_all_models()
