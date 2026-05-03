"""Breakeven win-rate math + ATR-scaled exit logic helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1.config import (
    STOP_ATR_MULT,
    TARGET_ATR_MULT,
    TIME_STOP_MINUTES,
)
from driftpilot.signals.whale_tail_v1.exits import breakeven_win_rate
from driftpilot.signals.whale_tail_v1 import WhaleTailV1Signal


ET = ZoneInfo("America/New_York")


def test_atr_scaled_rr_is_two_to_one():
    assert TARGET_ATR_MULT / STOP_ATR_MULT == pytest.approx(2.0)
    assert TIME_STOP_MINUTES == 60


def test_breakeven_win_rate_with_documented_slippage():
    """At $1000 notional and $100 price, slippage = max($0.02, 0.0005*100)=$0.05/share.
    Position size ~= 10 shares ($1000/$100). Round-trip slippage = ~$0.50/trade.

    With ATR ~= 0.30 (3%), target_pct = 1.5 * 0.003 = 0.0045 (0.45%),
    stop_pct = 0.75 * 0.003 = 0.00225 (0.225%).
    win_amount  = 0.0045 * 1000 - 0.50 = 4.0
    loss_amount = 0.00225 * 1000 + 0.50 = 2.75
    breakeven   = 2.75 / (4.0 + 2.75) = 0.4074...
    """
    rate = breakeven_win_rate(
        target_pct=0.0045,
        stop_pct=0.00225,
        notional=1000.0,
        avg_slippage_per_trade=0.50,
    )
    expected = 2.75 / (4.0 + 2.75)
    assert rate == pytest.approx(expected, rel=1e-6)
    assert 0.40 <= rate <= 0.41


def test_breakeven_zero_slippage_two_to_one_rr():
    """With 2:1 R:R and zero slippage, breakeven = 1/3."""
    rate = breakeven_win_rate(
        target_pct=0.02,
        stop_pct=0.01,
        notional=1000.0,
        avg_slippage_per_trade=0.0,
    )
    assert rate == pytest.approx(1.0 / 3.0, rel=1e-9)


def _bar(ts: datetime, close: float) -> MinuteBar:
    return MinuteBar(
        symbol="X",
        timestamp=ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


def _make_position(
    *,
    entry_at: datetime,
    entry_price: float,
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol="X",
        entry_at=entry_at,
        quantity=10,
        entry_reference_price=entry_price,
        entry_price=entry_price,
        target_price=entry_price * 1.0045,
        stop_price=entry_price * 0.99775,
        regime="GREEN",
        metadata=dict(metadata or {}),
    )


def test_evaluate_exit_target_hit_with_atr_at_entry():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(
        entry_at=entry,
        entry_price=100.0,
        metadata={"atr_at_entry": 0.30},  # target_pct = 1.5 * 0.003 = 0.45%
    )
    later = entry + timedelta(minutes=10)
    bar = _bar(later, 100.50)  # +0.5% > 0.45% target
    decision = sig.evaluate_exit(pos, bar, settings=SimpleNamespace())
    assert decision.should_exit is True
    assert decision.exit_reason == "TARGET"


def test_evaluate_exit_stop_hit_with_atr_at_entry():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(
        entry_at=entry,
        entry_price=100.0,
        metadata={"atr_at_entry": 0.30},  # stop_pct = 0.75 * 0.003 = 0.225%
    )
    later = entry + timedelta(minutes=5)
    bar = _bar(later, 99.70)  # -0.3% < -0.225% stop
    decision = sig.evaluate_exit(pos, bar, settings=SimpleNamespace())
    assert decision.should_exit is True
    assert decision.exit_reason == "STOP"


def test_evaluate_exit_time_stop():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(
        entry_at=entry,
        entry_price=100.0,
        metadata={"atr_at_entry": 0.30},
    )
    later = entry + timedelta(minutes=60)  # exactly TIME_STOP_MINUTES
    bar = _bar(later, 100.10)  # below target, above stop
    decision = sig.evaluate_exit(pos, bar, settings=SimpleNamespace())
    assert decision.should_exit is True
    assert decision.exit_reason == "TIME"


def test_evaluate_exit_distribution_break():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(
        entry_at=entry,
        entry_price=100.0,
        metadata={"atr_at_entry": 0.30, "compression_low_at_entry": 99.90},
    )
    later = entry + timedelta(minutes=5)
    bar = _bar(later, 99.85)  # below compression_low; above stop_pct -0.225%
    # 99.85/100 = -0.0015 = -0.15%; stop is -0.225% so STOP doesn't fire.
    decision = sig.evaluate_exit(pos, bar, settings=SimpleNamespace())
    assert decision.should_exit is True
    assert decision.exit_reason == "DISTRIBUTION_BREAK"


def test_evaluate_exit_no_exit_tracks_peak():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(
        entry_at=entry,
        entry_price=100.0,
        metadata={"atr_at_entry": 0.30},
    )
    bar1 = _bar(entry + timedelta(minutes=2), 100.10)  # +0.1%
    d1 = sig.evaluate_exit(pos, bar1, settings=SimpleNamespace())
    assert d1.should_exit is False
    assert pos.metadata["peak_unrealized_pct"] == pytest.approx(0.001, rel=1e-6)

    bar2 = _bar(entry + timedelta(minutes=4), 100.05)  # +0.05% (peak should hold)
    d2 = sig.evaluate_exit(pos, bar2, settings=SimpleNamespace())
    assert d2.should_exit is False
    assert pos.metadata["peak_unrealized_pct"] == pytest.approx(0.001, rel=1e-6)


def test_evaluate_exit_falls_back_to_settings_when_atr_missing():
    sig = WhaleTailV1Signal()
    entry = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    pos = _make_position(entry_at=entry, entry_price=100.0, metadata={})
    later = entry + timedelta(minutes=5)
    bar = _bar(later, 101.0)  # +1.0%
    settings = SimpleNamespace(target_pct=0.005, stop_pct=0.005)
    decision = sig.evaluate_exit(pos, bar, settings=settings)
    assert decision.should_exit is True
    assert decision.exit_reason == "TARGET"
