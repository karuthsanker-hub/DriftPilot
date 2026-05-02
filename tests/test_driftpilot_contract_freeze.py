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


def test_contract_signal_layer_dataclasses_exist() -> None:
    """Cross-signal contracts: every locked spec depends on these types."""
    from driftpilot.signals import (
        BlockedReason as ReExportedBlockedReason,
        Candidate,
        ExitDecision,
        no_exit_decision,
    )

    # BlockedReason must be reachable from the signals package
    assert ReExportedBlockedReason is BlockedReason

    # Candidate carries blocked_reason as the typed enum, not raw strings
    candidate = Candidate(
        symbol="ABC",
        score=1.5,
        sector="Technology",
        allowed=False,
        blocked_reason=BlockedReason.ADX_TOO_HIGH,
        features={"adx": 35.0},
    )
    assert candidate.blocked_reason is BlockedReason.ADX_TOO_HIGH
    assert candidate.features["adx"] == 35.0

    # Allowed candidates default to no blocked_reason and empty features
    allowed = Candidate(symbol="XYZ", score=2.0, sector="Energy", allowed=True)
    assert allowed.blocked_reason is None
    assert dict(allowed.features) == {}

    # ExitDecision: explicit exit
    exit_now = ExitDecision(should_exit=True, exit_reason="TARGET", metadata={"pnl_pct": 0.012})
    assert exit_now.should_exit
    assert exit_now.exit_reason == "TARGET"

    # ExitDecision: default no-op
    no_op = no_exit_decision()
    assert not no_op.should_exit
    assert no_op.exit_reason is None


def test_contract_blocked_reason_covers_all_locked_signals() -> None:
    """Every reason taxonomy from the four locked spec docs must be present."""
    required = {
        # stationary_ghost_v1
        "outside_scan_window",
        "below_adv_floor",
        "outside_price_corridor",
        "adx_too_high",
        "not_extended_enough",
        "pullback_volume_too_high",
        "stock_red_on_day",
        # whale_tail_v1
        "rvol_too_low",
        "not_compressed",
        "not_in_upper_range",
        "distribution_break_invalidated",
        # rs_drift_v1
        "rs_below_threshold",
        "below_post_open_vwap",
        "below_opening_range_high",
        "daily_profit_target_hit",
        # apex_hunter_v2_2
        "r2_too_low",
        "slope_negative_or_decelerating",
        "alpha_too_low",
        "correlation_too_low",
        "not_top_1pct",
        # intraday_momentum_v1 (existing)
        "stale_bar",
        "quote_unavailable",
        "sector_cap_reached",
    }
    have = {member.value for member in BlockedReason}
    missing = required - have
    assert not missing, f"BlockedReason missing values from locked specs: {sorted(missing)}"
