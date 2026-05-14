from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from driftpilot.operator import _live_signal_names
from driftpilot.services_live import MultiSignal, compute_dynamic_bands
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.volume_spike_v1 import VolumeSpikeConfig, VolumeSpikeV1Signal


@dataclass
class _StaticSignal:
    name: str
    candidates: list[Candidate]
    exit_decision: ExitDecision | None = None
    exit_calls: int = 0

    def scan(self, now: datetime | None = None) -> list[Candidate]:
        return self.candidates

    def evaluate_exit(self, position: Any, now: datetime) -> ExitDecision | None:
        self.exit_calls += 1
        return self.exit_decision


def test_live_signal_names_adds_volume_spike_to_default_and_configured_signals() -> None:
    assert _live_signal_names("intraday_momentum_v1") == [
        "earnings_report_v1",
        "filing_8a_v1",
        "volume_spike_v1",
    ]
    assert _live_signal_names("analyst_target_raise_v1") == [
        "analyst_target_raise_v1",
        "volume_spike_v1",
    ]
    assert _live_signal_names("earnings_report_v1,volume_spike_v1") == [
        "earnings_report_v1",
        "volume_spike_v1",
    ]


@pytest.mark.asyncio
async def test_multisignal_stamps_signal_name_dedupes_by_symbol_and_routes_exits() -> None:
    analyst = _StaticSignal(
        name="analyst_target_raise_v1",
        candidates=[
            Candidate(
                symbol="AAPL",
                score=1.0,
                sector="Technology",
                allowed=True,
                features={"headline": "AAPL target raised"},
            )
        ],
        exit_decision=ExitDecision(should_exit=False),
    )
    volume = _StaticSignal(
        name="volume_spike_v1",
        candidates=[
            Candidate(
                symbol="AAPL",
                score=2.5,
                sector="Technology",
                allowed=True,
                features={"rvol": 3.7},
            )
        ],
        exit_decision=ExitDecision(should_exit=True, exit_reason="TARGET"),
    )

    signal = MultiSignal([analyst, volume])
    candidates = await signal.scan(now=datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc))

    assert len(candidates) == 1
    assert candidates[0].score == pytest.approx(2.5)
    assert candidates[0].features["signal_name"] == "volume_spike_v1"
    assert candidates[0].features["rvol"] == pytest.approx(3.7)

    position = SimpleNamespace(metadata={"signal_name": "volume_spike_v1"})
    decision = signal.evaluate_exit(position, datetime(2026, 5, 14, 20, 1, tzinfo=timezone.utc))

    assert decision is not None
    assert decision.exit_reason == "TARGET"
    assert volume.exit_calls == 1
    assert analyst.exit_calls == 0


def test_volume_spike_candidates_include_signal_name_and_rvol() -> None:
    signal = VolumeSpikeV1Signal(
        VolumeSpikeConfig(
            min_volume_abs=1,
            min_volume_ratio=1.5,
            min_price_change_pct=0.1,
            max_candidates=5,
        ),
        symbols=["VOL"],
        clock=lambda: datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc),
    )
    signal._avg_volume_loaded = True
    signal._avg_volume_cache = {"VOL": 1_000_000}
    signal._fetch_snapshots = lambda: {
        "VOL": SimpleNamespace(
            daily_bar=SimpleNamespace(
                volume=3_000_000,
                open=10.0,
                close=11.0,
                vwap=10.5,
            )
        )
    }

    candidates = signal.scan(now=datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc))

    assert len(candidates) == 1
    features = candidates[0].features
    assert features["signal_name"] == "volume_spike_v1"
    assert features["signal_type"] == "volume_spike"
    assert features["rvol"] == pytest.approx(3.0)


def test_dynamic_bands_accept_volume_spike_rvol_without_atr() -> None:
    bands = compute_dynamic_bands(
        entry_price=10.0,
        reference_price=10.0,
        atr_pct=None,
        rvol=3.0,
        signal_name="volume_spike_v1",
        default_target_pct=0.01,
        default_stop_pct=0.01,
    )

    assert bands.target_pct > bands.stop_pct
    assert "no ATR" in bands.reasoning
    assert "catalyst_profile=volume_spike_v1" in bands.reasoning
    assert "rvol_boost" in bands.reasoning
