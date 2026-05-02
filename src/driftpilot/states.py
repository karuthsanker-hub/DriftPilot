from __future__ import annotations

from enum import StrEnum


class BlockedReason(StrEnum):
    STALE_BAR = "stale_bar"
    SPREAD_TOO_WIDE = "spread_too_wide"
    BELOW_RVOL = "below_rvol"
    BELOW_VWAP = "below_vwap"
    BELOW_15M_RETURN = "below_15m_return"
    REGIME_REJECTED = "regime_rejected"
    SECTOR_CAP_REACHED = "sector_cap_reached"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    QUOTE_UNAVAILABLE = "quote_unavailable"


class OperatorState(StrEnum):
    BOOT = "BOOT"
    MARKET_CLOSED = "MARKET_CLOSED"
    REGIME_CHECK = "REGIME_CHECK"
    SCANNING = "SCANNING"
    ALLOCATING = "ALLOCATING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    RECYCLING = "RECYCLING"
    HALTED_PDT = "HALTED_PDT"
    HALTED_RISK = "HALTED_RISK"
    ERROR = "ERROR"
