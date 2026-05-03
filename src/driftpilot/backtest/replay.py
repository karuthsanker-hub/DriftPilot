from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from driftpilot.clock import require_aware
from driftpilot.execution.paper_fills import entry_fill, exit_fill
from driftpilot.settings import DriftPilotSettings
from driftpilot.backtest.constants import (
    AVAILABLE_DATA_DEPENDENCIES,
    MAX_HISTORY_MINUTES,
)
from driftpilot.signals.base import (
    InsufficientDataError,
    signal_data_dependencies,
    signal_required_history_minutes,
)
from driftpilot.signals.features import MinuteBar, Quote
from driftpilot.signals import DEFAULT_SIGNAL, get_signal
from driftpilot.signals.regime import CAUTION_5M_RETURN_FLOOR, GREEN_5M_RETURN_FLOOR, VWAP_ATR_BREAK_MULTIPLE


def _validate_signal_compatibility(signal: object) -> None:
    """Per refactor plan v1.1 § 3 Task 2.1: validate at backtest startup that
    the signal's declared history requirement and data dependencies are
    satisfiable by the harness. Cheap fail-fast — runs once.
    """

    required = signal_required_history_minutes(signal)
    if required > MAX_HISTORY_MINUTES:
        raise ValueError(
            f"Signal {getattr(signal, 'name', '<unknown>')} requires "
            f"{required} mins of history; harness max is {MAX_HISTORY_MINUTES}"
        )
    for dep in signal_data_dependencies(signal):
        if dep not in AVAILABLE_DATA_DEPENDENCIES:
            raise ValueError(
                f"Signal {getattr(signal, 'name', '<unknown>')} requires "
                f"data dependency {dep!r}, which the harness cannot provide. "
                f"Available: {sorted(AVAILABLE_DATA_DEPENDENCIES)}"
            )


