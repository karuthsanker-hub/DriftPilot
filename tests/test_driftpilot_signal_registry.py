from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd  # type: ignore[import-untyped]
import pytest

from driftpilot.backtest.replay import replay_bars
from driftpilot.backtest.report import build_expectancy_report, default_report_path
from driftpilot.signals import DEFAULT_SIGNAL, get_signal, list_signals
from driftpilot.signals.base import SignalProtocol
from driftpilot.settings import DriftPilotSettings, load_settings


START = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
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


def test_default_signal_registry_resolves_intraday_momentum_v1() -> None:
    signal = get_signal()

    assert isinstance(signal, SignalProtocol)
    assert signal.name == "intraday_momentum_v1"
    assert signal.version == "1"
    assert DEFAULT_SIGNAL in list_signals()


def test_unknown_signal_has_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown signal"):
        get_signal("missing_signal")


def test_active_signal_setting_defaults_and_loads_from_env() -> None:
    assert DriftPilotSettings().active_signal == "intraday_momentum_v1"

    settings = load_settings(None, environ={"ACTIVE_SIGNAL": "intraday_momentum_v1"})

    assert settings.active_signal == "intraday_momentum_v1"


def test_backtest_report_includes_signal_metadata() -> None:
    settings = DriftPilotSettings(active_signal="intraday_momentum_v1")
    result = replay_bars(_bars(), settings=settings, rvol_lookback=20)

    report = build_expectancy_report(
        result,
        start=_bars()["timestamp"].dt.date.min(),
        end=_bars()["timestamp"].dt.date.max(),
        settings=settings,
        point_in_time_constituents=False,
    )

    assert report["signal"] == {"name": "intraday_momentum_v1", "version": "1"}
    assert report["run_config"]["signal"] == "intraday_momentum_v1"
    assert report["run_config"]["signal_version"] == "1"
    path = default_report_path(report)
    assert path.parts[:2] == ("reports", "intraday_momentum_v1")
    assert path.name.endswith(f"_{report['verdict'].lower()}.json")
