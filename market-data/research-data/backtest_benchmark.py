#!/usr/bin/env python3
"""Retired benchmark diagnostic; fails before data/network access.

The former implementation compared stock open-to-close with index
close-to-close, read future buy-day OHLC to decide an opening entry, and
treated correlated trade observations as independent. Its result did not
establish excess portfolio return or a strategy edge.

Reactivation requires same-time, versioned benchmark data and an approved
execution/portfolio/statistical protocol. No benchmark is downloaded here.
The old signal names remain aliases to the canonical signal definitions.
"""
from backtest import sig_ma_volume, sig_high_tight_flag

BLOCK_REASON = (
    "Benchmark diagnostic blocked: matched opening/closing benchmark inputs "
    "and an approved research protocol are required; portfolio_return=not_modeled"
)


def get_index():
    raise RuntimeError(BLOCK_REASON)


def backtest_benchmark(sym, sig_fn, hold_days, index, cal,
                       commission=0.00025, stamp_tax=0.0005):
    raise RuntimeError(BLOCK_REASON)


def main():
    raise SystemExit(BLOCK_REASON)


if __name__ == "__main__":
    main()
