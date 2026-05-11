"""SignalProtocol compliance + filter-chain coverage for RS-Drift v1.1."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from driftpilot.signals import get_signal, list_signals
from driftpilot.signals.base import SignalProtocol
from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1 import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    RsDriftV1Signal,
)
from driftpilot.states import BlockedReason


ET = ZoneInfo("America/New_York")


def _bar(symbol: str, ts: datetime, open_: float, *, high: float | None = None,
         low: float | None = None, close: float | None = None,
         volume: float = 1000.0) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=ts,
        open=open_,
        high=high if high is not None else open_,
        low=low if low is not None else open_,
        close=close if close is not None else open_,
        volume=volume,
    )


def _build_session(symbol: str, session_date: date,
                   *, opening_window_close: float, post_open_close: float,
                   base_open: float = 100.0,
                   end_minute_et: time = time(10, 15)) -> list[MinuteBar]:
    """Build bars from `base_open` at 09:30 ET, ending at `opening_window_close`
    at 09:59, then drifting from there to `post_open_close` at `end_minute_et`.
    """
    bars: list[MinuteBar] = []

    # Opening range 09:30–09:59 with prices rising from base_open to opening_window_close
    or_start = datetime(session_date.year, session_date.month, session_date.day, 9, 30, tzinfo=ET)
    or_minutes = 30
    for i in range(or_minutes):
        ts = or_start + timedelta(minutes=i)
        price = base_open + (opening_window_close - base_open) * (i / max(or_minutes - 1, 1))
        bars.append(_bar(symbol, ts, open_=price, high=price + 0.05, low=price - 0.05, close=price, volume=1000.0))

    # Post-open 10:00–end_minute_et with prices drifting to post_open_close
    po_start = datetime(session_date.year, session_date.month, session_date.day, 10, 0, tzinfo=ET)
    po_end = datetime(session_date.year, session_date.month, session_date.day, end_minute_et.hour, end_minute_et.minute, tzinfo=ET)
    po_minutes = max(int((po_end - po_start).total_seconds() / 60), 1)
    for i in range(po_minutes + 1):
        ts = po_start + timedelta(minutes=i)
        price = opening_window_close + (post_open_close - opening_window_close) * (i / po_minutes)
        bars.append(_bar(symbol, ts, open_=price, high=price + 0.05, low=price - 0.05, close=price, volume=1000.0))

    return bars


def _spy_session_flat(session_date: date, end_minute_et: time = time(10, 15)) -> list[MinuteBar]:
    """Flat SPY at 500.0 from 09:30 through end_minute_et."""
    return _build_session(
        "SPY", session_date,
        base_open=500.0,
        opening_window_close=500.0,
        post_open_close=500.0,
        end_minute_et=end_minute_et,
    )


def test_signal_implements_protocol_and_registry():
    sig = RsDriftV1Signal()
    assert isinstance(sig, SignalProtocol)
    assert sig.name == SIGNAL_NAME == "rs_drift_v1"
    assert sig.version == SIGNAL_VERSION == "1.1.0"

    fetched = get_signal("rs_drift_v1")
    assert fetched.name == "rs_drift_v1"
    assert "rs_drift_v1" in list_signals()


def test_scan_emits_allowed_candidate_when_all_filters_pass():
    session_date = date(2024, 6, 5)
    # Stock: 09:30 100 → 09:59 102.0 (+2.0%, RS=2.0% > 1.25%); 10:00 stays above
    # the 09:30-10:00 high (102.0) and above post-10:00 VWAP.
    stock_bars = _build_session(
        "ABC", session_date,
        opening_window_close=102.0, post_open_close=103.5,
        end_minute_et=time(10, 15),
    )
    spy_bars = _spy_session_flat(session_date)
    sig = RsDriftV1Signal()

    _, cands = sig.scan({"ABC": stock_bars}, {}, spy_bars)
    allowed = [c for c in cands if c.allowed]
    assert allowed, f"expected allowed; got reasons={[c.blocked_reason for c in cands]}"
    c = allowed[0]
    assert c.symbol == "ABC"
    assert c.features["rs_score"] >= 1.25
    assert c.features["price"] > c.features["orh"]
    assert c.features["price"] > c.features["post_open_vwap"]


def test_scan_blocks_outside_window_before_10am():
    # Inline bars from 09:30 to 09:55 only — strictly before the 10:00 scan window.
    stock_bars: list[MinuteBar] = []
    or_start = datetime(2024, 6, 5, 9, 30, tzinfo=ET)
    for i in range(26):  # 09:30 .. 09:55 inclusive (26 bars)
        ts = or_start + timedelta(minutes=i)
        stock_bars.append(_bar("ABC", ts, open_=100.0 + 0.1 * i))
    spy_bars: list[MinuteBar] = []
    for i in range(26):
        ts = or_start + timedelta(minutes=i)
        spy_bars.append(_bar("SPY", ts, open_=500.0))
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock_bars}, {}, spy_bars)
    for c in cands:
        if c.symbol == "ABC":
            assert not c.allowed
            assert c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW


def test_scan_blocks_outside_window_after_10_30():
    session_date = date(2024, 6, 5)
    # End of bars at 10:35 ET — past scan close.
    stock_bars = _build_session(
        "ABC", session_date,
        opening_window_close=102.0, post_open_close=103.5,
        end_minute_et=time(10, 35),
    )
    spy_bars = _spy_session_flat(session_date, end_minute_et=time(10, 35))
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock_bars}, {}, spy_bars)
    abc = [c for c in cands if c.symbol == "ABC"]
    assert abc
    assert all(not c.allowed for c in abc)
    assert any(c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW for c in abc)


def test_scan_blocks_rs_below_threshold():
    session_date = date(2024, 6, 5)
    # Stock only +0.8% from 9:30–10:00 → below 1.25% threshold.
    stock_bars = _build_session(
        "ABC", session_date,
        opening_window_close=100.8, post_open_close=101.2,
        end_minute_et=time(10, 15),
    )
    spy_bars = _spy_session_flat(session_date)
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock_bars}, {}, spy_bars)
    abc = [c for c in cands if c.symbol == "ABC"]
    assert abc and not abc[0].allowed
    assert abc[0].blocked_reason == BlockedReason.RS_BELOW_THRESHOLD


def test_scan_blocks_below_orh():
    session_date = date(2024, 6, 5)
    # Strong RS (+2.0%) but post-open price drifts BACK below the 09:30–10:00 high.
    # opening high 102.05 (peak inside the window); post_open closes at 101.5.
    stock_bars = _build_session(
        "ABC", session_date,
        opening_window_close=102.0, post_open_close=101.5,
        end_minute_et=time(10, 15),
    )
    spy_bars = _spy_session_flat(session_date)
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock_bars}, {}, spy_bars)
    abc = [c for c in cands if c.symbol == "ABC"]
    assert abc and not abc[0].allowed
    # Should be blocked either by VWAP or ORH; both are valid pre-rank states
    # but the spec orders ORH after VWAP in the filter chain. Accept either,
    # prioritize ORH since that's the scenario we're targeting.
    assert abc[0].blocked_reason in {
        BlockedReason.BELOW_OPENING_RANGE_HIGH,
        BlockedReason.BELOW_POST_OPEN_VWAP,
    }


def test_scan_ranks_allowed_by_rs_score_descending():
    session_date = date(2024, 6, 5)
    universe: dict[str, list[MinuteBar]] = {}
    rs_targets = {
        "A": (101.4, 102.5),  # RS ~1.4%
        "B": (102.0, 103.5),  # RS ~2.0%
        "C": (103.0, 104.5),  # RS ~3.0%
    }
    for sym, (or_close, po_close) in rs_targets.items():
        universe[sym] = _build_session(
            sym, session_date,
            opening_window_close=or_close, post_open_close=po_close,
            end_minute_et=time(10, 15),
        )
    spy_bars = _spy_session_flat(session_date)
    sig = RsDriftV1Signal()
    _, cands = sig.scan(universe, {}, spy_bars)
    allowed = [c for c in cands if c.allowed]
    assert len(allowed) == 3, f"expected 3 allowed; got {[c.blocked_reason for c in cands]}"
    # Ranked by RS score descending
    rs_scores = [c.features["rs_score"] for c in allowed]
    assert rs_scores == sorted(rs_scores, reverse=True)
    assert allowed[0].symbol == "C"
    assert allowed[-1].symbol == "A"
