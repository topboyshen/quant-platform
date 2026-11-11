#!/usr/bin/env python3
"""专业回测引擎 Pro版 (已迁移至 radar/backtest/backtest_pro.py，本文件为向后兼容代理)"""
import radar.backtest.backtest_pro as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
