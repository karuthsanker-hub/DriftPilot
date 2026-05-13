"""Adapter that computes PositionSnapshot market-data fields.

Plugs into the state-machine bridge to replace placeholder zeros with real
data from the AlpacaSIPStream (live) or from cached bar/quote data (paper).

All methods are best-effort — missing data returns sensible defaults rather
than blocking the agent tick.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from driftpilot.market_data.alpaca_stream import MarketBar, MarketQuote

logger = logging.getLogger(__name__)


class BarProvider(Protocol):
    """Anything that can return session bars and latest quotes."""

    def session_bars(self, symbol: str) -> list[MarketBar]: ...
    def latest_bar(self, symbol: str) -> MarketBar | None: ...
    def latest_quote(self, symbol: str) -> MarketQuote | None: ...


@dataclass(frozen=True, slots=True)
class MarketDataFields:
    """Computed market-data fields for PositionSnapshot."""

    last_10_closes: list[float]
    last_10_volumes: list[int]
    recent_vol: float  # price volatility of last 10 bars
    avg_vol: int  # average volume over session
    rvol: float  # relative volume ratio
    consolidation_bars: int
    spy_move_pct: float
    sector_move_pct: float
    vix: float
    new_headlines: str
    current_price: float | None  # latest price from quote/bar


class MarketDataAdapter:
    """Computes derived market fields from a BarProvider.

    Usage:
        adapter = MarketDataAdapter(stream, catalyst_db_path="data/catalyst.db")
        fields = adapter.compute(symbol="AAPL", sector="Technology", entry_time=...)
    """

    def __init__(
        self,
        bar_provider: BarProvider | None = None,
        catalyst_db_path: str | None = None,
        vix_value: float = 0.0,
    ) -> None:
        self._bars = bar_provider
        self._catalyst_db = catalyst_db_path
        self._vix = vix_value

    def set_vix(self, value: float) -> None:
        """Update the cached VIX value (from macro provider or external source)."""
        self._vix = value

    def compute(
        self,
        symbol: str,
        sector: str = "",
        entry_time: datetime | None = None,
    ) -> MarketDataFields:
        """Compute all market-data fields for a symbol."""
        bars = self._get_bars(symbol)
        spy_bars = self._get_bars("SPY")

        closes = [b.close for b in bars]
        volumes = [int(b.volume) for b in bars]

        last_10_closes = closes[-10:] if closes else []
        last_10_volumes = volumes[-10:] if volumes else []

        recent_vol = _std_dev(last_10_closes) if len(last_10_closes) >= 2 else 0.0
        avg_vol = int(sum(volumes) / len(volumes)) if volumes else 0
        recent_avg_vol = int(sum(last_10_volumes) / len(last_10_volumes)) if last_10_volumes else 0
        rvol = recent_avg_vol / max(avg_vol, 1)

        consolidation_bars = _count_consolidation(last_10_closes)

        spy_move_pct = _session_return_pct(spy_bars)
        sector_move_pct = self._sector_move(sector, spy_bars)

        current_price = self._latest_price(symbol)
        new_headlines = self._recent_headlines(symbol, entry_time)

        return MarketDataFields(
            last_10_closes=last_10_closes,
            last_10_volumes=last_10_volumes,
            recent_vol=recent_vol,
            avg_vol=avg_vol,
            rvol=rvol,
            consolidation_bars=consolidation_bars,
            spy_move_pct=spy_move_pct,
            sector_move_pct=sector_move_pct,
            vix=self._vix,
            new_headlines=new_headlines,
            current_price=current_price,
        )

    def _get_bars(self, symbol: str) -> list[Any]:
        if self._bars is None:
            return []
        try:
            return self._bars.session_bars(symbol)
        except Exception:
            return []

    def _latest_price(self, symbol: str) -> float | None:
        if self._bars is None:
            return None
        try:
            quote = self._bars.latest_quote(symbol)
            if quote and quote.bid_price > 0:
                return (quote.bid_price + quote.ask_price) / 2.0
            bar = self._bars.latest_bar(symbol)
            if bar:
                return bar.close
        except Exception:
            pass
        return None

    def _sector_move(self, sector: str, spy_bars: list[Any]) -> float:
        """Approximate sector move from SPY if no sector ETF bars available."""
        # In a full implementation, we'd have sector ETF bars (XLK, XLF, etc.)
        # For now, use SPY as a proxy.
        return _session_return_pct(spy_bars)

    def _recent_headlines(
        self, symbol: str, since: datetime | None = None
    ) -> str:
        """Pull recent headlines from catalyst DB."""
        if not self._catalyst_db:
            return ""
        since = since or datetime.now(timezone.utc)
        try:
            conn = sqlite3.connect(self._catalyst_db)
            try:
                rows = conn.execute(
                    "SELECT headline FROM catalyst_events "
                    "WHERE symbol = ? AND event_ts >= ? "
                    "ORDER BY event_ts DESC LIMIT 5",
                    (symbol.upper(), since.isoformat()),
                ).fetchall()
                return "\n".join(r[0] for r in rows) if rows else ""
            finally:
                conn.close()
        except Exception:
            return ""


# ── Pure helper functions ────────────────────────────────────────────────


def _std_dev(values: list[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _session_return_pct(bars: list[Any]) -> float:
    """Intraday return from first bar open to last bar close."""
    if len(bars) < 2:
        return 0.0
    first_open = bars[0].open
    last_close = bars[-1].close
    if first_open <= 0:
        return 0.0
    return ((last_close - first_open) / first_open) * 100.0


def _count_consolidation(closes: list[float]) -> int:
    """Count trailing bars where price change is < 0.1%."""
    if len(closes) < 2:
        return 0
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        pct_change = abs(closes[i] - closes[i - 1]) / max(closes[i - 1], 0.01)
        if pct_change < 0.001:
            count += 1
        else:
            break
    return count
