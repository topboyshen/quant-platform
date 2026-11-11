#!/usr/bin/env python3
"""Legacy entry point for the lightweight monitor.

The old classic RadarEngine implementation has been removed. Keep this file as
a thin compatibility wrapper so existing scripts that call ``python radar.py``
continue to start the supported monitor path.
"""

from __future__ import annotations

import sys
import warnings

from radar_monitor_v3 import main as monitor_main


def main() -> None:
    warnings.warn(
        "python radar.py is deprecated; use python radar_monitor_v3.py instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    monitor_main()


if __name__ == "__main__":
    sys.exit(main())
