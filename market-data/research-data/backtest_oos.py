#!/usr/bin/env python3
"""Retired historical split diagnostic; not untouched out-of-sample evidence.

A post-hoc split of already inspected history is exploratory, not an OOS
admission test. The former function also allowed labels to cross its split
and used a mismatched close-only benchmark. It now fails closed instead of
publishing unsupported alpha/confidence claims. No data is loaded here.
"""
from backtest import sig_high_tight_flag

BLOCK_REASON = (
    "OOS diagnostic blocked: pre-registered untouched sample, purged split "
    "and matched benchmark protocol required; profitability is not validated"
)


def get_index():
    raise RuntimeError(BLOCK_REASON)


def backtest_oos(sym, hold_days, index, cal, split_idx,
                 commission=0.00025, stamp_tax=0.0005):
    raise RuntimeError(BLOCK_REASON)


def main():
    raise SystemExit(BLOCK_REASON)


if __name__ == "__main__":
    main()
