from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from driftpilot.clock import FixedClock

from scripts.slot_manager import (
    find_slot_inventory_issues,
    find_stale_reserved_slots,
    decide_premarket_clean,
    run_safety_checks,
)


def test_slot_inventory_flags_active_slot_with_invalid_position_id() -> None:
    slots = [
        {"slot_id": 1, "status": "OPEN", "symbol": "AAPL", "position_id": None},
        {"slot_id": 2, "status": "ENTERING", "symbol": "MSFT", "position_id": "x"},
        {"slot_id": 3, "status": "EXITING", "symbol": "TSLA", "position_id": 0},
    ]

    issues = find_slot_inventory_issues(slots, open_positions=[])

    assert [issue.code for issue in issues] == [
        "active_slot_invalid_position_id",
        "active_slot_invalid_position_id",
        "active_slot_invalid_position_id",
    ]
    assert {issue.slot_id for issue in issues} == {1, 2, 3}


def test_slot_inventory_flags_slot_position_mismatches() -> None:
    slots = [
        {"slot_id": 1, "status": "OPEN", "symbol": "AAPL", "position_id": 10},
        {"slot_id": 2, "status": "OPEN", "symbol": "MSFT", "position_id": 20},
    ]
    open_positions = [
        {"id": 10, "symbol": "AAPL", "slot_id": 1},
        {"id": 20, "symbol": "NVDA", "slot_id": 2},
        {"id": 30, "symbol": "TSLA", "slot_id": 3},
    ]

    issues = find_slot_inventory_issues(slots, open_positions)

    assert [(issue.code, issue.slot_id, issue.position_id) for issue in issues] == [
        ("active_slot_symbol_mismatch", 2, 20),
        ("open_position_without_active_slot", None, 30),
    ]


def test_stale_reserved_detection_ignores_active_slots() -> None:
    now = datetime(2026, 5, 15, 13, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=10)).isoformat()
    fresh = (now - timedelta(minutes=2)).isoformat()
    slots = [
        {"slot_id": 1, "status": "RESERVED", "symbol": "AAPL", "updated_at": old},
        {"slot_id": 2, "status": "RESERVED", "symbol": "MSFT", "updated_at": fresh},
        {"slot_id": 3, "status": "OPEN", "symbol": "TSLA", "updated_at": old},
    ]

    stale = find_stale_reserved_slots(slots, now=now, stale_minutes=5)

    assert stale == [{"slot_id": 1, "symbol": "AAPL", "age_minutes": 10.0}]


def test_premarket_clean_refuses_without_explicit_broker_flat() -> None:
    clock = FixedClock(fixed_now=datetime(2026, 5, 15, 13, 0, tzinfo=UTC))

    decision = decide_premarket_clean(
        now=clock.now_utc(),
        operator_alive=False,
        local_open_position_count=0,
        broker_flat_confirmed=None,
        clock=clock,
    )

    assert not decision.allowed
    assert "broker-flat confirmation" in decision.reason


def test_premarket_clean_refuses_after_market_open() -> None:
    clock = FixedClock(fixed_now=datetime(2026, 5, 15, 14, 0, tzinfo=UTC))

    decision = decide_premarket_clean(
        now=clock.now_utc(),
        operator_alive=False,
        local_open_position_count=0,
        broker_flat_confirmed=True,
        clock=clock,
    )

    assert not decision.allowed
    assert "09:30 ET" in decision.reason


def test_premarket_clean_allows_only_when_all_gates_pass() -> None:
    clock = FixedClock(fixed_now=datetime(2026, 5, 15, 13, 0, tzinfo=UTC))

    decision = decide_premarket_clean(
        now=clock.now_utc(),
        operator_alive=False,
        local_open_position_count=0,
        broker_flat_confirmed=True,
        clock=clock,
    )

    assert decision.allowed


def test_safety_checks_warn_on_active_anomaly_without_recycling(monkeypatch, caplog) -> None:
    recycled: list[int] = []

    def fake_recycle(slot_id: int, reason: str) -> None:
        recycled.append(slot_id)

    monkeypatch.setattr("scripts.slot_manager._recycle_slot", fake_recycle)
    health = {
        "slots": {
            "stale_reserved": [],
            "inventory_issues": [
                {
                    "code": "active_slot_invalid_position_id",
                    "message": "OPEN slot 1 has null/invalid position_id",
                }
            ],
        },
        "operator": {
            "alive": True,
            "log_stale": False,
            "log_freshness_seconds": 1.0,
        },
    }

    actions = run_safety_checks(health)

    assert actions == 1
    assert recycled == []
    assert "active_slot_invalid_position_id" in caplog.text
