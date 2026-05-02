from __future__ import annotations

import argparse
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
from dotenv import dotenv_values


DEFAULT_DATASET = "EQUS.MINI"
DEFAULT_SCHEMA = "ohlcv-1m"
DEFAULT_ROOT = Path("data/bars/databento")
DEFAULT_SYMBOLS_FILE = Path("config/universe.csv")
REQUIRED_COLUMNS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class PullConfig:
    start: date
    end: date
    symbols: tuple[str, ...]
    dataset: str = DEFAULT_DATASET
    schema: str = DEFAULT_SCHEMA
    stype_in: str = "raw_symbol"
    root: Path = DEFAULT_ROOT
    batch_size: int = 50
    api_key: str = ""
    dry_run: bool = False
    max_cost: float | None = None
    skip_cost_check: bool = False


def main() -> None:
    args = _parse_args()
    symbols = load_symbols(args.symbols, args.symbols_file)
    api_key = args.api_key or _load_api_key(args.env_file)
    config = PullConfig(
        start=args.start,
        end=args.end,
        symbols=tuple(symbols),
        dataset=args.dataset,
        schema=args.schema,
        stype_in=args.stype_in,
        root=args.root,
        batch_size=args.batch_size,
        api_key=api_key,
        dry_run=args.dry_run,
        max_cost=args.max_cost,
        skip_cost_check=args.skip_cost_check,
    )
    written = pull_databento_bars(config)
    for path in written:
        print(path)


def load_symbols(raw_symbols: Iterable[str], symbols_file: Path | None) -> list[str]:
    symbols: set[str] = set()
    for raw in raw_symbols:
        symbols.update(_split_symbols(raw))

    if symbols_file is not None and symbols_file.exists():
        frame = pd.read_csv(symbols_file)
        symbol_column = "symbol" if "symbol" in frame.columns else frame.columns[0]
        symbols.update(str(value).upper() for value in frame[symbol_column].dropna())

    symbols.add("SPY")
    return sorted(symbol for symbol in symbols if symbol)


def pull_databento_bars(config: PullConfig) -> list[Path]:
    if config.start > config.end:
        raise ValueError("start must be on or before end")
    if not config.symbols:
        raise ValueError("at least one symbol is required")
    if config.batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not config.api_key:
        raise RuntimeError("DATABENTO_API_KEY is required to pull historical bars")

    try:
        import databento as db  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Install the databento package to pull historical bars") from exc

    client = db.Historical(config.api_key)
    if not config.skip_cost_check:
        estimated_cost = estimate_databento_cost(client, config)
        print(f"Databento estimated cost: ${estimated_cost:.2f}")
        if config.max_cost is not None and estimated_cost > config.max_cost:
            raise RuntimeError(
                f"Databento estimated cost ${estimated_cost:.2f} exceeds --max-cost ${config.max_cost:.2f}"
            )
    if config.dry_run:
        return []

    written: list[Path] = []
    for symbols in _chunks(config.symbols, config.batch_size):
        data = client.timeseries.get_range(
            dataset=config.dataset,
            schema=config.schema,
            symbols=list(symbols),
            stype_in=config.stype_in,
            start=config.start.isoformat(),
            end=_exclusive_end(config.end),
        )
        frame = normalize_databento_frame(data.to_df())
        written.extend(write_symbol_year_cache(frame, config.root))
    return sorted(set(written))


def estimate_databento_cost(client: Any, config: PullConfig) -> float:
    total = 0.0
    for symbols in _chunks(config.symbols, config.batch_size):
        cost = client.metadata.get_cost(
            dataset=config.dataset,
            schema=config.schema,
            symbols=list(symbols),
            stype_in=config.stype_in,
            start=config.start.isoformat(),
            end=_exclusive_end(config.end),
        )
        total += _cost_to_float(cost)
    return total


