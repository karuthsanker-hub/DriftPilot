"""Regression: signals expose evaluate_exit and the harness honors it.

The four locked v1 specs (Whale-Tail, RS-Drift, Apex Hunter) all rely on
custom exit logic via `signal.evaluate_exit(position, latest_bar, settings)`.
Stationary Ghost intentionally falls through to default TARGET/STOP/TIME.
This test pins the contract: a fake signal that always wants to exit forces
the harness to use that reason instead of TARGET/STOP/TIME.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd  # type: ignore[import-untyped]

from driftpilot.backtest.replay import replay_bars
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals import (
    DEFAULT_SIGNAL,
    ExitDecision,
    SignalProtocol,
    register_signal,
)
from driftpilot.signals.intraday_momentum_v1 import IntradayMomentumV1Signal


START = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    # 20 days of prior history at flat 100 so RVOL/15m have a baseline.
    for history_day in range(20, 0, -1):
        for minute in range(47):
            timestamp = START - timedelta(days=history_day) + timedelta(minutes=minute)
            for symbol, volume in {"SPY": 1000, "AAA": 100}.items():
                rows.append(
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "volume": volume,
                    }
                )
    # Today: AAA rallies enough to enter; SPY drifts up.
    for minute in range(47):
        timestamp = START + timedelta(minutes=minute)
        spy_close = 100 + (minute * 0.01)
        aaa_close = 100 + (minute * 0.06)
        rows.extend(
            [
                {
                    "timestamp": timestamp,
                    "symbol": "SPY",
                    "open": spy_close - 0.01,
                    "high": spy_close + 0.02,
                    "low": spy_close - 0.02,
                    "close": spy_close,
                    "volume": 1000,
                },
                {
                    "timestamp": timestamp,
                    "symbol": "AAA",
                    "open": aaa_close - 0.02,
                    "high": aaa_close + 0.03,
                    "low": aaa_close - 0.03,
                    "close": aaa_close,
                    "volume": 300 if minute >= 20 else 100,
                },
            ]
        )
    return pd.DataFrame(rows)


class _AlwaysExitSignal:
    """Wraps the default signal but always wants to exit immediately on
    the next bar with reason CUSTOM_EXIT — proves the harness routes through
    evaluate_exit rather than the default TARGET/STOP/TIME branches.
    """

    name = "always_exit_v0"
    version = "0"

    def __init__(self) -> None:
        self._inner = IntradayMomentumV1Signal()

    def scan(self, *args, **kwargs):
        return self._inner.scan(*args, **kwargs)

    def evaluate_exit(self, position, latest_bar, settings) -> ExitDecision:  # noqa: ARG002
        return ExitDecision(
            should_exit=True,
            exit_reason="CUSTOM_EXIT",
            metadata={"latest_close": latest_bar.close},
        )


def test_signal_protocol_remains_compatible() -> None:
    """The intraday reference still satisfies SignalProtocol after contract changes."""
    assert isinstance(IntradayMomentumV1Signal(), SignalProtocol)


def test_replay_uses_signal_evaluate_exit_when_provided() -> None:
    register_signal("always_exit_v0", lambda: _AlwaysExitSignal())
    settings = DriftPilotSettings(active_signal="always_exit_v0")

    result = replay_bars(_bars(), settings=settings, rvol_lookback=20)

    assert result.trades, "expected at least one trade so we can assert exit reason"
    custom_exits = [trade for trade in result.trades if trade.exit_reason == "CUSTOM_EXIT"]
    assert custom_exits, (
        f"signal.evaluate_exit was ignored — exit reasons seen: "
        f"{[t.exit_reason for t in result.trades]}"
    )


def test_default_signal_still_uses_default_exits() -> None:
    """Without evaluate_exit, the harness falls back to TARGET/STOP/TIME."""
    settings = DriftPilotSettings(active_signal=DEFAULT_SIGNAL)

    result = replay_bars(_bars(), settings=settings, rvol_lookback=20)

    if result.trades:
        for trade in result.trades:
            assert trade.exit_reason in {"TARGET", "STOP", "TIME", "end_of_replay"}, (
                f"unexpected exit reason for default signal: {trade.exit_reason}"
            )
