from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from trading_bot.data.market_data import EarningsEvent


@dataclass(frozen=True)
class EarningsEventStore:
    path: Path

    @classmethod
    def from_env_path(cls, value: str, *, env_path: str | Path = ".env") -> "EarningsEventStore":
        path = Path(value)
        if not path.is_absolute():
            path = Path(env_path).expanduser().resolve().parent / path
        return cls(path)

    def latest_event(self, ticker: str, scan_date: date) -> EarningsEvent | None:
        events = [
            event
            for event in self.events_for_ticker(ticker)
            if scan_date - timedelta(days=3) <= event.earnings_date <= scan_date
        ]
        return max(events, key=lambda event: event.earnings_date) if events else None

    def surprise_history(self, ticker: str, *, limit: int = 4) -> list[float]:
        surprises: list[float] = []
        events = sorted(self.events_for_ticker(ticker), key=lambda event: event.earnings_date, reverse=True)
        for event in events:
            if event.estimate_eps == 0:
                continue
            surprises.append((event.actual_eps / event.estimate_eps - 1) * 100)
            if len(surprises) == limit:
                break
        return surprises

    def events_for_ticker(self, ticker: str) -> list[EarningsEvent]:
        if not self.path.exists():
            return []
        target = ticker.strip().upper()
        events: list[EarningsEvent] = []
        with self.path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("ticker") or "").strip().upper() != target:
                    continue
                actual = _float(row.get("actual_eps"))
                estimate = _float(row.get("estimate_eps"))
                event_date = _date(row.get("earnings_date"))
                if actual is None or estimate is None or event_date is None:
                    continue
                text = row.get("text") or f"{target} earnings report. Actual EPS {actual}; estimate EPS {estimate}."
                events.append(EarningsEvent(ticker=target, earnings_date=event_date, actual_eps=actual, estimate_eps=estimate, text=text))
        return events

    def write_events(self, events: list[EarningsEvent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(events, key=lambda event: (event.ticker, event.earnings_date), reverse=True)
        with self.path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "earnings_date", "actual_eps", "estimate_eps", "text"])
            writer.writeheader()
            for event in rows:
                writer.writerow(
                    {
                        "ticker": event.ticker,
                        "earnings_date": event.earnings_date.isoformat(),
                        "actual_eps": event.actual_eps,
                        "estimate_eps": event.estimate_eps,
                        "text": event.text,
                    }
                )


def _float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _date(value: str | None) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(value)
