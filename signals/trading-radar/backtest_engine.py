#!/usr/bin/env python3
"""回测引擎 (已迁移至 radar/backtest/backtest_engine.py，本文件为向后兼容代理)"""
import sys
from pathlib import Path
__path__ = [str(Path(__file__).parent / "radar" / "backtest")]

# 透明代理：从此模块导入的任何内容都指向新位置
import radar.backtest.backtest_engine as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
