from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pandas as pd  # type: ignore[import-untyped]
import pytest

from driftpilot.backtest.metrics import compute_metrics
from driftpilot.backtest.replay import BacktestTrade, load_parquet_bars
from driftpilot.backtest.report import build_expectancy_report, write_expectancy_report
from driftpilot.dashboard.view_models import backtest_report_payload
from driftpilot.settings import DriftPilotSettings


def _databento_pull_module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "databento_pull.py"
    spec = importlib.util.spec_from_file_location("databento_pull", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


databento_pull = _databento_pull_module()


def test_databento_frame_normalizes_and_writes_symbol_year_cache(tmp_path) -> None:
    raw = pd.DataFrame(
        {
            "ts_event": [datetime(2024, 1, 2, 14, 30, tzinfo=UTC)],
            "symbol": ["spy"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.5],
            "volume": [12345],
        }
    )

    normalized = databento_pull.normalize_databento_frame(raw)
    written = databento_pull.write_symbol_year_cache(normalized, tmp_path)
    loaded = load_parquet_bars(tmp_path, start=date(2024, 1, 1), end=date(2024, 12, 31))

    assert written == [tmp_path / "SPY" / "2024.parquet"]
    assert loaded.loc[0, "symbol"] == "SPY"
    assert loaded.loc[0, "close"] == pytest.approx(100.5)


def test_load_symbols_includes_spy_and_sector_map_symbols(tmp_path) -> None:
    symbols_file = tmp_path / "sector_map.csv"
    symbols_file.write_text("symbol,sector\nnvda,Technology\n", encoding="utf-8")

    assert databento_pull.load_symbols(["aapl, msft"], symbols_file) == ["AAPL", "MSFT", "NVDA", "SPY"]


def test_databento_cost_estimate_batches_before_pull() -> None:
    class Metadata:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def get_cost(self, *, symbols: list[str], **_: object) -> float:
            self.calls.append(symbols)
            return 1.25

    class Client:
        def __init__(self) -> None:
            self.metadata = Metadata()

    client = Client()
    config = databento_pull.PullConfig(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        symbols=("AAPL", "MSFT", "NVDA"),
        batch_size=2,
    )

    assert databento_pull.estimate_databento_cost(client, config) == pytest.approx(2.5)
    assert client.metadata.calls == [["AAPL", "MSFT"], ["NVDA"]]


def test_phase12_report_shape_renders_from_file(tmp_path) -> None:
    settings = DriftPilotSettings()
    trade = BacktestTrade(
        symbol="AAA",
        entry_at=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        exit_at=datetime(2024, 2, 2, 15, 0, tzinfo=UTC),
        quantity=10,
        entry_reference_price=100,
        entry_price=100.05,
        exit_reference_price=101,
        exit_price=100.95,
        gross_pnl=10,
        net_pnl=9,
        slippage_cost=1,
        return_pct=9 / 1000.5,
        hold_minutes=45,
        exit_reason="TARGET",
        regime="GREEN",
    )
    replay = type(
        "Replay",
        (),
        {
            "trades": [trade],
            "equity_curve": [(trade.exit_at, 10_009.0)],
            "starting_capital": 10_000.0,
            "ending_capital": 10_009.0,
            "caveats": [],
        },
    )()

    report = build_expectancy_report(
        replay,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        settings=settings,
        point_in_time_constituents=False,
    )
    path = write_expectancy_report(report, tmp_path / "expectancy_report.json")
    payload = backtest_report_payload(path)

    assert payload["source"] == "file"
    assert payload["monthly_returns"]
    assert payload["survivorship_bias_note"] is True
    assert payload["headline_metrics"]["expectancy_per_dollar"] > 0
    assert payload["performance_by_regime"]["GREEN"]["trades"] == 1


def test_monthly_returns_are_calculated_from_daily_pnl() -> None:
    start = datetime(2024, 1, 31, 14, 30, tzinfo=UTC)
    trades = [
        BacktestTrade(
            symbol="AAA",
            entry_at=start,
            exit_at=start + timedelta(minutes=index),
            quantity=1,
            entry_reference_price=100,
            entry_price=100,
            exit_reference_price=101,
            exit_price=101,
            gross_pnl=1,
            net_pnl=1,
            slippage_cost=0,
            return_pct=0.01,
            hold_minutes=1,
            exit_reason="TARGET",
            regime="GREEN",
        )
        for index in (1, 1441)
    ]

    metrics = compute_metrics(trades, starting_capital=100)

    assert metrics.monthly_returns["2024-01"] == pytest.approx(0.01)
    assert metrics.monthly_returns["2024-02"] == pytest.approx(1 / 101)
