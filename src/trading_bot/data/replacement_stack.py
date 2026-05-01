from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import time
from typing import Any

import httpx
import pandas as pd

from trading_bot.data.market_data import CompanyProfile, EarningsEvent, MarketDataProvider, MomentumFundamentals
from trading_bot.settings import AppSettings


class ReplacementStackMarketDataProvider:
    """Market data provider matching the replacement stack.

    Earnings: Finnhub. Fundamentals: FMP. OHLCV/live price: Alpaca, with optional
    Polygon historical bars when POLYGON_API_KEY is configured.
    """

    def __init__(self, settings: AppSettings, *, timeout: float = 15.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def company_profile(self, ticker: str) -> CompanyProfile:
        try:
            profile = self._fmp_profile(ticker)
        except RuntimeError:
            profile = {}
        price = self._alpaca_latest_price(ticker) or _float(profile.get("price")) or 0.0
        try:
            analyst_count = self._fmp_analyst_count(ticker)
        except RuntimeError:
            analyst_count = 0
        return CompanyProfile(
            ticker=ticker.upper(),
            market_cap_m=(_float(profile.get("marketCap")) or 0.0) / 1_000_000,
            analyst_count=analyst_count,
            current_price=price,
            avg_volume=_float(profile.get("volAvg")) or _float(profile.get("avgVolume")) or _float(profile.get("averageVolume")) or 0.0,
            shortable=True,
        )

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        if self.settings.polygon_api_key is not None:
            try:
                frame = self._polygon_daily_history(ticker, period=period)
                if not frame.empty:
                    return frame
            except RuntimeError:
                pass
        return self._alpaca_daily_history(ticker, period=period)

    def latest_earnings_event(self, ticker: str, scan_date: date) -> EarningsEvent | None:
        if self.settings.finnhub_api_key is None:
            return None
        start = scan_date - timedelta(days=3)
        data = self._get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": start.isoformat(),
                "to": scan_date.isoformat(),
                "symbol": ticker.upper(),
                "token": self.settings.finnhub_api_key.get_secret_value(),
            },
        )
        rows = data.get("earningsCalendar") or []
        valid = [_finnhub_event(row, ticker) for row in rows]
        events = [event for event in valid if event is not None]
        return max(events, key=lambda event: event.earnings_date) if events else None

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
        ratios = self._fmp_ratios_ttm(ticker)
        return MomentumFundamentals(
            ticker=ticker.upper(),
            earnings_surprises_pct=self._finnhub_surprise_history(ticker),
            roe=_ratio_to_pct(_first_number(ratios, ["returnOnEquityTTM", "returnOnEquity"])),
            debt_to_equity=_debt_to_equity(_first_number(ratios, ["debtEquityRatioTTM", "debtEquityRatio"])),
            profit_margin=_ratio_to_pct(_first_number(ratios, ["netProfitMarginTTM", "netProfitMargin"])),
        )

    def spy_premarket_change_pct(self) -> float | None:
        latest = self._alpaca_latest_price("SPY")
        history = self._alpaca_daily_history("SPY", period="10d")
        if latest is None or history.empty:
            return None
        previous_close = float(history["close"].dropna().iloc[-1])
        return (latest / previous_close - 1) * 100 if previous_close else None

    def _fmp_profile(self, ticker: str) -> dict[str, Any]:
        if self.settings.fmp_api_key is None:
            return {}
        data = self._get(
            "https://financialmodelingprep.com/stable/profile",
            params={"symbol": ticker.upper(), "apikey": self.settings.fmp_api_key.get_secret_value()},
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def _fmp_ratios_ttm(self, ticker: str) -> dict[str, Any]:
        if self.settings.fmp_api_key is None:
            return {}
        data = self._get(
            "https://financialmodelingprep.com/stable/ratios-ttm",
            params={"symbol": ticker.upper(), "apikey": self.settings.fmp_api_key.get_secret_value()},
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def _fmp_analyst_count(self, ticker: str) -> int:
        if self.settings.fmp_api_key is None:
            return 0
        try:
            data = self._get(
                "https://financialmodelingprep.com/stable/analyst-estimates",
                params={"symbol": ticker.upper(), "period": "quarter", "limit": 1, "apikey": self.settings.fmp_api_key.get_secret_value()},
            )
        except RuntimeError as exc:
            if "status=402" in str(exc) or "status=403" in str(exc) or "status=404" in str(exc):
                return 0
            raise
        row = data[0] if isinstance(data, list) and data else {}
        value = _first_number(row, ["numberAnalystEstimatedEps", "numberAnalystsEstimatedEps", "estimatedEpsAvgNumberAnalyst"])
        return int(value or 0)

    def _finnhub_surprise_history(self, ticker: str) -> list[float]:
        if self.settings.finnhub_api_key is None:
            return []
        end = date.today()
        start = end - timedelta(days=550)
        data = self._get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "symbol": ticker.upper(),
                "token": self.settings.finnhub_api_key.get_secret_value(),
            },
        )
        events = [_finnhub_event(row, ticker) for row in data.get("earningsCalendar") or []]
        surprises = []
        for event in sorted([event for event in events if event is not None], key=lambda item: item.earnings_date, reverse=True):
            if event.estimate_eps:
                surprises.append((event.actual_eps / event.estimate_eps - 1) * 100)
            if len(surprises) == 4:
                break
        return surprises

    def _alpaca_daily_history(self, ticker: str, *, period: str) -> pd.DataFrame:
        key, secret = self._alpaca_keys()
        days = _period_days(period)
        start = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        data = self._get(
            f"https://data.alpaca.markets/v2/stocks/{ticker.upper()}/bars",
            params={"timeframe": "1Day", "start": start, "adjustment": "split", "feed": "iex", "limit": 10000},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )
        bars = data.get("bars") or []
        return _bars_to_frame(bars)

    def _polygon_daily_history(self, ticker: str, *, period: str) -> pd.DataFrame:
        assert self.settings.polygon_api_key is not None
        end = date.today()
        start = end - timedelta(days=_period_days(period))
        data = self._get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self.settings.polygon_api_key.get_secret_value()},
        )
        return _bars_to_frame(data.get("results") or [], polygon=True)

    def _alpaca_latest_price(self, ticker: str) -> float | None:
        key, secret = self._alpaca_keys()
        data = self._get(
            f"https://data.alpaca.markets/v2/stocks/{ticker.upper()}/trades/latest",
            params={"feed": "iex"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )
        return _float((data.get("trade") or {}).get("p"))

    def _alpaca_keys(self) -> tuple[str, str]:
        if self.settings.alpaca_api_key is None or self.settings.alpaca_secret_key is None:
            raise RuntimeError("Alpaca market data keys are not configured")
        return self.settings.alpaca_api_key.get_secret_value(), self.settings.alpaca_secret_key.get_secret_value()

    def _get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        attempts = self.settings.market_data_retry_attempts
        backoff = self.settings.market_data_retry_backoff_seconds
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if not _should_retry_status(exc.response.status_code) or attempt == attempts:
                    raise RuntimeError(f"{exc.response.request.url.host} returned status={exc.response.status_code}") from exc
                _sleep_before_retry(exc.response, attempt=attempt, backoff=backoff)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == attempts:
                    host = exc.request.url.host if exc.request is not None else "provider"
                    raise RuntimeError(f"{host} request failed after {attempts} attempts: {exc.__class__.__name__}") from exc
                _sleep_before_retry(None, attempt=attempt, backoff=backoff)
        raise RuntimeError(f"provider request failed: {last_error}")


def _finnhub_event(row: dict[str, Any], ticker: str) -> EarningsEvent | None:
    actual = _float(row.get("epsActual"))
    estimate = _float(row.get("epsEstimate"))
    event_date = row.get("date")
    if actual is None or estimate in (None, 0) or not event_date:
        return None
    return EarningsEvent(
        ticker=ticker.upper(),
        earnings_date=date.fromisoformat(str(event_date)[:10]),
        actual_eps=actual,
        estimate_eps=estimate,
        text=f"{ticker.upper()} earnings report. Actual EPS {actual}; estimate EPS {estimate}.",
    )


def _bars_to_frame(bars: list[dict[str, Any]], *, polygon: bool = False) -> pd.DataFrame:
    rows = []
    for bar in bars:
        rows.append(
            {
                "open": _float(bar.get("o")),
                "high": _float(bar.get("h")),
                "low": _float(bar.get("l")),
                "close": _float(bar.get("c")),
                "volume": _float(bar.get("v")),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("No daily history returned")
    return frame.dropna(subset=["open", "high", "low", "close"])


def _should_retry_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _sleep_before_retry(response: httpx.Response | None, *, attempt: int, backoff: float) -> None:
    if backoff <= 0:
        return
    retry_after = _retry_after_seconds(response)
    delay = retry_after if retry_after is not None else backoff * (2 ** (attempt - 1))
    time.sleep(min(delay, 30.0))


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _period_days(period: str) -> int:
    if period.endswith("y"):
        return int(period[:-1]) * 370
    if period.endswith("mo"):
        return int(period[:-2]) * 31
    if period.endswith("d"):
        return int(period[:-1])
    return 370


def _first_number(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    return float(value)


def _ratio_to_pct(value: float | None) -> float:
    if value is None:
        return 0.0
    return value * 100 if abs(value) <= 1 else value


def _debt_to_equity(value: float | None) -> float:
    if value is None:
        return 99.0
    return value / 100 if value > 10 else value
