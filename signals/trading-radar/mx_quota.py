"""妙想API配额 (已迁移至 radar/data/mx_quota.py，本文件为向后兼容代理)"""
import radar.data.mx_quota as _mod
__all__ = getattr(_mod, '__all__', [])
for _k in dir(_mod):
    if not _k.startswith('_'):
        globals()[_k] = getattr(_mod, _k)
