"""REST-based quote provider for the live broker client.

For catalyst signals (event-driven, not bar-driven), we don't need the
SIP WebSocket stream. We just need current bid/ask at the moment of
entry/exit. Alpaca's REST `/v2/stocks/{symbol}/quotes/latest` is fine.

This implements the `QuoteProvider` protocol expected by
`AlpacaBrokerClient`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from driftpilot.market_data.alpaca_stream import MarketQuote

logger = logging.getLogger(__name__)


class AlpacaRestQuoteProvider:
    """Synchronous REST quote provider with a small in-memory cache.

    The broker client calls `latest_quote(symbol)` synchronously inside
    `submit_entry_order` / `submit_exit_order`. We cache the last result
    for `cache_ttl_s` seconds so a flurry of entries on the same bar
    doesn't burn through API rate limits.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        cache_ttl_s: float = 1.0,
        client=None,  # injected for tests
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._cache_ttl_s = cache_ttl_s
        self._client = client
        self._cache: dict[str, tuple[float, MarketQuote]] = {}
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is not None:
            return self._client
        from alpaca.data.historical.stock import StockHistoricalDataClient
        return StockHistoricalDataClient(
            api_key=self._api_key, secret_key=self._api_secret
        )

    def latest_quote(self, symbol: str) -> MarketQuote | None:
        sym = symbol.upper()
        now_t = time.time()

        with self._lock:
            cached = self._cache.get(sym)
        if cached is not None:
            ts, quote = cached
            if now_t - ts < self._cache_ttl_s:
                return quote

        try:
            from alpaca.data.requests import StockLatestQuoteRequest

            client = self._get_client()
            req = StockLatestQuoteRequest(symbol_or_symbols=sym)
            result = client.get_stock_latest_quote(req)
            # alpaca-py returns dict[str, Quote]
            raw = result.get(sym) if isinstance(result, dict) else getattr(result, sym, None)
            if raw is None:
                return None
            quote = MarketQuote(
                symbol=sym,
                timestamp=getattr(raw, "timestamp", datetime.now(timezone.utc)),
                bid_price=float(getattr(raw, "bid_price", 0.0) or 0.0),
                ask_price=float(getattr(raw, "ask_price", 0.0) or 0.0),
                bid_size=_safe_float(getattr(raw, "bid_size", None)),
                ask_size=_safe_float(getattr(raw, "ask_size", None)),
            )
            if quote.bid_price <= 0 or quote.ask_price <= 0:
                return None  # invalid quote — broker will reject

            with self._lock:
                self._cache[sym] = (now_t, quote)
            return quote
        except Exception as exc:  # noqa: BLE001 — broker treats None as no quote
            logger.warning("rest quote fetch failed for %s: %s", sym, exc)
            return None


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
