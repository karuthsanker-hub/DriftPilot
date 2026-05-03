from __future__ import annotations
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from driftpilot.catalyst.db import init_catalyst_schema, insert_event
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.universe_filter import CatalystUniverseFilter


def _event(symbol, category, subcategory, ts, pillar="micro"):
    h = hashlib.sha256(f"{symbol}|{category}|{subcategory}|{ts.isoformat()}".encode()).hexdigest()[:16]
    return CatalystEvent(
        symbol=symbol, category=category, subcategory=subcategory, pillar=pillar,
        ts=ts, headline=f"{symbol} {category} {subcategory}", source="test",
        horizon_minutes=60, headline_hash=h,
    )


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "uf.db")
    init_catalyst_schema(p)
    return p


def test_empty_input_returns_empty(db_path):
    f = CatalystUniverseFilter(db_path)
    assert f.filter_and_rank([]) == []


def test_no_db_returns_input(db_path):
    f = CatalystUniverseFilter(db_path=None)
    assert f.filter_and_rank(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


def test_drops_negative_catalyst_symbol(db_path):
    now = datetime.now(timezone.utc)
    insert_event(db_path, _event("BAD", "analyst", "target_cut", now - timedelta(minutes=60)))

    f = CatalystUniverseFilter(db_path)
    out = f.filter_and_rank(["AAPL", "BAD", "MSFT"], now=now)
    assert "BAD" not in out
    assert set(out) == {"AAPL", "MSFT"}


def test_ranks_positive_catalyst_first(db_path):
    now = datetime.now(timezone.utc)
    insert_event(db_path, _event("MSFT", "earnings", "report", now - timedelta(minutes=30)))
    insert_event(db_path, _event("AAPL", "analyst", "target_raise", now - timedelta(minutes=10)))

    f = CatalystUniverseFilter(db_path)
    out = f.filter_and_rank(["NVDA", "MSFT", "AAPL"], now=now)
    assert out[0] == "AAPL"
    assert out[1] == "MSFT"
    assert out[2] == "NVDA"


def test_negative_wins_over_positive(db_path):
    now = datetime.now(timezone.utc)
    insert_event(db_path, _event("MIXED", "analyst", "target_raise", now - timedelta(minutes=20)))
    insert_event(db_path, _event("MIXED", "analyst", "target_cut", now - timedelta(minutes=10)))

    f = CatalystUniverseFilter(db_path)
    out = f.filter_and_rank(["AAPL", "MIXED"], now=now)
    assert "MIXED" not in out


def test_old_events_outside_lookback_ignored(db_path):
    now = datetime.now(timezone.utc)
    insert_event(db_path, _event("OLD", "earnings", "report", now - timedelta(hours=8)))

    f = CatalystUniverseFilter(db_path, lookback_minutes=240)
    out = f.filter_and_rank(["OLD", "NEW"], now=now)
    assert out == ["OLD", "NEW"]


def test_db_unreachable_returns_input(tmp_path):
    f = CatalystUniverseFilter(db_path="/nonexistent/dir/that/does/not/exist/db.sqlite")
    out = f.filter_and_rank(["AAPL", "MSFT"])
    assert out == ["AAPL", "MSFT"]


def test_1500_symbol_scenario(db_path):
    now = datetime.now(timezone.utc)
    universe = [f"SYM{i:04d}" for i in range(1500)]
    positives = universe[:50]
    negatives = universe[50:55]

    for i, sym in enumerate(positives):
        insert_event(db_path, _event(sym, "earnings", "report", now - timedelta(minutes=i + 1)))
    for sym in negatives:
        insert_event(db_path, _event(sym, "analyst", "target_cut", now - timedelta(minutes=30)))

    f = CatalystUniverseFilter(db_path)
    out = f.filter_and_rank(universe, now=now)
    assert len(out) == 1495
    assert set(out[:50]) == set(positives)
    assert not any(s in out for s in negatives)
