from __future__ import annotations

from enum import StrEnum


class BlockedReason(StrEnum):
    """Reasons a symbol can be blocked from entering an allocator slot.

    Shared across every signal in the registry. Signal authors must use
    these values rather than inventing string literals. Extend this enum
    via fix-up commit if a new locked spec adds a reason.
    """

    # Cross-signal (allocator/runtime layer)
    STALE_BAR = "stale_bar"
    SPREAD_TOO_WIDE = "spread_too_wide"
    REGIME_REJECTED = "regime_rejected"
    SECTOR_CAP_REACHED = "sector_cap_reached"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    QUOTE_UNAVAILABLE = "quote_unavailable"

    # Common to all four locked v1 specs (universe + time gates)
    OUTSIDE_SCAN_WINDOW = "outside_scan_window"
    BELOW_ADV_FLOOR = "below_adv_floor"
    OUTSIDE_PRICE_CORRIDOR = "outside_price_corridor"

    # intraday_momentum_v1
    BELOW_RVOL = "below_rvol"
    BELOW_VWAP = "below_vwap"
    BELOW_15M_RETURN = "below_15m_return"

    # stationary_ghost_v1
    ADX_TOO_HIGH = "adx_too_high"
    NOT_EXTENDED_ENOUGH = "not_extended_enough"
    PULLBACK_VOLUME_TOO_HIGH = "pullback_volume_too_high"
    STOCK_RED_ON_DAY = "stock_red_on_day"

    # whale_tail_v1
    RVOL_TOO_LOW = "rvol_too_low"
    NOT_COMPRESSED = "not_compressed"
    NOT_IN_UPPER_RANGE = "not_in_upper_range"
    DISTRIBUTION_BREAK_INVALIDATED = "distribution_break_invalidated"

    # rs_drift_v1
    RS_BELOW_THRESHOLD = "rs_below_threshold"
    BELOW_POST_OPEN_VWAP = "below_post_open_vwap"
    BELOW_OPENING_RANGE_HIGH = "below_opening_range_high"
    DAILY_PROFIT_TARGET_HIT = "daily_profit_target_hit"

    # apex_hunter_v2_2
    R2_TOO_LOW = "r2_too_low"
    SLOPE_NEGATIVE_OR_DECELERATING = "slope_negative_or_decelerating"
    ALPHA_TOO_LOW = "alpha_too_low"
    CORRELATION_TOO_LOW = "correlation_too_low"
    NOT_TOP_1PCT = "not_top_1pct"


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
