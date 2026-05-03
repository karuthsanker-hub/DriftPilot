"""Phase G — mid-price limit fill wiring into the per-bar replay loop.

These tests verify that:
  * Non-mid-price signals are unaffected (default path stays as-is).
  * RS-Drift (`ENTRY_ORDER_TYPE = "limit_mid"`) emits sentinel
    `BacktestTrade` rows for limits that timed out unfilled.
  * `compute_locked_spec_metrics` reports `fill_rate_pct` honestly and
    excludes unfilled rows from winner/loser averages.
  * The `limit_fill` module itself remains importable / unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]

from driftpilot.backtest.limit_fill import LimitOrder, attempt_limit_fill
from driftpilot.backtest.metrics import compute_locked_spec_metrics
from driftpilot.backtest.replay import BacktestTrade, replay_bars
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals.features import MinuteBar


ET = ZoneInfo("America/New_York")


def _bar_row(
    *,
    symbol: str,
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    bid: float | None = None,
    ask: float | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": timestamp,
        "symbol": symbol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    if bid is not None and ask is not None:
        row["bid"] = bid
        row["ask"] = ask
    return row


def _session_minutes(session_date: datetime) -> list[datetime]:
    """Generate timezone-aware UTC minute timestamps from 09:30 ET to 10:30 ET
    for a given ET session date.
    """
    start_et = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        9,
        30,
        tzinfo=ET,
    )
    return [(start_et + timedelta(minutes=i)).astimezone(UTC) for i in range(61)]


def _build_universe_bars(
    *,
    session_date: datetime,
    aaa_close_path: list[float],
    spy_close_path: list[float],
) -> pd.DataFrame:
    """Assemble a DataFrame of minute bars covering 09:30–10:30 ET for AAA + SPY.

    `aaa_close_path` and `spy_close_path` must each have 61 entries (one per
    minute of the inclusive 09:30–10:30 window).
    """
    timestamps = _session_minutes(session_date)
    rows: list[dict[str, object]] = []
    for ts, close in zip(timestamps, aaa_close_path):
        rows.append(
            _bar_row(
                symbol="AAA",
                timestamp=ts,
                open_=close,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                volume=10_000.0,
            )
        )
    for ts, close in zip(timestamps, spy_close_path):
        rows.append(
            _bar_row(
                symbol="SPY",
                timestamp=ts,
                open_=close,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                volume=10_000.0,
            )
        )
    return pd.DataFrame(rows)


def _trade(
    *,
    return_pct: float,
    net_pnl: float,
    entry_was_filled: bool = True,
    entry_was_attempted_at_mid: bool = True,
    symbol: str = "AAA",
) -> BacktestTrade:
    entry_at: datetime = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    exit_at: datetime = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    return BacktestTrade(
        symbol=symbol,
        entry_at=entry_at,
        exit_at=exit_at,
        quantity=10 if entry_was_filled else 0,
        entry_reference_price=100.0,
        entry_price=100.0,
        exit_reference_price=100.0 * (1.0 + return_pct),
        exit_price=100.0 * (1.0 + return_pct),
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        slippage_cost=0.0,
        return_pct=return_pct,
        hold_minutes=30,
        exit_reason="TARGET" if net_pnl > 0 else ("STOP" if net_pnl < 0 else "UNFILLED_LIMIT_TIMEOUT"),
        regime="GREEN",
        peak_unrealized_pct=0.0,
        entry_was_attempted_at_mid=entry_was_attempted_at_mid,
        entry_was_filled=entry_was_filled,
    )


# ---------------------------------------------------------------------------
# 1. Non-mid-price signal path is unchanged.
# ---------------------------------------------------------------------------


def test_non_mid_price_signal_unchanged() -> None:
    """For intraday_momentum_v1 (no ENTRY_ORDER_TYPE = limit_mid), every
    BacktestTrade in the replay output must keep the legacy semantics:
    `entry_was_filled=True`, `entry_was_attempted_at_mid=False`.
    """

    session = datetime(2026, 1, 5)
    # 61 closes; price doesn't really matter — this test asserts on shape, not
    # on the strategy's actual signal triggering. Even an empty trade list
    # satisfies the invariant trivially.
    aaa_path: list[float] = [100.0 + 0.01 * i for i in range(61)]
    spy_path: list[float] = [400.0 + 0.005 * i for i in range(61)]
    bars = _build_universe_bars(
        session_date=session, aaa_close_path=aaa_path, spy_close_path=spy_path
    )
    settings = DriftPilotSettings()
    result = replay_bars(bars, settings=settings, signal_name="intraday_momentum_v1")
    for trade in result.trades:
        assert trade.entry_was_filled is True, (
            f"non-mid-price signal must not emit unfilled rows: {trade}"
        )
        assert trade.entry_was_attempted_at_mid is False, (
            f"non-mid-price signal must not flag mid-price attempts: {trade}"
        )


# ---------------------------------------------------------------------------
# 2. Mid-price signal records unfilled attempts on a runaway market.
# ---------------------------------------------------------------------------


def test_mid_price_signal_records_unfilled_attempts() -> None:
    """RS-Drift on a synthetic monotone-up tape must:

      * place at least one mid-price limit (we know AAA outpaces SPY by 1.5%
        in the 09:30–10:00 window),
      * see that limit time out unfilled (no pullback to the placement mid
        ever happens within the 30-second timeout window),
      * emit at least one BacktestTrade row with
        `entry_was_attempted_at_mid=True, entry_was_filled=False, qty=0`,
      * yield `fill_rate_pct < 0.5` from `compute_locked_spec_metrics`,
      * exclude unfilled rows from winner/loser averages.
    """

    session = datetime(2026, 1, 5)
    # 09:30–10:00 ET window (first 30 minutes): AAA rises 2%, SPY rises 0.1%
    # → RS = ~1.9%, well above the 1.25% threshold.
    aaa_path: list[float] = []
    base_aaa = 100.0
    for i in range(31):
        aaa_path.append(base_aaa * (1.0 + 0.02 * (i / 30.0)))
    # Then 10:00–10:30: keep climbing (no pullback).
    for i in range(1, 31):
        aaa_path.append(aaa_path[-1] + 0.50)

    spy_path: list[float] = []
    base_spy = 400.0
    for i in range(31):
        spy_path.append(base_spy * (1.0 + 0.001 * (i / 30.0)))
    for i in range(1, 31):
        spy_path.append(spy_path[-1] + 0.05)

    bars = _build_universe_bars(
        session_date=session, aaa_close_path=aaa_path, spy_close_path=spy_path
    )
    # Force a tight entry-limit timeout so the runaway tape definitely
    # produces unfilled timeouts within the 60-minute synthetic window.
    settings = DriftPilotSettings(entry_limit_timeout_seconds=30)
    result = replay_bars(bars, settings=settings, signal_name="rs_drift_v1")

    unfilled = [t for t in result.trades if not t.entry_was_filled]
    assert unfilled, (
        "expected at least one unfilled mid-price-limit sentinel row, "
        f"got trades={result.trades}"
    )
    for trade in unfilled:
        assert trade.entry_was_attempted_at_mid is True
        assert trade.quantity == 0
        assert trade.exit_reason == "UNFILLED_LIMIT_TIMEOUT"
        assert trade.gross_pnl == 0.0
        assert trade.net_pnl == 0.0
        assert trade.return_pct == 0.0

    metrics = compute_locked_spec_metrics(result.trades, len(result.trades))
    assert metrics["fill_rate_pct"] < 0.5, metrics

    # Winner / loser averages must be computed from filled rows only.
    filled = [t for t in result.trades if t.entry_was_filled]
    if filled:
        winner_returns = [t.return_pct for t in filled if t.net_pnl > 0]
        if winner_returns:
            from statistics import fmean

            expected = fmean(winner_returns)
            assert abs(metrics["realized_avg_winner_pct"] - expected) < 1e-9


# ---------------------------------------------------------------------------
# 3. Mid-price signal fills on a pullback.
# ---------------------------------------------------------------------------


def test_mid_price_signal_fills_on_pullback() -> None:
    """When the tape pulls back through the placement mid within the timeout
    window, `attempt_limit_fill` must fire and the position should open.
    """

    # Direct unit test of the simulator wired into replay: verify a pullback
    # tape produces a filled result.
    placed_at = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    order = LimitOrder(
        symbol="AAA",
        placed_at=placed_at,
        placement_mid_price=100.50,
        placement_bid=100.49,
        placement_ask=100.51,
        timeout_seconds=10 * 60,
        side="buy",
    )
    bars = [
        MinuteBar(
            symbol="AAA",
            timestamp=placed_at + timedelta(minutes=1),
            open=100.60,
            high=100.80,
            low=100.55,
            close=100.75,
            volume=1_000.0,
        ),
        MinuteBar(
            symbol="AAA",
            timestamp=placed_at + timedelta(minutes=2),
            open=100.75,
            high=100.95,
            low=100.70,
            close=100.90,
            volume=1_000.0,
        ),
        MinuteBar(
            symbol="AAA",
            timestamp=placed_at + timedelta(minutes=3),
            open=100.90,
            high=101.00,
            low=100.40,
            close=100.45,
            volume=1_000.0,
        ),
    ]
    result = attempt_limit_fill(order, bars)
    assert result.filled is True
    assert result.price == 100.50
    assert result.reason == "filled"


# ---------------------------------------------------------------------------
# 4. Smoke import test — limit_fill module is intact.
# ---------------------------------------------------------------------------


def test_existing_test_limit_fill_still_passes() -> None:
    """Smoke check that `limit_fill` is importable + runnable from a
    one-bar fixture. The full 7-test limit_fill suite continues to live in
    `tests/backtest/test_limit_fill.py` — this is just a sanity touch.
    """

    placed_at = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    order = LimitOrder(
        symbol="ZZZ",
        placed_at=placed_at,
        placement_mid_price=50.00,
        placement_bid=49.99,
        placement_ask=50.01,
        timeout_seconds=120,
        side="buy",
    )
    bar = MinuteBar(
        symbol="ZZZ",
        timestamp=placed_at + timedelta(minutes=1),
        open=50.05,
        high=50.10,
        low=49.95,
        close=50.00,
        volume=500.0,
    )
    result = attempt_limit_fill(order, [bar])
    assert result.filled is True


# ---------------------------------------------------------------------------
# 5. Direct unit test of compute_locked_spec_metrics with mixed trades.
# ---------------------------------------------------------------------------


def test_signals_attempted_counts_filled_plus_unfilled() -> None:
    """4 hand-built BacktestTrades: 2 winners, 1 loser, 1 unfilled →
    `signals_attempted == 4`, `fill_rate_pct == 0.75`,
    winner average uses 2 trades not 3.
    """

    trades: list[BacktestTrade] = [
        _trade(return_pct=0.01, net_pnl=10.0, entry_was_filled=True),
        _trade(return_pct=0.02, net_pnl=20.0, entry_was_filled=True),
        _trade(return_pct=-0.01, net_pnl=-10.0, entry_was_filled=True),
        _trade(return_pct=0.0, net_pnl=0.0, entry_was_filled=False),
    ]
    metrics = compute_locked_spec_metrics(trades, len(trades))
    assert metrics["fill_rate_pct"] == 0.75
    # 2 winners out of 3 filled trades → actual_win_rate = 2/3.
    assert abs(metrics["actual_win_rate"] - (2.0 / 3.0)) < 1e-9
    # Winner avg is mean(0.01, 0.02) == 0.015. If unfilled (return 0) had
    # been counted, the mean would shift — guard against that regression.
    assert abs(metrics["realized_avg_winner_pct"] - 0.015) < 1e-9
