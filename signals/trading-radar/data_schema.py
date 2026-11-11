#!/usr/bin/env python3
"""统一数据 Schema (已迁移至 radar/schemas/data_schema.py，本文件为向后兼容代理)"""
import radar.schemas.data_schema as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
