"""Tests for the mid-price limit fill simulator.

These tests are explicitly required by Phase 3 Task 3.3 of the DriftPilot
Locked Integration Refactor Plan v1.1. The runaway-market test in particular
guards against the "tautologically true" bug where an earlier draft compared
``bar.low <= (bar.high + bar.low) / 2 <= bar.high`` and produced a 100% fill
rate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from driftpilot.backtest.limit_fill import (
    LimitOrder,
    LimitFillResult,
    attempt_limit_fill,
)
from driftpilot.signals.features import MinuteBar


SYMBOL = "TEST"
PLACED_AT = datetime(2025, 1, 6, 14, 30, tzinfo=timezone.utc)


def _bar(
    *,
    minute_offset: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000.0,
) -> MinuteBar:
    return MinuteBar(
        symbol=SYMBOL,
        timestamp=PLACED_AT + timedelta(minutes=minute_offset),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _buy_order(*, mid: float = 100.50, timeout_seconds: int = 120) -> LimitOrder:
    return LimitOrder(
        symbol=SYMBOL,
        placed_at=PLACED_AT,
        placement_mid_price=mid,
        placement_bid=mid - 0.01,
        placement_ask=mid + 0.01,
        timeout_seconds=timeout_seconds,
        side="buy",
    )


def _sell_order(*, mid: float = 100.50, timeout_seconds: int = 120) -> LimitOrder:
    return LimitOrder(
        symbol=SYMBOL,
        placed_at=PLACED_AT,
        placement_mid_price=mid,
        placement_bid=mid - 0.01,
        placement_ask=mid + 0.01,
        timeout_seconds=timeout_seconds,
        side="sell",
    )


def test_buy_limit_fills_when_low_crosses_placement_mid() -> None:
    order = _buy_order(mid=100.50)
    bars = [
        _bar(minute_offset=1, open_=100.60, high=100.70, low=100.40, close=100.55),
    ]
    result = attempt_limit_fill(order, bars)

    assert result.filled is True
    assert result.price == 100.50
    assert result.filled_at == PLACED_AT + timedelta(minutes=1)
    assert result.reason == "filled"


def test_buy_limit_does_not_fill_when_market_runs_away() -> None:
    """Regression test for the bar-derived-mid bug.

    Every bar's low is strictly ABOVE the placement mid. If the implementation
    incorrectly computes mid as ``(bar.high + bar.low) / 2`` and asks whether
    ``bar.low <= mid <= bar.high``, that comparison is tautologically true and
    this test would (wrongly) report a fill.
    """

    order = _buy_order(mid=100.50, timeout_seconds=10 * 60)
    bars = [
        _bar(minute_offset=1, open_=100.80, high=101.00, low=100.70, close=100.95),
        _bar(minute_offset=2, open_=100.95, high=101.20, low=100.90, close=101.10),
        _bar(minute_offset=3, open_=101.10, high=101.40, low=101.05, close=101.30),
    ]
    result = attempt_limit_fill(order, bars)

    assert result.filled is False
    assert result.price is None
    assert result.filled_at is None


def test_sell_limit_fills_when_high_crosses_placement_mid() -> None:
    order = _sell_order(mid=100.50)
    bars = [
        _bar(minute_offset=1, open_=100.40, high=100.60, low=100.35, close=100.45),
    ]
    result = attempt_limit_fill(order, bars)

    assert result.filled is True
    assert result.price == 100.50
    assert result.filled_at == PLACED_AT + timedelta(minutes=1)
    assert result.reason == "filled"


def test_timeout_aborts_unfilled_orders() -> None:
    order = _buy_order(mid=100.50, timeout_seconds=120)  # 2-minute window
    bars = [
        # Inside the window but never crosses mid.
        _bar(minute_offset=1, open_=100.80, high=101.00, low=100.70, close=100.95),
        _bar(minute_offset=2, open_=100.95, high=101.10, low=100.85, close=101.00),
        # Outside the window — even though this bar would have filled, it must
        # be ignored because the order has timed out.
        _bar(minute_offset=5, open_=101.00, high=101.20, low=100.20, close=100.80),
    ]
    result = attempt_limit_fill(order, bars)

    assert result.filled is False
    assert result.reason == "timeout"
    assert result.price is None


def test_no_subsequent_bars_returns_unfilled() -> None:
    order = _buy_order(mid=100.50)
    result = attempt_limit_fill(order, [])

    assert result.filled is False
    assert result.reason == "no_subsequent_bars"
    assert result.price is None
    assert result.filled_at is None


def test_monotonic_uptrend_synthetic_data_produces_zero_fill_rate() -> None:
    """60 bars in a strict monotone uptrend; one buy limit at the placement
    mid before bar 0. Correct fill rate is 0/1. The broken bar-derived-mid
    implementation gave 100%.
    """

    placement_mid = 100.00
    order = LimitOrder(
        symbol=SYMBOL,
        placed_at=PLACED_AT,
        placement_mid_price=placement_mid,
        placement_bid=placement_mid - 0.01,
        placement_ask=placement_mid + 0.01,
        timeout_seconds=60 * 60,  # 1-hour window covers all 60 bars
        side="buy",
    )

    bars: list[MinuteBar] = []
    prior_low = placement_mid + 0.05  # first low is already above the mid
    for i in range(60):
        low = prior_low + 0.01 * (i + 1)  # strictly monotone increasing
        high = low + 0.20
        open_ = low + 0.05
        close = low + 0.15
        bars.append(
            _bar(
                minute_offset=i + 1,
                open_=open_,
                high=high,
                low=low,
                close=close,
            )
        )
        prior_low = low

    # Sanity-check the synthetic data really is monotone-up and never touches
    # the placement mid from above.
    lows = [bar.low for bar in bars]
    assert all(b > a for a, b in zip(lows, lows[1:]))
    assert min(lows) > placement_mid

    attempts = [attempt_limit_fill(order, bars)]
    fills = sum(1 for r in attempts if r.filled)

    assert fills == 0, (
        "Strict monotone uptrend must produce 0 fills. A non-zero count means "
        "the simulator is using a bar-derived mid and is tautologically true."
    )


def test_realistic_pullback_produces_fill() -> None:
    """Price runs up first, then pulls back through the placement mid."""

    order = _buy_order(mid=100.50, timeout_seconds=10 * 60)
    bars = [
        # Initial run-up — does not touch 100.50 from above.
        _bar(minute_offset=1, open_=100.60, high=100.80, low=100.55, close=100.75),
        _bar(minute_offset=2, open_=100.75, high=100.95, low=100.70, close=100.90),
        _bar(minute_offset=3, open_=100.90, high=101.00, low=100.80, close=100.85),
        # Pullback — low crosses below the placement mid.
        _bar(minute_offset=4, open_=100.85, high=100.90, low=100.40, close=100.45),
    ]
    result = attempt_limit_fill(order, bars)

    assert result.filled is True
    assert result.price == 100.50
    assert result.filled_at == PLACED_AT + timedelta(minutes=4)
    assert result.reason == "filled"
