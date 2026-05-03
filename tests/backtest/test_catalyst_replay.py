"""Tests for the v3 catalyst backtest harness.

Uses fabricated bars + a tiny in-memory catalyst DB so the test runs in
milliseconds without touching the 1.7GB Databento cache.
"""

from __future__ import annotations
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from driftpilot.backtest.catalyst_replay import replay_catalyst_signal
from driftpilot.catalyst.db import init_catalyst_schema, insert_event
from driftpilot.catalyst.event import CatalystEvent


def _event(symbol, category, subcategory, ts, headline="t"):
    h = hashlib.sha256(f"{symbol}|{ts.isoformat()}".encode()).hexdigest()[:16]
    return CatalystEvent(
        symbol=symbol, category=category, subcategory=subcategory, pillar="micro",
        ts=ts, headline=headline, source="test",
        horizon_minutes=60, headline_hash=h,
    )


def _write_bars(bar_root: Path, symbol: str, year: int, bars: list[dict]) -> None:
    """Write a tiny parquet file with the columns the replay needs."""
    df = pd.DataFrame(bars)
    out = bar_root / symbol / f"{year}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)


@pytest.fixture
def catalyst_db(tmp_path):
    p = str(tmp_path / "catalyst.db")
    init_catalyst_schema(p)
    return p


@pytest.fixture
def bar_root(tmp_path):
    return tmp_path / "bars"


def test_no_events_returns_empty_replay(catalyst_db, bar_root):
    """Empty DB → empty result, no crash."""
    bar_root.mkdir(parents=True, exist_ok=True)
    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert result.trades == []
    assert result.starting_capital == result.ending_capital
    assert any("No catalyst events" in c for c in result.caveats)


def test_profit_take_fires_when_target_hit(catalyst_db, bar_root):
    """Event publishes; bars rise 1.5% within 5 min → profit_take exit."""
    event_ts = datetime(2024, 10, 15, 14, 35, tzinfo=timezone.utc)  # 14:35 UTC = 10:35 ET
    # 10 minute bars, prices rising 0.3% each minute → 3% in 10 min, hits 1% target by min 4
    bars = []
    for i in range(60):
        ts = event_ts + timedelta(minutes=i + 1)
        price = 100.0 * (1 + 0.003 * i)
        bars.append({"timestamp": ts, "open": price, "high": price, "low": price, "close": price, "volume": 1000})
    _write_bars(bar_root, "AAPL", 2024, bars)
    insert_event(catalyst_db, _event("AAPL", "earnings", "report", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "profit_take"
    assert t.return_pct >= 0.5  # Net of slippage; target was 1%, slippage trims
    assert t.symbol == "AAPL"


def test_stop_loss_fires_when_drawdown_breached(catalyst_db, bar_root):
    """Event publishes; bars fall 2% → stop_loss exit at -1.5%."""
    event_ts = datetime(2024, 10, 15, 14, 35, tzinfo=timezone.utc)
    bars = []
    for i in range(60):
        ts = event_ts + timedelta(minutes=i + 1)
        price = 100.0 * (1 - 0.005 * i)  # falling 0.5%/min
        bars.append({"timestamp": ts, "open": price, "high": price, "low": price, "close": price, "volume": 1000})
    _write_bars(bar_root, "MSFT", 2024, bars)
    insert_event(catalyst_db, _event("MSFT", "earnings", "report", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].return_pct < 0


def test_time_stop_when_neither_target_nor_stop_hit(catalyst_db, bar_root):
    """Bars drift sideways; max_hold expires → time_stop exit."""
    event_ts = datetime(2024, 10, 15, 14, 35, tzinfo=timezone.utc)
    bars = []
    for i in range(120):
        ts = event_ts + timedelta(minutes=i + 1)
        price = 100.0 + (i % 2) * 0.05  # noise around 100, no trend
        bars.append({"timestamp": ts, "open": price, "high": price, "low": price, "close": price, "volume": 1000})
    _write_bars(bar_root, "NVDA", 2024, bars)
    insert_event(catalyst_db, _event("NVDA", "earnings", "report", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "time_stop"


def test_no_lookahead_event_with_no_post_event_bar_skipped(catalyst_db, bar_root):
    """Event lands AFTER all bars; cannot enter → skipped, no crash."""
    bars = [{
        "timestamp": datetime(2024, 10, 14, 14, 35, tzinfo=timezone.utc),
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000,
    }]
    _write_bars(bar_root, "AAPL", 2024, bars)
    # Event AFTER the only bar
    event_ts = datetime(2024, 10, 16, 14, 35, tzinfo=timezone.utc)
    insert_event(catalyst_db, _event("AAPL", "earnings", "report", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    # No trade possible — bar timestamp is BEFORE event_ts so no post-event bar
    assert result.trades == []


def test_event_age_cap_skips_stale_events(catalyst_db, bar_root):
    """Event lands at 16:00 ET (post-close); next bar is 14:30 ET next day
    (>16h later) → exceeds 60min max_event_age → skipped."""
    event_ts = datetime(2024, 10, 15, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
    next_open_bar = datetime(2024, 10, 16, 13, 30, tzinfo=timezone.utc)  # next-day pre-mkt
    bars = [{
        "timestamp": next_open_bar,
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000,
    }]
    _write_bars(bar_root, "TSLA", 2024, bars)
    insert_event(catalyst_db, _event("TSLA", "earnings", "report", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db,
        bar_root=bar_root,
        signal_factory=lambda: None,
        category="earnings", subcategory="report",
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert result.trades == []
    assert any("too_late" in c or "max_event_age" in c for c in result.caveats)


def test_query_filters_by_category_and_subcategory(catalyst_db, bar_root):
    """Inserted target_raise event should NOT match an earnings/report query."""
    event_ts = datetime(2024, 10, 15, 14, 35, tzinfo=timezone.utc)
    bars = []
    for i in range(60):
        ts = event_ts + timedelta(minutes=i + 1)
        bars.append({"timestamp": ts, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000})
    _write_bars(bar_root, "GOOG", 2024, bars)
    insert_event(catalyst_db, _event("GOOG", "analyst", "target_raise", event_ts))

    result = replay_catalyst_signal(
        catalyst_db_path=catalyst_db, bar_root=bar_root, signal_factory=lambda: None,
        category="earnings", subcategory="report",  # different from inserted
        start=datetime(2024, 10, 1, tzinfo=timezone.utc),
        end=datetime(2024, 10, 31, tzinfo=timezone.utc),
        max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5,
        max_event_age_minutes=60,
    )
    assert result.trades == []
