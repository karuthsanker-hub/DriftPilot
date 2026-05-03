"""Locked configuration for RS-Drift v1.1.

Parameters are the spec verbatim — do not "improve" them.
"""

from __future__ import annotations


SIGNAL_NAME = "rs_drift_v1"
SIGNAL_VERSION = "1.1.0"

# Universe
UNIVERSE_SOURCE = "config/universe.csv"
MIN_ADV_SHARES = 2_000_000
PRICE_MIN = 15.00
PRICE_MAX = 400.00

# Slot model — concentrated by design
SLOT_COUNT = 5
SLOT_NOTIONAL = 2000
SECTOR_CAP = 2

# Discovery — relative strength vs SPY
SCAN_START_TIME_ET = "10:00"
SCAN_END_TIME_ET = "10:30"
RS_THRESHOLD_PCT = 1.25
ANCHOR_REQUIRES_ABOVE_VWAP = True
ENTRY_REQUIRES_ABOVE_ORH = True
OPENING_RANGE_WINDOW_ET = ("09:30", "10:00")

# Entry execution
ENTRY_ORDER_TYPE = "limit_mid"

# Exit logic
TARGET_PCT = 0.015
STOP_PCT = 0.0075
BREAK_EVEN_TRIGGER_PCT = 0.0075
TIME_STOP_TIME_ET = "15:55"

# Daily circuit breakers
DAILY_PROFIT_TARGET_USD = 125
DAILY_LOSS_LIMIT_USD = 100

# SPY heat sensor
SPY_HEAT_DROP_PCT = 0.005
SPY_HEAT_WINDOW_MINUTES = 5
SPY_HEAT_TIGHTENED_STOP_PCT = 0.0025
