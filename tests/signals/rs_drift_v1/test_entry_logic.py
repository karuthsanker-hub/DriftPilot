"""Entry-time gate: only emits candidates between 10:00 and 10:30 ET."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1 import RsDriftV1Signal
from driftpilot.states import BlockedReason


ET = ZoneInfo("America/New_York")


def _bar(symbol: str, ts: datetime, price: float) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=ts,
        open=price,
        high=price + 0.05,
        low=price - 0.05,
        close=price,
        volume=1000.0,
    )


def _spy(ts_end: datetime, n: int = 60) -> list[MinuteBar]:
    return [_bar("SPY", ts_end - timedelta(minutes=n - 1 - i), 500.0) for i in range(n)]


def _stock_with_strong_rs(symbol: str, end_ts: datetime) -> list[MinuteBar]:
    """Build bars with strong RS so that ONLY the time-gate matters here."""
    bars: list[MinuteBar] = []
    session_date = end_ts.date()
    # 09:30-09:59: rise from 100 to 102 (+2.0% RS vs flat SPY)
    or_start = datetime(session_date.year, session_date.month, session_date.day, 9, 30, tzinfo=ET)
    for i in range(30):
        ts = or_start + timedelta(minutes=i)
        price = 100.0 + (2.0 * i / 29)
        bars.append(_bar(symbol, ts, price))
    # 10:00 to end_ts: stay above ORH and rising
    po_start = datetime(session_date.year, session_date.month, session_date.day, 10, 0, tzinfo=ET)
    minutes = max(int((end_ts - po_start).total_seconds() / 60), 1)
    for i in range(minutes + 1):
        ts = po_start + timedelta(minutes=i)
        price = 102.0 + 0.05 * i
        bars.append(_bar(symbol, ts, price))
    return [b for b in bars if b.timestamp <= end_ts]


def test_emits_allowed_at_10_15():
    end_ts = datetime(2024, 6, 5, 10, 15, tzinfo=ET)
    stock = _stock_with_strong_rs("ABC", end_ts)
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock}, {}, _spy(end_ts))
    allowed = [c for c in cands if c.allowed]
    assert allowed, f"expected allowed at 10:15 ET; got {[c.blocked_reason for c in cands]}"


def test_blocks_at_09_45():
    end_ts = datetime(2024, 6, 5, 9, 45, tzinfo=ET)
    bars: list[MinuteBar] = []
    or_start = datetime(2024, 6, 5, 9, 30, tzinfo=ET)
    for i in range(16):  # 09:30 to 09:45
        ts = or_start + timedelta(minutes=i)
        bars.append(_bar("ABC", ts, 100.0 + 0.1 * i))
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": bars}, {}, _spy(end_ts))
    abc = [c for c in cands if c.symbol == "ABC"]
    assert abc and not abc[0].allowed
    assert abc[0].blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW


def test_blocks_at_10_31():
    end_ts = datetime(2024, 6, 5, 10, 31, tzinfo=ET)
    stock = _stock_with_strong_rs("ABC", end_ts)
    sig = RsDriftV1Signal()
    _, cands = sig.scan({"ABC": stock}, {}, _spy(end_ts))
    abc = [c for c in cands if c.symbol == "ABC"]
    assert abc
    assert all(not c.allowed for c in abc)
    assert any(c.blocked_reason == BlockedReason.OUTSIDE_SCAN_WINDOW for c in abc)
