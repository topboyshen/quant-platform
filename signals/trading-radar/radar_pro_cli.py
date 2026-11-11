#!/usr/bin/env python3
"""Radar Pro realtime monitor entry point."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from radar_pro import entry_monitor

    return entry_monitor(argv)


if __name__ == "__main__":
    sys.exit(main())
