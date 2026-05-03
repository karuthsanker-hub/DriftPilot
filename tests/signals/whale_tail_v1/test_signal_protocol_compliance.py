"""End-to-end SignalProtocol compliance and filter-chain coverage."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals import get_signal, list_signals
from driftpilot.signals.base import SignalProtocol
from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1 import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    WhaleTailV1Signal,
)
from driftpilot.states import BlockedReason


ET = ZoneInfo("America/New_York")


def _bar(
    symbol: str,
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000.0,
) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _spy_bars(latest: datetime, n: int = 60) -> list[MinuteBar]:
    out: list[MinuteBar] = []
    for i in range(n):
        ts = latest - timedelta(minutes=n - 1 - i)
        out.append(_bar("SPY", ts, 500.0, 500.0, 500.0, 500.0, 1_000_000))
    return out


def _good_setup_bars(
    symbol: str,
    end_minute: datetime,
    *,
    rvol_target: float = 5.0,
    final_close_offset: float = 0.18,
) -> list[MinuteBar]:
    """Build a synthetic series that PASSES every Whale-Tail filter:
    - 30 pre-bars with small ranges to seed ATR (~ 0.10 ATR)
    - 15 tight oscillating bars between 100.00 (low) and 100.20 (high) -> compression box
    - Final bar volume = rvol_target * 1000 (lookback-15 average is 1000)
    - Final close at 100.0 + final_close_offset (range_position = offset / 0.20)
    - No close in the last 5 bars below compression_low (100.00)
    """
    bars: list[MinuteBar] = []
    n_pre = 30
    n_window = 15
    total = n_pre + n_window
    start = end_minute - timedelta(minutes=total - 1)

    # Pre-bars: WIDE ranges so ATR seeds high. The compression window below
    # has range 0.20; we need ATR much larger (e.g. ~0.5+) so compression_score
    # (= window_range / ATR) stays well below the 0.5 threshold.
    for i in range(n_pre):
        ts = start + timedelta(minutes=i)
        bars.append(_bar(symbol, ts, 100.0, 100.5, 99.5, 100.0, 1000.0))

    # Window bars: 14 oscillators inside [100.00, 100.20], then final bar.
    for j in range(n_window - 1):
        ts = start + timedelta(minutes=n_pre + j)
        if j % 2 == 0:
            bars.append(_bar(symbol, ts, 100.10, 100.20, 100.05, 100.15, 1000.0))
        else:
            bars.append(_bar(symbol, ts, 100.15, 100.18, 100.00, 100.05, 1000.0))

    # Final bar — high RVOL, close near top of compression box.
    ts_final = start + timedelta(minutes=n_pre + n_window - 1)
    final_close = 100.00 + final_close_offset
    bars.append(
        _bar(
            symbol,
            ts_final,
            100.10,
            100.20,
            100.10,
            final_close,
            rvol_target * 1000.0,
        )
    )
    return bars


def test_signal_implements_protocol_and_registry():
    sig = WhaleTailV1Signal()
    assert isinstance(sig, SignalProtocol)
    assert sig.name == SIGNAL_NAME == "whale_tail_v1"
    assert sig.version == SIGNAL_VERSION == "1.1.0"

    fetched = get_signal("whale_tail_v1")
    assert fetched.name == "whale_tail_v1"
    assert "whale_tail_v1" in list_signals()


def test_scan_emits_allowed_candidate_when_all_filters_pass():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars = _good_setup_bars("WHALE", end, rvol_target=5.0, final_close_offset=0.18)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"WHALE": bars}, {}, _spy_bars(end))
    allowed = [c for c in cands if c.allowed]
    assert allowed, f"expected allowed candidate; got {[c.blocked_reason for c in cands]}"
    c = allowed[0]
    assert c.symbol == "WHALE"
    assert c.features["rvol"] > 3.0
    assert c.features["compression_score"] < 0.5
    assert c.features["range_position"] > 0.75
    assert c.features["atr"] > 0
    assert "compression_high" in c.features
    assert "compression_midpoint" in c.features
    assert "compression_low" in c.features
    assert "sector" in c.features


def test_scan_blocks_low_rvol():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    # rvol_target=2.5 < threshold 3.0
    bars = _good_setup_bars("LOW", end, rvol_target=2.5, final_close_offset=0.18)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"LOW": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "LOW" and not c.allowed]
    assert blocked
    assert blocked[0].blocked_reason == BlockedReason.RVOL_TOO_LOW


def test_scan_blocks_not_in_upper_range():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    # final_close_offset=0.10 -> range_position=0.5 (below 0.75 threshold)
    bars = _good_setup_bars("MID", end, rvol_target=5.0, final_close_offset=0.10)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"MID": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "MID" and not c.allowed]
    assert blocked
    assert blocked[0].blocked_reason == BlockedReason.NOT_IN_UPPER_RANGE


def test_scan_blocks_not_compressed():
    """Wide range relative to ATR -> NOT_COMPRESSED."""
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    start = end - timedelta(minutes=44)
    # Pre-bars (30) tight, then 15 bars with very wide range (5 wide each)
    for i in range(30):
        ts = start + timedelta(minutes=i)
        bars.append(_bar("WIDE", ts, 100.0, 100.02, 99.98, 100.0, 1000.0))
    # 14 wide oscillators inside [95, 105]
    for j in range(14):
        ts = start + timedelta(minutes=30 + j)
        if j % 2 == 0:
            bars.append(_bar("WIDE", ts, 100.0, 105.0, 99.0, 104.0, 1000.0))
        else:
            bars.append(_bar("WIDE", ts, 104.0, 104.5, 95.0, 96.0, 1000.0))
    # Final bar high RVOL, near top
    ts_final = start + timedelta(minutes=44)
    bars.append(_bar("WIDE", ts_final, 96.0, 105.0, 96.0, 104.5, 5000.0))
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"WIDE": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "WIDE" and not c.allowed]
    assert blocked, f"expected blocked; got {cands}"
    assert blocked[0].blocked_reason == BlockedReason.NOT_COMPRESSED


def test_scan_outside_window_returns_blocked():
    early = datetime(2025, 6, 2, 9, 45, tzinfo=ET)  # before 10:00
    bars = _good_setup_bars("EARLY", early, rvol_target=5.0, final_close_offset=0.18)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"EARLY": bars}, {}, _spy_bars(early))
    for c in cands:
        assert not c.allowed
        assert c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW


def test_scan_after_window_returns_blocked():
    late = datetime(2025, 6, 2, 15, 30, tzinfo=ET)  # after 15:00
    bars = _good_setup_bars("LATE", late, rvol_target=5.0, final_close_offset=0.18)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"LATE": bars}, {}, _spy_bars(late))
    for c in cands:
        assert not c.allowed
        assert c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW
