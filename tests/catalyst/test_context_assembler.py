from __future__ import annotations

import sqlite3
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from driftpilot.catalyst.context_assembler import (
    ContextAssembler,
    EnrichmentContext,
    _fetch_yfinance_atr_pct,
    _fetch_yfinance_profile,
)
from driftpilot.clock import DriftPilotClock


@dataclass
class _Profile:
    market_cap_m: float
    avg_volume: float
    beta: float


@dataclass
class _Fundamentals:
    earnings_surprises_pct: list[float]


class _MarketData:
    def __init__(self) -> None:
        self.profile_calls = 0
        self.fundamental_calls = 0

    def company_profile(self, ticker: str) -> _Profile:
        self.profile_calls += 1
        assert ticker == "REGN"
        return _Profile(market_cap_m=100_000.0, avg_volume=1_234_567, beta=0.82)

    def momentum_fundamentals(self, ticker: str) -> _Fundamentals:
        self.fundamental_calls += 1
        assert ticker == "REGN"
        return _Fundamentals(earnings_surprises_pct=[2.1, 1.8, -0.3, 3.5, 9.9])

    def spy_premarket_change_pct(self) -> float:
        return 0.42


class _Macro:
    def current_vix(self) -> float:
        return 18.5


def test_context_prompt_block_formats_known_inputs() -> None:
    context = EnrichmentContext(
        market_cap_m=100_000,
        avg_volume=1_234_567,
        beta=0.82,
        sector="Health Care",
        atr_pct=2.4,
        eps_beat_pct=6.52,
        revenue_beat_pct=3.5,
        guidance_direction="up",
        last_4_surprises=[2.1, 1.8, -0.3, 3.5],
        headline_cluster_count=2,
        minutes_to_open=15,
        spy_change_pct=0.42,
        vix=18.5,
        sector_etf_5d_pct=1.25,
    )

    block = context.to_prompt_block()

    assert "Market cap: $100,000M" in block
    assert "Beta: 0.8" in block
    assert "EPS beat/miss: +6.52%" in block
    assert "Last 4 earnings surprises: +2.1%, +1.8%, -0.3%, +3.5%" in block
    assert "Prior same-symbol headlines in last 30m: 2" in block


def test_context_assembler_caches_symbol_data(tmp_path: Path) -> None:
    universe = tmp_path / "universe.csv"
    universe.write_text("symbol,name,sector\nREGN,Regeneron,Health Care\n")
    bars = tmp_path / "bars" / "REGN"
    bars.mkdir(parents=True)
    _write_bars(bars / "2024.parquet")
    market = _MarketData()
    assembler = ContextAssembler(
        universe_csv_path=universe,
        bar_root=tmp_path / "bars",
        market_data_provider=market,
        macro_provider=_Macro(),
        sector_etf_5d_pct_by_etf={"XLV": 1.25},
    )
    assembler.cache_run_context()
    ts = datetime(2024, 12, 19, 14, 32, tzinfo=UTC)

    first = assembler.build_context(
        "REGN",
        "REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate, Sales $3.605B Beat $3.483B Estimate",
        ts,
        "earnings",
        "report",
    )
    second = assembler.build_context("REGN", "REGN EPS $1.10 Beats $1.00 Estimate", ts, "earnings", "report")

    assert first.market_cap_m == 100_000.0
    assert first.avg_volume == 1_234_567
    assert first.beta == pytest.approx(0.82)
    assert first.sector == "Health Care"
    assert first.atr_pct is not None
    assert first.eps_beat_pct == pytest.approx(6.524, abs=0.01)
    assert first.revenue_beat_pct == pytest.approx(3.503, abs=0.01)
    assert first.last_4_surprises == [2.1, 1.8, -0.3, 3.5]
    assert first.spy_change_pct == pytest.approx(0.42)
    assert first.vix == pytest.approx(18.5)
    assert first.sector_etf_5d_pct == pytest.approx(1.25)
    assert second.eps_beat_pct == pytest.approx(10.0)
    assert market.profile_calls == 1
    assert market.fundamental_calls == 1


def test_missing_data_gracefully_returns_none_fields(tmp_path: Path) -> None:
    assembler = ContextAssembler(
        universe_csv_path=tmp_path / "missing.csv",
        bar_root=tmp_path / "missing-bars",
        market_data_provider=None,
        macro_provider=None,
        sector_etf_5d_pct_by_etf={},
    )

    context = assembler.build_context(
        "XXXX",
        "XXXX Announces Quarterly Dividend",
        datetime(2024, 6, 1, 14, 0, tzinfo=UTC),
        "other",
        "generic",
    )

    assert context.market_cap_m is None
    assert context.avg_volume is None
    assert context.beta is None
    assert context.sector is None
    assert context.atr_pct is None
    assert context.eps_beat_pct is None


def test_yfinance_profile_helper_reads_beta(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ticker:
        def __init__(self, symbol: str) -> None:
            assert symbol == "REGN"
            self.info = {
                "marketCap": 100_000_000_000,
                "averageVolume": 1_234_567,
                "beta": 0.82,
            }

    fake_yfinance = types.SimpleNamespace(Ticker=_Ticker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)

    market_cap_m, avg_volume, beta = _fetch_yfinance_profile("REGN")

    assert market_cap_m == pytest.approx(100_000.0)
    assert avg_volume == 1_234_567
    assert beta == pytest.approx(0.82)


def test_yfinance_atr_helper_computes_daily_atr_pct(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ticker:
        def __init__(self, symbol: str) -> None:
            assert symbol == "REGN"

        def history(self, period: str) -> pd.DataFrame:
            assert period == "1mo"
            closes = [100.0 + i for i in range(16)]
            return pd.DataFrame(
                {
                    "High": [close + 1.0 for close in closes],
                    "Low": [close - 1.0 for close in closes],
                    "Close": closes,
                }
            )

    fake_yfinance = types.SimpleNamespace(Ticker=_Ticker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)

    atr_pct = _fetch_yfinance_atr_pct("REGN")

    assert atr_pct == pytest.approx((2.0 / 115.0) * 100.0)


def test_headline_cluster_count_uses_prior_30_minutes(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE catalyst_events (symbol TEXT, event_ts TEXT, headline TEXT)"
    )
    event_ts = datetime(2024, 6, 1, 14, 0, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO catalyst_events VALUES (?, ?, ?)",
        [
            ("REGN", (event_ts - timedelta(minutes=29)).isoformat(), "prior 1"),
            ("REGN", (event_ts - timedelta(minutes=5)).isoformat(), "prior 2"),
            ("REGN", event_ts.isoformat(), "current"),
            ("REGN", (event_ts - timedelta(minutes=31)).isoformat(), "old"),
            ("AAPL", (event_ts - timedelta(minutes=5)).isoformat(), "other symbol"),
        ],
    )
    conn.commit()
    conn.close()

    assembler = ContextAssembler(db_path=str(db), sector_etf_5d_pct_by_etf={})
    context = assembler.build_context("REGN", "REGN EPS $1.10 Beats $1.00 Estimate", event_ts, "earnings", "report")

    assert context.headline_cluster_count == 2


def test_minutes_to_open_uses_project_clock() -> None:
    assembler = ContextAssembler(sector_etf_5d_pct_by_etf={}, clock=DriftPilotClock("America/New_York"))

    premarket = assembler.build_context(
        "REGN",
        "REGN EPS $1.10 Beats $1.00 Estimate",
        datetime(2024, 6, 3, 13, 0, tzinfo=UTC),
        "earnings",
        "report",
    )
    during_market = assembler.build_context(
        "REGN",
        "REGN EPS $1.10 Beats $1.00 Estimate",
        datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        "earnings",
        "report",
    )

    assert premarket.minutes_to_open == 30
    assert during_market.minutes_to_open is None


def _write_bars(path: Path) -> None:
    rows = []
    start = datetime(2024, 12, 1, 14, 30, tzinfo=UTC)
    close = 100.0
    for i in range(25):
        close += 1.0
        rows.append(
            {
                "timestamp": start + timedelta(minutes=i),
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)
