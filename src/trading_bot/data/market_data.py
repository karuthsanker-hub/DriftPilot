from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

import pandas as pd


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    market_cap_m: float
    analyst_count: int
    current_price: float
    avg_volume: float
    shortable: bool = True


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    earnings_date: date
    actual_eps: float
    estimate_eps: float
    text: str


@dataclass(frozen=True)
class MomentumFundamentals:
    ticker: str
    earnings_surprises_pct: list[float]
    roe: float
    debt_to_equity: float
    profit_margin: float


class MarketDataProvider(Protocol):
    def company_profile(self, ticker: str) -> CompanyProfile: ...

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame: ...

    def latest_earnings_event(self, ticker: str, scan_date: date) -> EarningsEvent | None: ...

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals: ...

    def spy_premarket_change_pct(self) -> float | None: ...


class YFinanceMarketDataProvider:
    def company_profile(self, ticker: str) -> CompanyProfile:
        import yfinance as yf

        symbol = yf.Ticker(ticker)
        info = symbol.info or {}
        market_cap = float(info.get("marketCap") or 0) / 1_000_000
        analyst_count = int(info.get("numberOfAnalystOpinions") or 0)
        current_price = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
        avg_volume = float(info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0)
        return CompanyProfile(
            ticker=ticker.upper(),
            market_cap_m=market_cap,
            analyst_count=analyst_count,
            current_price=current_price,
            avg_volume=avg_volume,
            shortable=True,
        )

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        import yfinance as yf

        frame = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=False)
        if frame.empty:
            raise RuntimeError(f"No daily history returned for {ticker}")
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [str(col[0]).lower() for col in frame.columns]
        else:
            frame.columns = [str(col).lower() for col in frame.columns]
        return frame.rename(columns={"adj close": "adj_close"})

    def latest_earnings_event(self, ticker: str, scan_date: date) -> EarningsEvent | None:
        import yfinance as yf

        symbol = yf.Ticker(ticker)
        earnings_dates = _safe_attr(symbol, "earnings_dates")
        if earnings_dates is None or getattr(earnings_dates, "empty", True):
            return None

        frame = earnings_dates.reset_index()
        date_col = frame.columns[0]
        frame["event_date"] = pd.to_datetime(frame[date_col]).dt.date
        candidates = frame[frame["event_date"].between(scan_date - timedelta(days=3), scan_date)]
        if candidates.empty:
            return None
        row = candidates.iloc[0]
        actual = _first_present(row, ["Reported EPS", "reportedEPS", "epsActual"])
        estimate = _first_present(row, ["EPS Estimate", "epsEstimate", "estimatedEPS"])
        if actual is None or estimate in (None, 0):
            return None
        text = f"{ticker.upper()} earnings report. Actual EPS {actual}; estimate EPS {estimate}."
        return EarningsEvent(
            ticker=ticker.upper(),
            earnings_date=row["event_date"],
            actual_eps=float(actual),
            estimate_eps=float(estimate),
            text=text,
        )

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
        import yfinance as yf

        symbol = yf.Ticker(ticker)
        info = symbol.info or {}
        earnings_dates = _safe_attr(symbol, "earnings_dates")
        surprises = _earnings_surprises_pct(earnings_dates)
        return MomentumFundamentals(
            ticker=ticker.upper(),
            earnings_surprises_pct=surprises[:4],
            roe=_ratio_to_pct(info.get("returnOnEquity")),
            debt_to_equity=_debt_to_equity(info.get("debtToEquity")),
            profit_margin=_ratio_to_pct(info.get("profitMargins")),
        )

    def spy_premarket_change_pct(self) -> float | None:
        import yfinance as yf

        info = yf.Ticker("SPY").info or {}
        pre = info.get("preMarketPrice")
        prev = info.get("previousClose")
        if not pre or not prev:
            return None
        return (float(pre) / float(prev) - 1) * 100


def _safe_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _first_present(row: Any, names: list[str]) -> Any:
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return None


def _earnings_surprises_pct(frame: Any) -> list[float]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = frame.reset_index()
    surprises: list[float] = []
    for _, row in rows.iterrows():
        actual = _first_present(row, ["Reported EPS", "reportedEPS", "epsActual"])
        estimate = _first_present(row, ["EPS Estimate", "epsEstimate", "estimatedEPS"])
        if actual is None or estimate in (None, 0):
            continue
        surprises.append((float(actual) / float(estimate) - 1) * 100)
        if len(surprises) == 4:
            break
    return surprises


def _ratio_to_pct(value: Any) -> float:
    if value is None:
        return 0.0
    number = float(value)
    return number * 100 if abs(number) <= 1 else number


def _debt_to_equity(value: Any) -> float:
    if value is None:
        return 99.0
    number = float(value)
    return number / 100 if number > 10 else number