REQUIRED_BAR_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
LARGE_REPLAY_ROW_THRESHOLD = 1_000_000


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
    # Locked Integration Refactor v1.1 (Phase 4): peak unrealized return %
    # observed while the position was open. Sourced from
    # `position.metadata["peak_unrealized_pct"]` written by signal
    # `evaluate_exit` implementations (Apex Hunter give-back metric). Default
    # 0.0 so every existing caller / test that constructs `BacktestTrade`
    # without this field keeps working.
    peak_unrealized_pct: float = 0.0


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
    metadata: dict[str, Any] = field(default_factory=dict)


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

    bars = _read_bar_dataset(root_path, files)
    missing = REQUIRED_BAR_COLUMNS.difference(bars.columns)
    if missing:
        raise ValueError(f"bar cache missing required columns: {sorted(missing)}")

    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    dates = bars["timestamp"].dt.date
    bars = bars[(dates >= start) & (dates <= end)].copy()
    bars["symbol"] = bars["symbol"].astype(str).str.upper()
    return bars.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def replay_parquet_cache(
    root: str | Path,
    *,
    start: date,
    end: date,
    settings: DriftPilotSettings | None = None,
    rvol_lookback: int = 20,
    point_in_time_constituents: bool = False,
    signal_name: str | None = None,
) -> ReplayResult:
    settings = settings or DriftPilotSettings()
    signal = get_signal(signal_name or settings.active_signal)
    if signal.name != DEFAULT_SIGNAL:
        # Non-intraday signals route through the generic per-symbol streaming
        # path. The vectorized path below is a special-case optimization for
        # intraday_momentum_v1 only.
        return replay_parquet_cache_generic(
            root,
            start=start,
            end=end,
            settings=settings,
            rvol_lookback=rvol_lookback,
            point_in_time_constituents=point_in_time_constituents,
            signal_name=signal.name,
        )
    root_path = Path(root)
    files = sorted(root_path.glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet bar files found under {root_path}")

    spy_file = root_path / "SPY" / f"{start.year}.parquet"
    if not spy_file.exists():
        raise FileNotFoundError(f"SPY bar file is required for regime replay: {spy_file}")

    spy_bars = _read_symbol_cache(spy_file, start=start, end=end)
    regime_by_timestamp = _vectorized_spy_regime(spy_bars)
    candidate_frames: list[pd.DataFrame] = []
    for file in files:
        if file.parent.name.upper() == "SPY":
            continue
        frame = _read_symbol_cache(file, start=start, end=end)
        if frame.empty:
            continue
        candidates = _vectorized_symbol_candidates(
            frame,
            regime_by_timestamp,
            rvol_lookback=rvol_lookback,
        )
        if not candidates.empty:
            candidate_frames.append(candidates)

    caveats = []
    if not point_in_time_constituents:
        caveats.append(
            "Point-in-time index constituents were unavailable; report may include survivorship bias."
        )
    caveats.append("Bid/ask quotes were unavailable in the bar cache; replay modeled spread from close price.")
    if not candidate_frames:
        return ReplayResult(
            trades=[],
            equity_curve=[],
            starting_capital=settings.paper_capital,
            ending_capital=settings.paper_capital,
            caveats=[*caveats, "No candidates passed the intraday momentum filters."],
        )

    candidates = pd.concat(candidate_frames, ignore_index=True)
    candidates = _score_candidate_frame(candidates)
    candidates = candidates.sort_values(["timestamp", "score", "symbol"], ascending=[True, False, True])
    return _replay_candidate_events(
        candidates,
        root_path=root_path,
        start=start,
        end=end,
        settings=settings,
        caveats=caveats,
    )


def _read_bar_dataset(root_path: Path, files: list[Path]) -> pd.DataFrame:
    try:
        import pyarrow.dataset as ds  # type: ignore[import-not-found]
    except ImportError:
        frames = [pd.read_parquet(file, columns=list(REQUIRED_BAR_COLUMNS)) for file in files]
        return pd.concat(frames, ignore_index=True)

    dataset = ds.dataset(str(root_path), format="parquet")
    table = dataset.to_table(columns=list(REQUIRED_BAR_COLUMNS))
    return table.to_pandas()


def _read_symbol_cache(path: Path, *, start: date, end: date) -> pd.DataFrame:
    frame = pd.read_parquet(path, columns=list(REQUIRED_BAR_COLUMNS))
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    dates = frame["timestamp"].dt.date
    frame = frame[(dates >= start) & (dates <= end)].copy()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column])
    return frame.sort_values("timestamp").reset_index(drop=True)


