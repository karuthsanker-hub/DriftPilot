from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd  # type: ignore[import-untyped]

from driftpilot.clock import require_aware
from driftpilot.execution.paper_fills import entry_fill, exit_fill
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals.features import MinuteBar, Quote
from driftpilot.signals.intraday_momentum import scan_intraday_momentum


REQUIRED_BAR_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    entry_at: datetime
    exit_at: datetime
    quantity: int
    entry_reference_price: float
    entry_price: float
    exit_reference_price: float
    exit_price: float
    gross_pnl: float
    net_pnl: float
    slippage_cost: float
    return_pct: float
    hold_minutes: int
    exit_reason: str
    regime: str


@dataclass(frozen=True, slots=True)
class ReplayResult:
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, float]]
    starting_capital: float
    ending_capital: float
    caveats: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _OpenPosition:
    symbol: str
    entry_at: datetime
    quantity: int
    entry_reference_price: float
    entry_price: float
    target_price: float
    stop_price: float
    regime: str


def load_parquet_bars(
    root: str | Path,
    *,
    start: date,
    end: date,
) -> pd.DataFrame:
    root_path = Path(root)
    files = sorted(root_path.glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet bar files found under {root_path}")

    frames = [pd.read_parquet(file) for file in files]
    bars = pd.concat(frames, ignore_index=True)
    missing = REQUIRED_BAR_COLUMNS.difference(bars.columns)
    if missing:
        raise ValueError(f"bar cache missing required columns: {sorted(missing)}")

    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    dates = bars["timestamp"].dt.date
    bars = bars[(dates >= start) & (dates <= end)].copy()
    bars["symbol"] = bars["symbol"].astype(str).str.upper()
    return bars.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def replay_bars(
    bars: pd.DataFrame,
    *,
    settings: DriftPilotSettings | None = None,
    rvol_lookback: int = 20,
    point_in_time_constituents: bool = False,
) -> ReplayResult:
    settings = settings or DriftPilotSettings()
    if bars.empty:
        return ReplayResult(
            trades=[],
            equity_curve=[],
            starting_capital=settings.paper_capital,
            ending_capital=settings.paper_capital,
            caveats=["No bars were available for the requested period."],
        )

    normalized = _normalize_bars(bars)
    has_quote_columns = {"bid", "ask"}.issubset(normalized.columns)
    caveats: list[str] = []
    if not has_quote_columns:
        caveats.append(
            "Bid/ask quotes were unavailable in the bar cache; replay modeled spread from close price."
        )
    if not point_in_time_constituents:
        caveats.append(
            "Point-in-time index constituents were unavailable; report may include survivorship bias."
        )

    history: dict[str, list[MinuteBar]] = {}
    quotes: dict[str, Quote] = {}
    positions: list[_OpenPosition] = []
    trades: list[BacktestTrade] = []
    equity = settings.paper_capital
    equity_curve: list[tuple[datetime, float]] = []

    for timestamp, rows in normalized.groupby("timestamp", sort=True):
        current_time = _as_aware_datetime(timestamp)
        latest_by_symbol: dict[str, MinuteBar] = {}
        for row in rows.to_dict("records"):
            bar = _row_to_bar(row)
            history.setdefault(bar.symbol, []).append(bar)
            latest_by_symbol[bar.symbol] = bar
            quotes[bar.symbol] = _row_to_quote(row, bar, modeled=not has_quote_columns)

        exits = _evaluate_exits(
            positions,
            latest_by_symbol,
            current_time=current_time,
            settings=settings,
        )
        for position, exit_bar, exit_reason in exits:
            positions.remove(position)
            trade = _close_position(position, exit_bar, exit_reason)
            trades.append(trade)
            equity += trade.net_pnl

        free_slots = settings.trade_slots - len(positions)
        if free_slots > 0 and "SPY" in history:
            universe_history = {
                symbol: symbol_bars
                for symbol, symbol_bars in history.items()
                if symbol != "SPY" and symbol in latest_by_symbol
            }
            try:
                regime, queue = scan_intraday_momentum(
                    universe_history,
                    quotes,
                    history["SPY"],
                    rvol_lookback=rvol_lookback,
                )
            except ValueError:
                queue = []
                regime = None
            open_symbols = {position.symbol for position in positions}
            for decision in queue:
                if free_slots <= 0:
                    break
                if decision.symbol in open_symbols:
                    continue
                entry_bar = latest_by_symbol.get(decision.symbol)
                if entry_bar is None:
                    continue
                candidate_position = _open_position(
                    decision.symbol,
                    entry_bar,
                    current_time=current_time,
                    settings=settings,
                    regime=regime.regime.value if regime is not None else "UNKNOWN",
                )
                if candidate_position is None:
                    continue
                positions.append(candidate_position)
                open_symbols.add(candidate_position.symbol)
                free_slots -= 1

        equity_curve.append((current_time, equity + _unrealized_pnl(positions, latest_by_symbol)))

    for position in list(positions):
        last_bar = history[position.symbol][-1]
        trade = _close_position(position, last_bar, "end_of_replay")
        trades.append(trade)
        equity += trade.net_pnl

    return ReplayResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_capital=settings.paper_capital,
        ending_capital=equity,
        caveats=caveats,
    )


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_BAR_COLUMNS.difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    normalized = bars.copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True)
    normalized["symbol"] = normalized["symbol"].astype(str).str.upper()
    return normalized.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def _as_aware_datetime(value: object) -> datetime:
    if isinstance(value, pd.Timestamp):
        return require_aware(value.to_pydatetime())
    if isinstance(value, datetime):
        return require_aware(value)
    raise TypeError(f"unsupported timestamp type: {type(value)!r}")