def normalize_databento_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    normalized = frame.reset_index()
    timestamp_column = _first_present(normalized, ("timestamp", "ts_event", "ts_recv", "index"))
    if timestamp_column is None:
        raise ValueError("Databento frame did not include ts_event, ts_recv, or timestamp")
    if "symbol" not in normalized.columns:
        raise ValueError("Databento frame did not include symbol; request text symbols from Databento")

    rename_map = {timestamp_column: "timestamp"}
    if "open" not in normalized.columns and "open_price" in normalized.columns:
        rename_map["open_price"] = "open"
    if "high" not in normalized.columns and "high_price" in normalized.columns:
        rename_map["high_price"] = "high"
    if "low" not in normalized.columns and "low_price" in normalized.columns:
        rename_map["low_price"] = "low"
    if "close" not in normalized.columns and "close_price" in normalized.columns:
        rename_map["close_price"] = "close"
    normalized = normalized.rename(columns=rename_map)

    missing = set(REQUIRED_COLUMNS).difference(normalized.columns)
    if missing:
        raise ValueError(f"Databento frame missing required columns: {sorted(missing)}")

    normalized = normalized.loc[:, list(REQUIRED_COLUMNS)].copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True)
    normalized["symbol"] = normalized["symbol"].astype(str).str.upper()
    for column in ("open", "high", "low", "close", "volume"):
        normalized[column] = pd.to_numeric(normalized[column])
    return normalized.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def write_symbol_year_cache(frame: pd.DataFrame, root: Path) -> list[Path]:
    if frame.empty:
        return []
    written: list[Path] = []
    for (symbol, year), group in frame.groupby(["symbol", frame["timestamp"].dt.year], sort=True):
        path = root / str(symbol) / f"{int(year)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        combined = group.loc[:, list(REQUIRED_COLUMNS)]
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, combined], ignore_index=True)
            combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
            combined["symbol"] = combined["symbol"].astype(str).str.upper()
            combined = combined.drop_duplicates(["timestamp", "symbol"], keep="last")
        combined = combined.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
        combined.to_parquet(path, index=False)
        written.append(path)
    return written


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull Databento 1-minute bars into the DriftPilot Parquet cache.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--stype-in", default="raw_symbol")
    parser.add_argument("--symbol", "--symbols", dest="symbols", action="append", default=[])
    parser.add_argument("--symbols-file", type=Path, default=DEFAULT_SYMBOLS_FILE)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--max-cost", type=float, default=None)
    parser.add_argument("--skip-cost-check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _load_api_key(env_file: Path) -> str:
    if "DATABENTO_API_KEY" in os.environ:
        return os.environ["DATABENTO_API_KEY"]
    if env_file.exists():
        values = dotenv_values(env_file)
        value = values.get("DATABENTO_API_KEY")
        if value:
            return value
    return ""


def _cost_to_float(cost: object) -> float:
    if isinstance(cost, int | float):
        return float(cost)
    if isinstance(cost, str):
        return float(cost.replace("$", "").replace(",", ""))
    for attr in ("cost", "amount", "total"):
        value = getattr(cost, attr, None)
        if value is not None:
            return _cost_to_float(value)
    if isinstance(cost, dict):
        for key in ("cost", "amount", "total"):
            if key in cost:
                return _cost_to_float(cost[key])
    raise TypeError(f"unsupported Databento cost response: {cost!r}")


def _split_symbols(raw: str) -> set[str]:
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _chunks(symbols: Iterable[str], size: int) -> Iterable[tuple[str, ...]]:
    batch: list[str] = []
    for symbol in symbols:
        batch.append(symbol)
        if len(batch) == size:
            yield tuple(batch)
            batch = []
    if batch:
        yield tuple(batch)


def _exclusive_end(value: date) -> str:
    return (value + timedelta(days=1)).isoformat()


def _first_present(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for column in candidates:
        if column in frame.columns:
            return column
    return None


if __name__ == "__main__":
    main()
