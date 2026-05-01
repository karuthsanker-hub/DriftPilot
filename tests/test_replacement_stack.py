from __future__ import annotations

from datetime import date

import httpx
from pydantic import SecretStr

from trading_bot.data.replacement_stack import ReplacementStackMarketDataProvider
from trading_bot.settings import AppSettings


def test_replacement_stack_maps_finnhub_earnings(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        finnhub_api_key=SecretStr("finnhub-key"),
        fmp_api_key=SecretStr("fmp-key"),
    )
    provider = ReplacementStackMarketDataProvider(settings)

    def fake_get(url, *, params=None, headers=None):
        assert "finnhub" in url
        return {"earningsCalendar": [{"date": "2026-04-24", "epsActual": 1.2, "epsEstimate": 1.0}]}

    monkeypatch.setattr(provider, "_get", fake_get)

    event = provider.latest_earnings_event("ABC", date(2026, 4, 26))

    assert event is not None
    assert event.ticker == "ABC"
    assert event.actual_eps == 1.2


def test_replacement_stack_maps_fmp_profile_and_alpaca_price(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        fmp_api_key=SecretStr("fmp-key"),
    )
    provider = ReplacementStackMarketDataProvider(settings)

    monkeypatch.setattr(provider, "_fmp_profile", lambda ticker: {"marketCap": 800_000_000, "volAvg": 100_000})
    monkeypatch.setattr(provider, "_fmp_analyst_count", lambda ticker: 3)
    monkeypatch.setattr(provider, "_alpaca_latest_price", lambda ticker: 12.5)

    profile = provider.company_profile("abc")

    assert profile.ticker == "ABC"
    assert profile.market_cap_m == 800
    assert profile.analyst_count == 3
    assert profile.current_price == 12.5


def test_replacement_stack_profile_survives_fmp_rate_limit_for_live_price(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        fmp_api_key=SecretStr("fmp-key"),
    )
    provider = ReplacementStackMarketDataProvider(settings)

    monkeypatch.setattr(provider, "_fmp_profile", lambda ticker: (_ for _ in ()).throw(RuntimeError("financialmodelingprep.com returned status=429")))
    monkeypatch.setattr(provider, "_fmp_analyst_count", lambda ticker: (_ for _ in ()).throw(RuntimeError("financialmodelingprep.com returned status=429")))
    monkeypatch.setattr(provider, "_alpaca_latest_price", lambda ticker: 42.5)

    profile = provider.company_profile("abc")

    assert profile.ticker == "ABC"
    assert profile.current_price == 42.5
    assert profile.analyst_count == 0


def test_replacement_stack_daily_history_falls_back_to_alpaca_when_polygon_rate_limited(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        polygon_api_key=SecretStr("polygon-key"),
    )
    provider = ReplacementStackMarketDataProvider(settings)

    monkeypatch.setattr(provider, "_polygon_daily_history", lambda ticker, *, period: (_ for _ in ()).throw(RuntimeError("api.polygon.io returned status=429")))
    monkeypatch.setattr(provider, "_alpaca_daily_history", lambda ticker, *, period: {"source": "alpaca"})

    assert provider.daily_history("ABC") == {"source": "alpaca"}


def test_replacement_stack_retries_rate_limited_requests(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        market_data_retry_attempts=3,
        market_data_retry_backoff_seconds=0,
    )
    provider = ReplacementStackMarketDataProvider(settings)
    calls = {"count": 0}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls["count"] += 1
        request = httpx.Request("GET", url)
        if calls["count"] == 1:
            return httpx.Response(429, request=request, json={"error": "slow down"})
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(httpx, "get", fake_get)

    assert provider._get("https://api.example.test/data") == {"ok": True}
    assert calls["count"] == 2


def test_replacement_stack_does_not_retry_bad_request(monkeypatch) -> None:
    settings = AppSettings(
        alpaca_api_key=SecretStr("alpaca-key"),
        alpaca_secret_key=SecretStr("alpaca-secret"),
        market_data_retry_attempts=3,
        market_data_retry_backoff_seconds=0,
    )
    provider = ReplacementStackMarketDataProvider(settings)
    calls = {"count": 0}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        calls["count"] += 1
        request = httpx.Request("GET", url)
        return httpx.Response(400, request=request, json={"error": "bad"})

    monkeypatch.setattr(httpx, "get", fake_get)

    try:
        provider._get("https://api.example.test/data")
    except RuntimeError as exc:
        assert "status=400" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert calls["count"] == 1
