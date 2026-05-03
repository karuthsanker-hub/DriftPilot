"""Whale-Tail v1 - locked strategy parameters.

Values come VERBATIM from the locked spec. Do not tune within a single
backtest dataset (overfitting). Sweeps are deferred to v2.
"""

from __future__ import annotations


SIGNAL_NAME = "whale_tail_v1"
SIGNAL_VERSION = "1.1.0"

# Universe
UNIVERSE_SOURCE = "config/universe.csv"
MIN_ADV_SHARES = 1_500_000
PRICE_MIN = 10.00
PRICE_MAX = 500.00

# Slot model
SLOT_COUNT = 10
SLOT_NOTIONAL = 1000
SECTOR_CAP = 2

# Entry — RVOL with current bar EXCLUDED, compression, range position.
RVOL_WINDOW_MINUTES = 15
RVOL_THRESHOLD = 3.0
COMPRESSION_LOOKBACK_BARS = 15
COMPRESSION_THRESHOLD = 0.5
RANGE_POSITION_THRESHOLD = 0.75

# ATR — Wilder's smoothing
ATR_PERIOD = 20

# Time gate (ET)
SCAN_START_TIME_ET = "10:00"
SCAN_END_TIME_ET = "15:00"

# Exits — ATR-scaled
TARGET_ATR_MULT = 1.5
STOP_ATR_MULT = 0.75
TIME_STOP_MINUTES = 60

ENTRY_MODE = "variant_b"  # marketable_limit_at_range_high + slippage


__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "UNIVERSE_SOURCE",
    "MIN_ADV_SHARES",
    "PRICE_MIN",
    "PRICE_MAX",
    "SLOT_COUNT",
    "SLOT_NOTIONAL",
    "SECTOR_CAP",
    "RVOL_WINDOW_MINUTES",
    "RVOL_THRESHOLD",
    "COMPRESSION_LOOKBACK_BARS",
    "COMPRESSION_THRESHOLD",
    "RANGE_POSITION_THRESHOLD",
    "ATR_PERIOD",
    "SCAN_START_TIME_ET",
    "SCAN_END_TIME_ET",
    "TARGET_ATR_MULT",
    "STOP_ATR_MULT",
    "TIME_STOP_MINUTES",
    "ENTRY_MODE",
]
