from __future__ import annotations

import csv
from pathlib import Path

from trading_bot.settings import AppSettings


def load_pead_universe(settings: AppSettings, *, env_path: str | Path = ".env") -> list[str]:
    """Load the PEAD scan universe from env override or a local CSV file."""
    if settings.pead_scan_tickers:
        return _dedupe(settings.pead_scan_tickers)

    universe_path = Path(settings.pead_universe_file)
    if not universe_path.is_absolute():
        universe_path = Path(env_path).expanduser().resolve().parent / universe_path
    if not universe_path.exists():
        return []

    with universe_path.open(newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        return []

    first_cell = rows[0][0].strip().lower() if rows[0] else ""
    data_rows = rows[1:] if first_cell in {"ticker", "symbol"} else rows
    tickers = [row[0] for row in data_rows if row]
    return _dedupe(tickers)


def _dedupe(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ticker in tickers:
        normalized = ticker.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