def _vectorized_spy_regime(spy_bars: pd.DataFrame) -> pd.DataFrame:
    frame = spy_bars.copy()
    if frame.empty:
        raise ValueError("SPY bars are required for regime replay")
    session = frame["timestamp"].dt.date
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    frame["_tpv"] = typical * frame["volume"]
    frame["_session"] = session
    frame["session_vwap"] = frame.groupby("_session")["_tpv"].cumsum() / frame.groupby("_session")[
        "volume"
    ].cumsum()
    frame["return_5m"] = frame.groupby("_session")["close"].pct_change(5).fillna(0.0)
    frame["benchmark_return_15m"] = frame.groupby("_session")["close"].pct_change(15).fillna(0.0)
    previous_close = frame.groupby("_session")["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = true_range.groupby(frame["_session"]).rolling(14, min_periods=1).mean().reset_index(level=0, drop=True)
    atr_distance = (frame["session_vwap"] - frame["close"]) / frame["atr"].replace(0.0, np.nan)
    broken_below_atr = (frame["close"] < frame["session_vwap"]) & (atr_distance > VWAP_ATR_BREAK_MULTIPLE)
    frame["regime"] = np.select(
        [
            frame["return_5m"] < CAUTION_5M_RETURN_FLOOR,
            broken_below_atr,
            (frame["close"] > frame["session_vwap"]) & (frame["return_5m"] > GREEN_5M_RETURN_FLOOR),
            (frame["close"] < frame["session_vwap"]) & (frame["return_5m"] > CAUTION_5M_RETURN_FLOOR),
        ],
        ["RED", "RED", "GREEN", "CAUTION"],
        default="RED",
    )
    return frame.loc[:, ["timestamp", "regime", "benchmark_return_15m"]]


def _vectorized_symbol_candidates(
    frame: pd.DataFrame,
    regime_by_timestamp: pd.DataFrame,
    *,
    rvol_lookback: int,
) -> pd.DataFrame:
    symbol = str(frame["symbol"].iloc[0]).upper()
    working = frame.copy()
    working["_session"] = working["timestamp"].dt.date
    working["_minute"] = working["timestamp"].dt.strftime("%H:%M")
    typical = (working["high"] + working["low"] + working["close"]) / 3.0
    working["_tpv"] = typical * working["volume"]
    working["session_vwap"] = working.groupby("_session")["_tpv"].cumsum() / working.groupby("_session")[
        "volume"
    ].cumsum()
    working["return_15m"] = working.groupby("_session")["close"].pct_change(15)
    prior_same_minute_volume = (
        working.groupby("_minute")["volume"]
        .transform(lambda series: series.shift(1).rolling(rvol_lookback, min_periods=rvol_lookback).mean())
    )
    working["rvol"] = working["volume"] / prior_same_minute_volume
    working["distance_above_vwap_pct"] = working["close"] / working["session_vwap"] - 1.0
    base = working[
        (working["rvol"] >= 2.0)
        & (working["close"] > working["session_vwap"])
        & (working["return_15m"] >= 0.005)
    ].copy()
    if base.empty:
        return pd.DataFrame()
    base = base.merge(regime_by_timestamp, on="timestamp", how="inner")
    if base.empty:
        return pd.DataFrame()
    relative_strength = base["return_15m"] - base["benchmark_return_15m"]
    allowed = (
        (base["regime"] == "GREEN")
        | ((base["regime"] == "CAUTION") & (relative_strength > 0.005))
        | ((base["regime"] == "RED") & (relative_strength > 0.010) & (base["return_15m"] > 0))
    )
    base = base[allowed].copy()
    if base.empty:
        return pd.DataFrame()
    base["symbol"] = symbol
    base["relative_strength"] = relative_strength.loc[base.index]
    return base.loc[
        :,
        [
            "timestamp",
            "symbol",
            "close",
            "rvol",
            "return_15m",
            "distance_above_vwap_pct",
            "relative_strength",
            "regime",
        ],
    ]


def _score_candidate_frame(candidates: pd.DataFrame) -> pd.DataFrame:
    scored = candidates.copy()
    for source, target in (
        ("rvol", "rvol_zscore"),
        ("return_15m", "return_15m_zscore"),
        ("distance_above_vwap_pct", "distance_above_vwap_zscore"),
    ):
        mean = scored.groupby("timestamp")[source].transform("mean")
        std = scored.groupby("timestamp")[source].transform("std").replace(0.0, np.nan)
        scored[target] = ((scored[source] - mean) / std).fillna(0.0)
    scored["score"] = (
        0.4 * scored["rvol_zscore"]
        + 0.3 * scored["return_15m_zscore"]
        + 0.3 * scored["distance_above_vwap_zscore"]
    )
    return scored


def _replay_candidate_events(
    candidates: pd.DataFrame,
    *,
    root_path: Path,
    start: date,
    end: date,
    settings: DriftPilotSettings,
    caveats: list[str],
) -> ReplayResult:
    price_cache: dict[str, pd.DataFrame] = {}
    positions: list[tuple[_OpenPosition, BacktestTrade]] = []
    trades: list[BacktestTrade] = []
    equity = settings.paper_capital
    equity_curve: list[tuple[datetime, float]] = []

    for timestamp, rows in candidates.groupby("timestamp", sort=True):
        current_time = _as_aware_datetime(timestamp)
        for position, planned_trade in list(positions):
            if planned_trade.exit_at <= current_time:
                positions.remove((position, planned_trade))
                trades.append(planned_trade)
                equity += planned_trade.net_pnl

        open_symbols = {position.symbol for position, _ in positions}
        free_slots = settings.trade_slots - len(positions)
        if free_slots <= 0:
            equity_curve.append((current_time, equity))
            continue

        for row in rows.sort_values(["score", "symbol"], ascending=[False, True]).to_dict("records"):
            if free_slots <= 0:
                break
            symbol = str(row["symbol"])
            if symbol in open_symbols:
                continue
            symbol_bars = _cached_symbol_bars(price_cache, root_path, symbol, start=start, end=end)
            entry_bar = _bar_at(symbol_bars, current_time)
            if entry_bar is None:
                continue
            new_position = _open_position(
                symbol,
                entry_bar,
                current_time=current_time,
                settings=settings,
                regime=str(row["regime"]),
            )
            if new_position is None:
                continue
            planned_trade = _plan_exit_trade(new_position, symbol_bars, settings=settings)
            positions.append((new_position, planned_trade))
            open_symbols.add(symbol)
            free_slots -= 1
        equity_curve.append((current_time, equity))

    for _, planned_trade in sorted(positions, key=lambda item: item[1].exit_at):
        trades.append(planned_trade)
        equity += planned_trade.net_pnl

    return ReplayResult(
        trades=sorted(trades, key=lambda trade: trade.exit_at),
        equity_curve=equity_curve,
        starting_capital=settings.paper_capital,
        ending_capital=equity,
        caveats=caveats,
    )


def _cached_symbol_bars(
    cache: dict[str, pd.DataFrame],
    root_path: Path,
    symbol: str,
    *,
    start: date,
    end: date,
) -> pd.DataFrame:
    normalized = symbol.upper()
    if normalized not in cache:
        path = root_path / normalized / f"{start.year}.parquet"
        cache[normalized] = _read_symbol_cache(path, start=start, end=end)
    return cache[normalized]


def _bar_at(frame: pd.DataFrame, timestamp: datetime) -> MinuteBar | None:
    matches = frame[frame["timestamp"] == pd.Timestamp(timestamp)]
    if matches.empty:
        return None
    return _row_to_bar(matches.iloc[0].to_dict())


def _plan_exit_trade(
    position: _OpenPosition,
    symbol_bars: pd.DataFrame,
    *,
    settings: DriftPilotSettings,
) -> BacktestTrade:
    entry_timestamp = pd.Timestamp(position.entry_at)
    deadline = entry_timestamp + pd.Timedelta(minutes=settings.max_hold_minutes)
    future = symbol_bars[
        (symbol_bars["timestamp"] > entry_timestamp) & (symbol_bars["timestamp"] <= deadline)
    ].copy()
    if future.empty:
        entry_bar = _bar_at(symbol_bars, position.entry_at)
        if entry_bar is None:
            raise ValueError(f"missing entry bar for {position.symbol} at {position.entry_at.isoformat()}")
        return _close_position(position, entry_bar, "TIME")
    target_or_stop = future[(future["close"] >= position.target_price) | (future["close"] <= position.stop_price)]
    if target_or_stop.empty:
        exit_row = future.iloc[-1]
        exit_reason = "TIME"
    else:
        exit_row = target_or_stop.iloc[0]
        exit_reason = "TARGET" if float(exit_row["close"]) >= position.target_price else "STOP"
    return _close_position(position, _row_to_bar(exit_row.to_dict()), exit_reason)


def replay_bars(
    bars: pd.DataFrame,
    *,
    settings: DriftPilotSettings | None = None,
    rvol_lookback: int = 20,
    point_in_time_constituents: bool = False,
    signal_name: str | None = None,
    history_cap_minutes: int | None = None,
) -> ReplayResult:
    settings = settings or DriftPilotSettings()
    signal = get_signal(signal_name or settings.active_signal)
    _validate_signal_compatibility(signal)
    # Per refactor plan v1.1 § 6 Task 5.2: collect data-dependency skip events
    # so the report's diagnostics block can surface them. List of (timestamp, reason).
    _data_dependency_skips: list[tuple[datetime, str]] = []
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

    # Per refactor plan v1.1 Phase 6: cap per-symbol bar history at
    # `history_cap_minutes` to keep memory bounded over long replays.
    # Without a cap, 1500 symbols × ~100k bars/year × ~200 bytes/MinuteBar
    # ≈ 30 GB per process — silently OOM-killed on memory-constrained
    # shared boxes (DGX with vllm running, etc). Capping at 180 minutes
    # keeps the per-process working set near 50 MB.
    #
    # The cap is OPT-IN because intraday_momentum_v1's same-minute-of-day
    # RVOL needs ~20 trading days of bar history (~7,800 bars) per symbol.
    # The four new signals (Stationary Ghost / Whale-Tail / RS-Drift /
    # Apex Hunter) do NOT need that and can safely cap at 180 minutes.
    # Generic-path callers pass `history_cap_minutes=MAX_HISTORY_MINUTES`;
    # tests and intraday-momentum callers leave it None (preserve old
    # behavior).
    cap: int | None = history_cap_minutes
    # Per-month progress checkpoints so a silent OOM-kill leaves a trace of
    # how far the replay got. Only fires when the harness sees real activity
    # (i.e. `_log` prints to stdout, captured by nohup'd log files).
    _last_logged_month: tuple[int, int] | None = None
    _timestamps_seen = 0
    for timestamp, rows in normalized.groupby("timestamp", sort=True):
        current_time = _as_aware_datetime(timestamp)
        _timestamps_seen += 1
        month_key = (current_time.year, current_time.month)
        if month_key != _last_logged_month:
            _log(
                f"replay_bars: entering {current_time.date()} "
                f"({_timestamps_seen:,} bars processed, "
                f"{len(trades)} trades closed, equity ${equity:,.2f})"
            )
            _last_logged_month = month_key
        latest_by_symbol: dict[str, MinuteBar] = {}
        for row in rows.to_dict("records"):
            bar = _row_to_bar(row)
            symbol_history = history.setdefault(bar.symbol, [])
            symbol_history.append(bar)
            if cap is not None and len(symbol_history) > cap:
                del symbol_history[: len(symbol_history) - cap]
            latest_by_symbol[bar.symbol] = bar
            quotes[bar.symbol] = _row_to_quote(row, bar, modeled=not has_quote_columns)

        exits = _evaluate_exits(
            positions,
            latest_by_symbol,
            current_time=current_time,
            settings=settings,
            signal=signal,
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
                regime, queue = signal.scan(
                    universe_history,
                    quotes,
                    history["SPY"],
                    rvol_lookback=rvol_lookback,
                )
            except InsufficientDataError as exc:
                # Per plan § 3 Task 2.2: log a non-trading "data dependency
                # skip" event and return empty for this cycle. Don't crash.
                _data_dependency_skips.append((current_time, str(exc)))
                queue = []
                regime = None
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


def _require_vectorized_signal(signal_name: str) -> None:
    if signal_name != DEFAULT_SIGNAL:
        raise NotImplementedError(
            f"vectorized parquet replay currently supports {DEFAULT_SIGNAL}; got {signal_name}"
        )


def _log(msg: str) -> None:
    """Lightweight progress logger. Prints with timestamp + flushes so
    nohup'd processes show progress in their log files in real time
    (Python `-u` flag also helps; this belt-and-braces explicit flush).
    """
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def replay_parquet_cache_generic(
    root: str | Path,
    *,
    start: date,
    end: date,
    settings: DriftPilotSettings | None = None,
    rvol_lookback: int = 20,
    point_in_time_constituents: bool = False,
    signal_name: str | None = None,
) -> ReplayResult:
    """Per-symbol streaming Databento replay for any registered signal.

    This is the harness path for the four locked v1 signals (stationary_ghost,
    whale_tail, rs_drift, apex_hunter). It loads SPY first (every locked spec
    that needs SPY context — RS-Drift's relative strength, Apex's relative
    alpha, Whale-Tail/Stationary Ghost regime classification — relies on SPY
    being available before the universe loop). Then it streams symbol parquet
    files into one DataFrame and dispatches to `replay_bars`, which calls
    `signal.scan(...)` and `signal.evaluate_exit(...)` per the signal contract.

    intraday_momentum_v1 keeps using the special-cased vectorized path in
    `replay_parquet_cache` for performance. New signals trade some throughput
    for the ability to run their own logic.
    """
    settings = settings or DriftPilotSettings()
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"parquet bar root does not exist: {root_path}")

    sig_name = signal_name or settings.active_signal
    _log(f"replay_parquet_cache_generic: signal={sig_name} start={start} end={end} root={root_path}")

    spy_file = root_path / "SPY" / f"{start.year}.parquet"
    if not spy_file.exists():
        raise FileNotFoundError(
            f"SPY bar file is required (every locked v1 signal needs SPY context): {spy_file}"
        )

    files = sorted(root_path.glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet bar files found under {root_path}")
    _log(f"loading bars: {len(files)} parquet files (incl SPY) — this is the memory-heavy step")

    frames: list[pd.DataFrame] = [_read_symbol_cache(spy_file, start=start, end=end)]
    progress_every = max(1, len(files) // 10)
    loaded = 1
    for idx, file in enumerate(files):
        if file.parent.name.upper() == "SPY":
            continue
        frame = _read_symbol_cache(file, start=start, end=end)
        if not frame.empty:
            frames.append(frame)
            loaded += 1
        if idx > 0 and idx % progress_every == 0:
            _log(f"  parquet load progress: {idx}/{len(files)} files visited, {loaded} non-empty kept")

    _log(f"parquet load done: {loaded} non-empty frames; concatenating...")

    if not frames:
        return ReplayResult(
            trades=[],
            equity_curve=[],
            starting_capital=settings.paper_capital,
            ending_capital=settings.paper_capital,
            caveats=["No bars were available for the requested period."],
        )

    bars = pd.concat(frames, ignore_index=True)
    _log(
        f"concat done: {len(bars):,} rows in unified frame "
        f"(memory peak around here — if OOM, this is where it dies)"
    )
    # Free the per-symbol frame list so concat'd 'bars' has the only reference.
    del frames

    result = replay_bars(
        bars,
        settings=settings,
        rvol_lookback=rvol_lookback,
        point_in_time_constituents=point_in_time_constituents,
        signal_name=sig_name,
        # Phase 6: bound memory for the four new signals' year-long replay.
        # See replay_bars docstring re: why this is opt-in.
        history_cap_minutes=MAX_HISTORY_MINUTES,
    )
    _log(
        f"replay_bars done: {len(result.trades)} trades, "
        f"final equity ${result.ending_capital:,.2f}"
    )
    return result


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
    signal: Any | None = None,
) -> list[tuple[_OpenPosition, MinuteBar, str]]:
    """Evaluate every open position against its latest bar.

    A signal that implements `evaluate_exit(position, latest_bar, settings)`
    gets first say. If the signal returns `should_exit=True`, that reason
    wins. If `should_exit=False`, fall back to the default TARGET/STOP/TIME
    rules. Signals without `evaluate_exit` go straight to the defaults.

    The signal may also mutate `position.metadata` to track per-position
    state across bars (peak unrealized P&L, ratchet stage, break-even
    triggered, etc.) — that's what the `metadata` field exists for.
    """
    exits: list[tuple[_OpenPosition, MinuteBar, str]] = []
    max_hold = timedelta(minutes=settings.max_hold_minutes)
    custom_exit = getattr(signal, "evaluate_exit", None) if signal is not None else None
    for position in positions:
        latest = latest_by_symbol.get(position.symbol)
        if latest is None:
            continue
        if custom_exit is not None:
            decision = custom_exit(position, latest, settings)
            if getattr(decision, "should_exit", False):
                reason = getattr(decision, "exit_reason", None) or "CUSTOM"
                exits.append((position, latest, reason))
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
    # TODO[phase 3.3 wiring]: replace this fallback with the value the signal's
    # evaluate_exit wrote into `position.metadata["peak_unrealized_pct"]` once
    # limit_fill.py wires signal-attempted tracking into replay.
    peak_unrealized_pct: float = float(position.metadata.get("peak_unrealized_pct", 0.0))
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
        peak_unrealized_pct=peak_unrealized_pct,
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