def _row_to_bar(row: dict[str, object]) -> MinuteBar:
    return MinuteBar(
        symbol=str(row["symbol"]),
        timestamp=_as_aware_datetime(row["timestamp"]),
        open=_as_float(row["open"]),
        high=_as_float(row["high"]),
        low=_as_float(row["low"]),
        close=_as_float(row["close"]),
        volume=_as_float(row["volume"]),
    )


def _row_to_quote(row: dict[str, object], bar: MinuteBar, *, modeled: bool) -> Quote:
    if not modeled:
        return Quote(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            bid=_as_float(row["bid"]),
            ask=_as_float(row["ask"]),
        )
    spread = min(max(0.01, 0.0002 * bar.close), max(0.02, 0.001 * bar.close))
    half_spread = spread / 2
    return Quote(
        symbol=bar.symbol,
        timestamp=bar.timestamp,
        bid=bar.close - half_spread,
        ask=bar.close + half_spread,
    )


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"expected numeric value, got {type(value)!r}")


def _open_position(
    symbol: str,
    bar: MinuteBar,
    *,
    current_time: datetime,
    settings: DriftPilotSettings,
    regime: str,
) -> _OpenPosition | None:
    fill = entry_fill(
        symbol=symbol,
        quantity=1,
        reference_price=bar.close,
        filled_at=current_time,
        metadata={"source": "backtest"},
    )
    quantity = int(settings.slot_value // fill.price)
    if quantity <= 0:
        return None
    total_slippage = fill.slippage * quantity
    entry_price = bar.close + (total_slippage / quantity)
    return _OpenPosition(
        symbol=symbol,
        entry_at=current_time,
        quantity=quantity,
        entry_reference_price=bar.close,
        entry_price=entry_price,
        target_price=entry_price * (1 + settings.target_pct),
        stop_price=entry_price * (1 - settings.stop_pct),
        regime=regime,
    )


def _evaluate_exits(
    positions: Iterable[_OpenPosition],
    latest_by_symbol: dict[str, MinuteBar],
    *,
    current_time: datetime,
    settings: DriftPilotSettings,
) -> list[tuple[_OpenPosition, MinuteBar, str]]:
    exits: list[tuple[_OpenPosition, MinuteBar, str]] = []
    max_hold = timedelta(minutes=settings.max_hold_minutes)
    for position in positions:
        latest = latest_by_symbol.get(position.symbol)
        if latest is None:
            continue
        if latest.close >= position.target_price:
            exits.append((position, latest, "TARGET"))
        elif latest.close <= position.stop_price:
            exits.append((position, latest, "STOP"))
        elif current_time - position.entry_at >= max_hold:
            exits.append((position, latest, "TIME"))
    return exits


def _close_position(
    position: _OpenPosition,
    exit_bar: MinuteBar,
    exit_reason: str,
) -> BacktestTrade:
    fill = exit_fill(
        symbol=position.symbol,
        quantity=position.quantity,
        reference_price=exit_bar.close,
        filled_at=exit_bar.timestamp,
        metadata={"source": "backtest", "exit_reason": exit_reason},
    )
    gross_pnl = (exit_bar.close - position.entry_reference_price) * position.quantity
    net_pnl = (fill.price - position.entry_price) * position.quantity
    slippage_cost = gross_pnl - net_pnl
    hold_minutes = int((exit_bar.timestamp - position.entry_at).total_seconds() // 60)
    return BacktestTrade(
        symbol=position.symbol,
        entry_at=position.entry_at,
        exit_at=exit_bar.timestamp,
        quantity=position.quantity,
        entry_reference_price=position.entry_reference_price,
        entry_price=position.entry_price,
        exit_reference_price=exit_bar.close,
        exit_price=fill.price,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        slippage_cost=slippage_cost,
        return_pct=net_pnl / (position.entry_price * position.quantity),
        hold_minutes=hold_minutes,
        exit_reason=exit_reason,
        regime=position.regime,
    )


def _unrealized_pnl(
    positions: Iterable[_OpenPosition],
    latest_by_symbol: dict[str, MinuteBar],
) -> float:
    total = 0.0
    for position in positions:
        latest = latest_by_symbol.get(position.symbol)
        if latest is None:
            continue
        total += (latest.close - position.entry_price) * position.quantity
    return total
