"""Distribution-trap invalidation: a recent close below compression_low blocks the candidate."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1 import WhaleTailV1Signal
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


def _build_compressed_then_break(
    symbol: str,
    end_minute: datetime,
) -> list[MinuteBar]:
    """Build:
    - 30 pre-bars (settle baseline for ATR seeding)
    - 15 tight oscillating bars between 100.0 and 100.2 (compression box)
      compression_low = 100.0
    - within the LAST 5 bars of the series, include a bar whose close BREAKS below 100.0.
    """
    bars: list[MinuteBar] = []
    n_pre = 30
    n_window = 15
    total = n_pre + n_window
    start = end_minute - timedelta(minutes=total - 1)

    # Pre-bars: WIDE ranges so ATR seeds high. The compression window range
    # is ~0.28 (with the break bar pulling comp_low to 99.92); we need
    # ATR > ~0.6 so compression_score < 0.5 and the distribution-break
    # check is reached.
    for i in range(n_pre):
        ts = start + timedelta(minutes=i)
        bars.append(_bar(symbol, ts, 100.0, 100.75, 99.25, 100.0, 1000.0))

    # Window bars: 15 oscillating between 100.05 and 100.2 with low=100.0.
    # Pattern: first 10 oscillate normally (low=100.0, high=100.2), then last 5
    # include a bar whose CLOSE drops below the compression_low (100.0).
    for j in range(n_window):
        ts = start + timedelta(minutes=n_pre + j)
        if j == n_window - 1:
            # Final bar: high RVOL bar, close above 100.15 to look like absorption,
            # but a recent close already broke below compression_low so the
            # invalidation should fire.
            bars.append(_bar(symbol, ts, 100.15, 100.20, 100.10, 100.18, 50_000.0))
        elif j == n_window - 3:
            # 3rd-to-last bar's CLOSE breaks below the established compression
            # floor (100.0). Low kept just below the close so it doesn't widen
            # the compression range too far (otherwise compression_score
            # exceeds the 0.5 threshold and NOT_COMPRESSED would block before
            # the distribution-break check is reached).
            bars.append(_bar(symbol, ts, 100.05, 100.05, 99.92, 99.93, 1500.0))
        else:
            high = 100.20 if j % 2 == 0 else 100.15
            low = 100.00 if j % 2 == 0 else 100.05
            close = 100.18 if j % 2 == 0 else 100.10
            bars.append(_bar(symbol, ts, close, high, low, close, 1000.0))
    return bars


def test_distribution_break_blocks_candidate():
    end = datetime(2025, 6, 2, 11, 30, tzinfo=ET)
    bars = _build_compressed_then_break("WHALE", end)
    sig = WhaleTailV1Signal()
    _, cands = sig.scan({"WHALE": bars}, {}, _spy_bars(end))
    # Find the WHALE candidate.
    whale = [c for c in cands if c.symbol == "WHALE"]
    assert whale, "expected WHALE candidate emitted"
    c = whale[0]
    assert not c.allowed, f"expected blocked but got allowed candidate: {c}"
    assert c.blocked_reason == BlockedReason.DISTRIBUTION_BREAK_INVALIDATED
