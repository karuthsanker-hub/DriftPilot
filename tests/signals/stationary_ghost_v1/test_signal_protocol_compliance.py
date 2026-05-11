"""End-to-end SignalProtocol compliance and filter-chain coverage."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


from driftpilot.signals import get_signal, list_signals
from driftpilot.signals.base import SignalProtocol
from driftpilot.signals.features import MinuteBar
from driftpilot.signals.stationary_ghost_v1 import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    StationaryGhostV1Signal,
)
from driftpilot.states import BlockedReason


ET = ZoneInfo("America/New_York")


def _bar(symbol: str, ts: datetime, o: float, h: float, low: float, c: float, v: float = 1000.0) -> MinuteBar:
    return MinuteBar(symbol=symbol, timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


def _spy_bars(latest_minute: datetime, n: int = 60) -> list[MinuteBar]:
    """Flat SPY series ending at `latest_minute` (so regime computation has data)."""
    out: list[MinuteBar] = []
    for i in range(n):
        ts = latest_minute - timedelta(minutes=n - 1 - i)
        out.append(_bar("SPY", ts, 500.0, 500.0, 500.0, 500.0, 1_000_000))
    return out


def _build_mean_reverting_bars(
    *,
    symbol: str,
    n_pre: int,
    final_z: float,
    rvol: float,
    day_return: float,
    end_minute: datetime,
    base_price: float = 100.0,
    noise: float = 1.0,
    final_high: float | None = None,
    final_low: float | None = None,
) -> list[MinuteBar]:
    """Build a synthetic series where:
       - The first bar's open sets the day open such that day_return is achieved
         at the final close.
       - The 14 bars BEFORE the latest oscillate close around `base_price` with
         alternating +/- noise to give a known std.
       - The latest bar's close sits at exactly `final_z` * std below the mean
         of the lookback window (period = 15, so we use the latest 15 bars).
       - Lookback rvol is 1000 average; latest bar volume = rvol * 1000.
       - 30+ pre-bars are produced before the lookback window to satisfy ADX
         (2*14+1 = 29 prior bars required).

    Final bar high/low default to the close to keep ADX low.
    """
    # We use n_pre pre-bars (flat) at base_price, then 14 oscillating bars,
    # then 1 final bar = the candidate.
    # The 15-bar window for Bollinger = the last 15 closes = 14 oscillating + final.
    # We need to satisfy: pstdev of those 15 closes must be > 0 (oscillation) and
    # final close = mean - final_z * std.
    # Simpler: produce 14 oscillating closes around base_price with alternating
    # +noise / -noise. Then choose final close to give the desired z.
    osc = [base_price + noise if i % 2 == 0 else base_price - noise for i in range(14)]
    # Solve for final close c such that, over [osc + c]:
    #   mean = (sum(osc) + c) / 15
    #   var  = sum((x - mean)^2 for x in osc + [c]) / 15
    #   z    = (c - mean) / sqrt(var) = final_z
    # We solve numerically by binary search.
    target = final_z

    def z_of(c: float) -> float:
        window = osc + [c]
        m = sum(window) / 15
        var = sum((x - m) ** 2 for x in window) / 15
        if var <= 0:
            return 0.0
        return (c - m) / math.sqrt(var)

    # Binary search c in a wide range.
    lo, hi = base_price - 50.0, base_price + 50.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if z_of(mid) < target:
            lo = mid
        else:
            hi = mid
    final_close = (lo + hi) / 2

    closes_in_window = osc + [final_close]
    # Now compose full bar list.
    bars: list[MinuteBar] = []
    total_bars = n_pre + 15
    start_minute = end_minute - timedelta(minutes=total_bars - 1)

    # Day open: derived so that day_return = (final_close - day_open) / day_open.
    # We force the FIRST pre-bar's open = day_open so the session-day-open
    # check uses the correct value; all other pre-bars stay near base_price
    # to keep ADX low (no big trend, no big jump into the oscillating window).
    if day_return == 0:
        day_open = final_close
    else:
        day_open = final_close / (1.0 + day_return)

    # First pre-bar carries day_open. Subsequent pre-bars sit at the
    # oscillating-window starting price (osc[0]) so the transition into the
    # window is smooth, keeping ADX low. ADX is computed off prior-bar
    # deltas, so a SINGLE large bar at index 0 contributes to TR/DM only at
    # bar 1 and is then attenuated through Wilder smoothing across the
    # remaining 28 pre-bar steps and the 15 window bars.
    settle_price = osc[0]
    for i in range(n_pre):
        ts = start_minute + timedelta(minutes=i)
        if i == 0:
            # First bar opens at day_open and closes at settle_price. The
            # internal bar range is large but ADX uses BAR-TO-BAR deltas
            # (high vs prev_high, etc.), so this is invisible to subsequent
            # DM/TR calculations. Only TR for THIS bar would matter, but
            # there's no prior bar to compute against.
            high = max(day_open, settle_price)
            low = min(day_open, settle_price)
            bars.append(_bar(symbol, ts, day_open, high, low, settle_price, 1000.0))
        else:
            bars.append(_bar(symbol, ts, settle_price, settle_price, settle_price, settle_price, 1000.0))

    # Window bars (14 oscillating + 1 final).
    for j, c in enumerate(closes_in_window):
        ts = start_minute + timedelta(minutes=n_pre + j)
        is_last = (j == len(closes_in_window) - 1)
        if is_last:
            high = final_high if final_high is not None else c
            low = final_low if final_low is not None else c
            volume = rvol * 1000.0
            bars.append(_bar(symbol, ts, c, high, low, c, volume))
        else:
            bars.append(_bar(symbol, ts, c, c + 0.01, c - 0.01, c, 1000.0))

    return bars


def test_signal_implements_protocol_and_registry():
    sig = StationaryGhostV1Signal()
    assert isinstance(sig, SignalProtocol)
    assert sig.name == SIGNAL_NAME == "stationary_ghost_v1"
    assert sig.version == SIGNAL_VERSION

    # Registry: name reachable via get_signal
    fetched = get_signal("stationary_ghost_v1")
    assert fetched.name == "stationary_ghost_v1"
    assert "stationary_ghost_v1" in list_signals()


def test_scan_emits_allowed_candidate_when_all_filters_pass():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars = _build_mean_reverting_bars(
        symbol="GOOD",
        n_pre=30,
        final_z=-3.0,
        rvol=0.3,
        day_return=0.01,
        end_minute=end,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"GOOD": bars}, {}, _spy_bars(end))
    assert len(cands) >= 1
    c = cands[0]
    assert c.allowed is True
    assert c.symbol == "GOOD"
    assert c.features["z_score"] < -2.5
    assert c.features["adx"] < 20
    assert c.features["relative_volume"] < 0.7
    assert c.features["day_return_pct"] > 0


def test_scan_blocks_high_adx():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    # Trending bars upward strongly to push ADX high.
    bars: list[MinuteBar] = []
    price = 100.0
    start = end - timedelta(minutes=44)
    for i in range(45):
        prev = price
        nxt = prev * 1.005
        bars.append(_bar("TREND", start + timedelta(minutes=i),
                         o=prev, h=nxt, low=prev, c=nxt, v=1000.0))
        price = nxt
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"TREND": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "TREND" and not c.allowed]
    assert blocked, "expected TREND blocked"
    assert blocked[0].blocked_reason == BlockedReason.ADX_TOO_HIGH


def test_scan_blocks_not_extended_enough():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars = _build_mean_reverting_bars(
        symbol="MILD",
        n_pre=30,
        final_z=-2.0,  # not below -2.5
        rvol=0.3,
        day_return=0.01,
        end_minute=end,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"MILD": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "MILD" and not c.allowed]
    assert blocked
    assert blocked[0].blocked_reason == BlockedReason.NOT_EXTENDED_ENOUGH


def test_scan_blocks_high_pullback_volume():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars = _build_mean_reverting_bars(
        symbol="VOL",
        n_pre=30,
        final_z=-3.0,
        rvol=1.5,  # >> 0.7
        day_return=0.01,
        end_minute=end,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"VOL": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "VOL" and not c.allowed]
    assert blocked
    assert blocked[0].blocked_reason == BlockedReason.PULLBACK_VOLUME_TOO_HIGH


def test_scan_blocks_red_on_day():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    bars = _build_mean_reverting_bars(
        symbol="RED",
        n_pre=30,
        final_z=-3.0,
        rvol=0.3,
        day_return=-0.01,
        end_minute=end,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"RED": bars}, {}, _spy_bars(end))
    blocked = [c for c in cands if c.symbol == "RED" and not c.allowed]
    assert blocked
    assert blocked[0].blocked_reason == BlockedReason.STOCK_RED_ON_DAY


def test_scan_outside_window_returns_empty_or_blocked():
    early = datetime(2025, 6, 2, 9, 45, tzinfo=ET)  # before 10:00
    bars = _build_mean_reverting_bars(
        symbol="EARLY",
        n_pre=30,
        final_z=-3.0,
        rvol=0.3,
        day_return=0.01,
        end_minute=early,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"EARLY": bars}, {}, _spy_bars(early))
    # Either empty list OR every candidate blocked OUTSIDE_SCAN_WINDOW.
    for c in cands:
        assert not c.allowed
        assert c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW


def test_scan_after_window_returns_empty_or_blocked():
    late = datetime(2025, 6, 2, 15, 35, tzinfo=ET)
    bars = _build_mean_reverting_bars(
        symbol="LATE",
        n_pre=30,
        final_z=-3.0,
        rvol=0.3,
        day_return=0.01,
        end_minute=late,
    )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan({"LATE": bars}, {}, _spy_bars(late))
    for c in cands:
        assert not c.allowed
        assert c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW


def test_scan_ranks_allowed_by_z_score_magnitude():
    end = datetime(2025, 6, 2, 11, 0, tzinfo=ET)
    universe: dict[str, list[MinuteBar]] = {}
    # Z-targets kept in a range where ADX stays below 20 with the synthetic
    # fixture. Extreme z-scores (e.g. -3.5+) push ADX above 20 because the
    # large mean-deviation widens the true range; that's correct signal
    # behavior (ADX filter rejects), but it defeats the ranking test.
    z_targets = {"A": -2.55, "B": -2.7, "C": -2.85, "D": -3.0, "E": -3.15}
    for sym, z in z_targets.items():
        universe[sym] = _build_mean_reverting_bars(
            symbol=sym,
            n_pre=30,
            final_z=z,
            rvol=0.3,
            day_return=0.01,
            end_minute=end,
        )
    sig = StationaryGhostV1Signal()
    _, cands = sig.scan(universe, {}, _spy_bars(end))
    allowed = [c for c in cands if c.allowed]
    assert len(allowed) == 5
    # Ranked by descending |z_score|
    zs = [abs(float(c.features["z_score"])) for c in allowed]
    assert zs == sorted(zs, reverse=True)
