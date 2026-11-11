#!/usr/bin/env python3
"""RL Strategy (已迁移至 radar/strategy/rl_strategy.py，本文件为向后兼容代理)"""
import radar.strategy.rl_strategy as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
