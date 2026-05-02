from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftpilot.clock import FixedClock, datetime_from_storage, datetime_to_storage
from driftpilot.settings import load_settings
from driftpilot.storage.repositories import (
    DriftPilotRepository,
    connect,
    initialize_schema,
    list_user_tables,
    primary_key_columns,
)


def test_settings_loads_phase_one_defaults_and_env_overrides(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MODE=paper\n"
        "DRIFTPILOT_SQLITE_PATH=state/operator.sqlite3\n"
        "DRIFTPILOT_TIMEZONE=America/New_York\n"
        "OPERATOR_TRADE_SLOTS=10\n"
        "MAX_TRADES_PER_DAY=50\n"
        "MAX_TRADES_PER_SYMBOL_PER_DAY=3\n"
        "DAILY_LOSS_LIMIT_PCT=0.03\n"
    )
    for key in (
        "MODE",
        "DRIFTPILOT_SQLITE_PATH",
        "DRIFTPILOT_TIMEZONE",
        "OPERATOR_TRADE_SLOTS",
        "MAX_TRADES_PER_DAY",
        "MAX_TRADES_PER_SYMBOL_PER_DAY",
        "DAILY_LOSS_LIMIT_PCT",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(env_path)

    assert settings.mode == "paper"
    assert settings.sqlite_path == "state/operator.sqlite3"
    assert settings.timezone == "America/New_York"
    assert settings.trade_slots == 10
    assert settings.max_trades_per_day == 50
    assert settings.max_trades_per_symbol_per_day == 3
    assert settings.daily_loss_limit_pct == 0.03


def test_settings_falls_back_for_invalid_daily_loss_limit(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DAILY_LOSS_LIMIT_PCT=-2.0\n")
    monkeypatch.delenv("DAILY_LOSS_LIMIT_PCT", raising=False)

    settings = load_settings(env_path)

    assert settings.daily_loss_limit_pct == 0.03


def test_schema_creates_all_phase_one_tables_cleanly() -> None:
    connection = connect(":memory:")
    initialize_schema(connection)

    table_names = list_user_tables(connection)

    assert {
        "operator_state",
        "state_transitions",
        "slots",
        "positions",
        "orders",
        "fills",
        "candidate_queue",
        "recycle_events",
        "daily_pnl",
        "daily_counters",
        "live_gate_evaluations",
        "errors",
        "allocator_state",
        "universe",
        "sector_map",
    } <= table_names

    assert primary_key_columns(connection, "daily_counters") == [
        ("date_et", 1),
        ("counter_name", 2),
    ]


def test_state_and_slots_survive_simulated_restart(tmp_path) -> None:
    db_path = tmp_path / "operator.sqlite3"
    first_clock = FixedClock(fixed_now=datetime(2026, 4, 30, 13, 30, tzinfo=UTC))
    first_repo = DriftPilotRepository.open(db_path, first_clock)
    transition = first_repo.transitions.append(
        from_state="BOOT",
        to_state="SCANNING",
        reason="market open",
        metadata={"gate": "passed"},
    )
    first_repo.state.set("SCANNING", last_transition_id=transition.id, metadata={"heartbeat": "SPY"})
    first_repo.slots.upsert(1, status="available", slot_value=1_000)
    first_repo.connection.close()

    second_repo = DriftPilotRepository.open(db_path, FixedClock(fixed_now=datetime(2026, 4, 30, 14, 0, tzinfo=UTC)))

    state = second_repo.state.get()
    slot = second_repo.slots.get(1)
    latest_transition = second_repo.transitions.latest()

    assert state is not None
    assert state.current_state == "SCANNING"
    assert state.last_transition_id == transition.id
    assert state.updated_at.tzinfo is not None
    assert slot is not None
    assert slot.status == "available"
    assert latest_transition is not None
    assert latest_transition.metadata == {"gate": "passed"}


def test_daily_counters_reset_only_when_date_et_changes_not_restart(tmp_path) -> None:
    db_path = tmp_path / "operator.sqlite3"
    april_30 = FixedClock(fixed_now=datetime(2026, 4, 30, 16, 0, tzinfo=UTC))
    first_repo = DriftPilotRepository.open(db_path, april_30)
    first_repo.daily_counters.increment("trades_total")
    first_repo.daily_counters.increment("trades_total")
    first_repo.connection.close()

    restart_same_day = DriftPilotRepository.open(db_path, april_30)
    same_day = restart_same_day.daily_counters.get("trades_total")
    restart_same_day.connection.close()

    may_1 = DriftPilotRepository.open(db_path, FixedClock(fixed_now=datetime(2026, 5, 1, 16, 0, tzinfo=UTC)))
    next_day = may_1.daily_counters.get("trades_total")

    assert same_day.counter_value == 2
    assert same_day.date_et.isoformat() == "2026-04-30"
    assert next_day.counter_value == 0
    assert next_day.date_et.isoformat() == "2026-05-01"


def test_timezone_aware_datetimes_round_trip_without_naive_corruption(tmp_path) -> None:
    db_path = tmp_path / "operator.sqlite3"
    timestamp = datetime(2026, 4, 30, 9, 31, tzinfo=FixedClock().timezone)
    repo = DriftPilotRepository.open(db_path, FixedClock(fixed_now=timestamp))

    transition = repo.transitions.append(
        from_state="REGIME_CHECK",
        to_state="SCANNING",
        reason="regime ok",
        timestamp=timestamp,
    )
    repo.connection.close()

    reopened = DriftPilotRepository.open(db_path)
    latest = reopened.transitions.latest()

    assert latest is not None
    assert latest.id == transition.id
    assert latest.timestamp.tzinfo is not None
    assert latest.timestamp.utcoffset() is not None
    assert latest.timestamp.isoformat() == timestamp.isoformat()


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_to_storage(datetime(2026, 4, 30, 9, 31))

    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_from_storage("2026-04-30T09:31:00")
