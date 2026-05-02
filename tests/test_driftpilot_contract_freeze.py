from __future__ import annotations

from datetime import UTC, date, datetime

from driftpilot.clock import FixedClock
from driftpilot.states import BlockedReason
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)


def test_contract_candidate_counter_recycle_and_fill_methods(tmp_path) -> None:
    repo = DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))
    repo.slots.upsert(1, status="EMPTY", slot_value=1_000, updated_at=NOW)
    order = repo.orders.create(
        symbol="abc",
        side="buy",
        order_type="limit",
        status="submitted",
        quantity=1,
        slot_id=1,
        submitted_at=NOW,
    )

    repo.upsert_candidate_queue_row(
        symbol="abc",
        score=2.5,
        rvol=3.2,
        vwap_distance_pct=0.01,
        return_15m_pct=0.007,
        sector="Technology",
        blocked_reason=None,
        queue_status="queued",
        cycle_at=NOW,
    )
    assert repo.list_candidates(limit=1)[0].symbol == "ABC"

    assert repo.increment_daily_counter(
        date_et=date(2026, 5, 1),
        counter_name="trades",
        delta=2,
    ) == 2
    assert repo.get_daily_counter(date_et=date(2026, 5, 1), counter_name="trades") == 2

    repo.record_recycle_event(
        slot_id=1,
        freed_symbol="ABC",
        exit_reason="TARGET",
        exit_pnl_pct=0.01,
        replacement_symbol="XYZ",
        at=NOW,
    )
    assert repo.list_recycle_events(limit=1)[0].replacement_symbol == "XYZ"

    repo.record_fill(
        order_id=order.id,
        symbol="ABC",
        side="buy",
        qty=1,
        reference_price=100,
        slippage_applied=0.05,
        fill_price=100.05,
        at=NOW,
    )
    fill_metadata = repo.fills.list_all()[0].metadata
    assert fill_metadata is not None
    assert fill_metadata["slippage_applied"] == 0.05


def test_contract_blocked_reason_enum_is_locked() -> None:
    assert BlockedReason.STALE_BAR.value == "stale_bar"
    assert BlockedReason.QUOTE_UNAVAILABLE.value == "quote_unavailable"
