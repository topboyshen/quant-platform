"""Radar Pro package facade.

The historical aggregate CLI still lives at the repository root as
``radar_pro.py``. This package facade loads that file under an internal module
name so importers can use ``import radar_pro`` without colliding with the root
script.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType
from pathlib import Path

_ROOT_ENTRY_PATH = Path(__file__).resolve().parents[1] / "radar_pro.py"
_ENTRY: ModuleType | None = None


def _load_entry() -> ModuleType:
    global _ENTRY
    if _ENTRY is None:
        spec = importlib.util.spec_from_file_location("_radar_pro_legacy_entry", _ROOT_ENTRY_PATH)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive import guard
            raise ImportError(f"Cannot load Radar Pro entry module from {_ROOT_ENTRY_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ENTRY = module
    return _ENTRY


def __getattr__(name: str):
    if name in __all__:
        return getattr(_load_entry(), name)
    raise AttributeError(name)

__all__ = ["entry_monitor", "entry_train", "entry_backtest", "main"]
