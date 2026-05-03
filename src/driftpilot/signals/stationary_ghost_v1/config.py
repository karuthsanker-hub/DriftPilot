"""Stationary Ghost v1 — locked strategy parameters.

These values come VERBATIM from the locked spec. Do not tune within a single
backtest dataset (overfitting). Sweeps are deferred to v2.
"""

from __future__ import annotations


SIGNAL_NAME = "stationary_ghost_v1"
SIGNAL_VERSION = "1.0.0"

# Universe
UNIVERSE_SOURCE = "config/universe.csv"
MIN_ADV_SHARES = 1_500_000
PRICE_MIN = 10.00
PRICE_MAX = 500.00

# Slot model — same as global default for v1, do not override
SLOT_COUNT = 10
SLOT_NOTIONAL = 1000

# Entry signal — distance from intraday mean
LOOKBACK_BARS = 15
ENTRY_Z_SCORE_THRESHOLD = -2.5

# Trend filter — refuses to enter trending stocks
ADX_PERIOD = 14
ADX_MAX_THRESHOLD = 20

# Volume confirmation — current bar EXCLUDED from average (lookahead-bias guard)
PULLBACK_VOLUME_RATIO_MAX = 0.7

# Bullish-context filter
REQUIRE_GREEN_ON_DAY = True

# Exit logic — INVERTED RATIO. See KNOWN_RISKS.md section 1.
TARGET_PCT = 0.0075
STOP_PCT = 0.015
MAX_HOLD_MINUTES = 20

# Time gate (ET)
SCAN_START_TIME_ET = "10:00"
SCAN_END_TIME_ET = "15:30"

ENTRY_MODE = "marketable_limit_at_ask"


__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "UNIVERSE_SOURCE",
    "MIN_ADV_SHARES",
    "PRICE_MIN",
    "PRICE_MAX",
    "SLOT_COUNT",
    "SLOT_NOTIONAL",
    "LOOKBACK_BARS",
    "ENTRY_Z_SCORE_THRESHOLD",
    "ADX_PERIOD",
    "ADX_MAX_THRESHOLD",
    "PULLBACK_VOLUME_RATIO_MAX",
    "REQUIRE_GREEN_ON_DAY",
    "TARGET_PCT",
    "STOP_PCT",
    "MAX_HOLD_MINUTES",
    "SCAN_START_TIME_ET",
    "SCAN_END_TIME_ET",
    "ENTRY_MODE",
]
