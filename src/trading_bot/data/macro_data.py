from __future__ import annotations

from typing import Protocol

import httpx

from trading_bot.settings import AppSettings


class MacroDataProvider(Protocol):
    def current_vix(self) -> float | None: ...


class FredMacroDataProvider:
    def __init__(self, settings: AppSettings, *, timeout: float = 10.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def current_vix(self) -> float | None:
        if self.settings.fred_api_key is not None:
            try:
                response = httpx.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": "VIXCLS",
                        "api_key": self.settings.fred_api_key.get_secret_value(),
                        "file_type": "json",
                        "limit": 1,
                        "sort_order": "desc",
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                observations = response.json().get("observations") or []
                if observations and observations[0].get("value") not in {None, "."}:
                    return float(observations[0]["value"])
            except Exception:
                pass
        return _vix_from_yfinance()


def _vix_from_yfinance() -> float | None:
    try:
        import yfinance as yf

        history = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        if history.empty:
            return None
        close = history["Close"] if "Close" in history else history["close"]
        return float(close.dropna().iloc[-1])
    except Exception:
        return None
