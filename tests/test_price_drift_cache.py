from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from driftpilot.clock import FixedClock
from driftpilot.storage.repositories import DriftPilotRepository


def test_price_drift_baseline_repository_persists_first_seen_price(tmp_path) -> None:
    now = datetime(2026, 5, 13, 14, 30, tzinfo=UTC)
    db_path = tmp_path / "ops.db"
    first_repo = DriftPilotRepository.open(db_path, FixedClock(fixed_now=now))

    first = first_repo.price_drift_baselines.update_seen(
        symbol="drft",
        event_key="headline-1",
        price=100.0,
        seen_at=now,
        metadata={"headline": "DriftCo beats estimates"},
    )

    restarted_repo = DriftPilotRepository.open(
        db_path, FixedClock(fixed_now=now + timedelta(minutes=2))
    )
    updated = restarted_repo.price_drift_baselines.update_seen(
        symbol="DRFT",
        event_key="headline-1",
        price=104.0,
        seen_at=now + timedelta(minutes=2),
        metadata={"source": "catalyst_bus"},
    )

    assert first.first_seen_price == pytest.approx(100.0)
    assert updated.first_seen_price == pytest.approx(100.0)
    assert updated.last_seen_price == pytest.approx(104.0)
    assert updated.drift_pct == pytest.approx(4.0)
    assert updated.first_seen_at == now
    assert updated.last_seen_at == now + timedelta(minutes=2)
    assert updated.metadata == {
        "headline": "DriftCo beats estimates",
        "source": "catalyst_bus",
    }
