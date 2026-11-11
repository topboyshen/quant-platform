#!/usr/bin/env python3
"""RL Environment (已迁移至 radar/strategy/rl_environment.py，本文件为向后兼容代理)"""
import radar.strategy.rl_environment as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
