"""Context assembly for Qwen enrichment v2.

The assembler is deliberately best-effort. Missing API keys, absent parquet
bars, or unavailable external providers produce ``None`` fields rather than
blocking enrichment.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from driftpilot.catalyst.headline_parser import HeadlineParsed, parse_headline
from driftpilot.clock import DriftPilotClock, require_aware

logger = logging.getLogger(__name__)

SECTOR_ETF_BY_SECTOR: dict[str, str] = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


class MarketDataProvider(Protocol):
    def company_profile(self, ticker: str) -> Any: ...

    def momentum_fundamentals(self, ticker: str) -> Any: ...

    def spy_premarket_change_pct(self) -> float | None: ...


class MacroProvider(Protocol):
    def current_vix(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class EnrichmentContext:
    market_cap_m: float | None = None
    avg_volume: int | None = None
    sector: str | None = None
    atr_pct: float | None = None
    eps_beat_pct: float | None = None
    revenue_beat_pct: float | None = None
    guidance_direction: str | None = None
    last_4_surprises: list[float] | None = None
    headline_cluster_count: int = 0
    minutes_to_open: int | None = None
    spy_change_pct: float | None = None
    vix: float | None = None
    sector_etf_5d_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> "EnrichmentContext | None":
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("context_json must decode to object")
        return cls(**data)

    def to_prompt_block(self) -> str:
        surprises = self.last_4_surprises or []
        surprises_text = ", ".join(f"{item:+.1f}%" for item in surprises) if surprises else "unknown"
        lines = [
            f"Market cap: {_fmt_millions(self.market_cap_m)}",
            f"Average volume: {_fmt_int(self.avg_volume)}",
            f"Sector: {self.sector or 'unknown'}",
            f"20-day ATR: {_fmt_pct(self.atr_pct)}",
            f"EPS beat/miss: {_fmt_pct(self.eps_beat_pct)}",
            f"Revenue beat/miss: {_fmt_pct(self.revenue_beat_pct)}",
            f"Guidance direction: {self.guidance_direction or 'unknown'}",
            f"Last 4 earnings surprises: {surprises_text}",
            f"Prior same-symbol headlines in last 30m: {self.headline_cluster_count}",
            f"Minutes to market open: {self.minutes_to_open if self.minutes_to_open is not None else 'market open/unknown'}",
            f"SPY change: {_fmt_pct(self.spy_change_pct)}",
            f"VIX: {_fmt_float(self.vix)}",
            f"Sector ETF 5d return: {_fmt_pct(self.sector_etf_5d_pct)}",
        ]
        return "\n".join(f"- {line}" for line in lines)


@dataclass(frozen=True, slots=True)
class _SymbolContext:
    market_cap_m: float | None = None
    avg_volume: int | None = None
    sector: str | None = None
    atr_pct: float | None = None
    last_4_surprises: list[float] | None = None


@dataclass(frozen=True, slots=True)
class _RunContext:
    spy_change_pct: float | None = None
    vix: float | None = None
    sector_etf_5d_pct_by_etf: dict[str, float] | None = None


class ContextAssembler:
    def __init__(
        self,
        *,
        db_path: str | None = None,
        universe_csv_path: str | Path = "config/universe.csv",
        bar_root: str | Path = "data/bars/databento",
        market_data_provider: MarketDataProvider | None = None,
        macro_provider: MacroProvider | None = None,
        sector_etf_5d_pct_by_etf: dict[str, float] | None = None,
        clock: DriftPilotClock | None = None,
        enable_external_fetch: bool = False,
    ) -> None:
        self._db_path = db_path
        self._universe_csv_path = Path(universe_csv_path)
        self._bar_root = Path(bar_root)
        self._market_data_provider = market_data_provider
        self._macro_provider = macro_provider
        self._clock = clock or DriftPilotClock()
        self._sector_by_symbol = _load_sector_map(self._universe_csv_path)
        self._symbol_cache: dict[str, _SymbolContext] = {}
        self._run_context = _RunContext(sector_etf_5d_pct_by_etf=sector_etf_5d_pct_by_etf or {})
        self._enable_external_fetch = enable_external_fetch
        self._allow_sector_fetch = enable_external_fetch and sector_etf_5d_pct_by_etf is None

    def cache_run_context(self) -> None:
        spy_change_pct = _safe_call(lambda: self._market_data_provider.spy_premarket_change_pct()) if self._market_data_provider else None
        vix = _safe_call(lambda: self._macro_provider.current_vix()) if self._macro_provider else None
        sector_returns = dict(self._run_context.sector_etf_5d_pct_by_etf or {})
        for etf in set(SECTOR_ETF_BY_SECTOR.values()):
            if self._allow_sector_fetch and etf not in sector_returns:
                fetched_return = _fetch_yfinance_5d_return(etf)
                if fetched_return is not None:
                    sector_returns[etf] = fetched_return
        self._run_context = _RunContext(
            spy_change_pct=_as_float(spy_change_pct),
            vix=_as_float(vix),
            sector_etf_5d_pct_by_etf=sector_returns,
        )

    def cache_symbol_context(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self._symbol_cache:
            return

        sector = self._sector_by_symbol.get(symbol)
        market_cap_m: float | None = None
        avg_volume: int | None = None
        last_4_surprises: list[float] = []

        if self._market_data_provider is not None:
            profile = _safe_call(lambda: self._market_data_provider.company_profile(symbol))
            market_cap_m = _as_float(_attr_or_key(profile, "market_cap_m"))
            avg_volume_float = _as_float(_attr_or_key(profile, "avg_volume"))
            avg_volume = int(avg_volume_float) if avg_volume_float is not None else None
            fundamentals = _safe_call(lambda: self._market_data_provider.momentum_fundamentals(symbol))
            surprises = _attr_or_key(fundamentals, "earnings_surprises_pct")
            if isinstance(surprises, list):
                parsed_surprises: list[float] = []
                for item in surprises[:4]:
                    parsed = _as_float(item)
                    if parsed is not None:
                        parsed_surprises.append(parsed)
                last_4_surprises = parsed_surprises
        elif self._enable_external_fetch:
            market_cap_m, avg_volume = _fetch_yfinance_profile(symbol)

        self._symbol_cache[symbol] = _SymbolContext(
            market_cap_m=market_cap_m,
            avg_volume=avg_volume,
            sector=sector,
            atr_pct=_compute_atr_pct(self._bar_root, symbol),
            last_4_surprises=last_4_surprises,
        )

    def build_context(
        self,
        symbol: str,
        headline: str,
        event_ts: datetime,
        category: str,
        subcategory: str,
        *,
        parsed: HeadlineParsed | None = None,
    ) -> EnrichmentContext:
        del category, subcategory
        event_ts = require_aware(event_ts)
        symbol = symbol.upper()
        self.cache_symbol_context(symbol)
        symbol_ctx = self._symbol_cache[symbol]
        parsed = parsed or parse_headline(headline)
        sector_etf = SECTOR_ETF_BY_SECTOR.get(symbol_ctx.sector or "")
        sector_returns = self._run_context.sector_etf_5d_pct_by_etf or {}
        return EnrichmentContext(
            market_cap_m=symbol_ctx.market_cap_m,
            avg_volume=symbol_ctx.avg_volume,
            sector=symbol_ctx.sector,
            atr_pct=symbol_ctx.atr_pct,
            eps_beat_pct=parsed.eps_beat_pct,
            revenue_beat_pct=parsed.revenue_beat_pct,
            guidance_direction=parsed.guidance_direction,
            last_4_surprises=symbol_ctx.last_4_surprises or [],
            headline_cluster_count=self._headline_cluster_count(symbol, event_ts),
            minutes_to_open=self._minutes_to_open(event_ts),
            spy_change_pct=self._run_context.spy_change_pct,
            vix=self._run_context.vix,
            sector_etf_5d_pct=sector_returns.get(sector_etf or ""),
        )

    def _headline_cluster_count(self, symbol: str, event_ts: datetime) -> int:
        if not self._db_path:
            return 0
        start = (event_ts - timedelta(minutes=30)).isoformat()
        end = event_ts.isoformat()
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM catalyst_events "
                    "WHERE symbol = ? AND event_ts >= ? AND event_ts < ?",
                    (symbol, start, end),
                )
                return int(cur.fetchone()[0] or 0)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("context cluster count failed for %s: %s", symbol, exc)
            return 0

    def _minutes_to_open(self, event_ts: datetime) -> int | None:
        et = self._clock.to_et(event_ts)
        market_open = datetime.combine(et.date(), time(9, 30), tzinfo=self._clock.timezone)
        market_close = datetime.combine(et.date(), time(16, 0), tzinfo=self._clock.timezone)
        if market_open <= et <= market_close:
            return None
        if et < market_open:
            return max(0, int((market_open - et).total_seconds() // 60))
        next_open = market_open + timedelta(days=1)
        return max(0, int((next_open - et).total_seconds() // 60))


def _load_sector_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for index, line in enumerate(path.read_text().splitlines()):
        if index == 0:
            continue
        parts = line.split(",")
        if len(parts) >= 3 and parts[0].strip():
            out[parts[0].strip().upper()] = parts[2].strip() or "Unknown"
    return out


def _compute_atr_pct(bar_root: Path, symbol: str, period: int = 20) -> float | None:
    try:
        import pandas as pd  # type: ignore[import-untyped]

        files = sorted((bar_root / symbol.upper()).glob("*.parquet"))
        if not files:
            return None
        df = pd.read_parquet(files[-1], columns=["high", "low", "close"]).tail(period + 1)
        if len(df) < period + 1:
            return None
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1).dropna()
        if len(tr) < period:
            return None
        atr = float(tr.tail(period).mean())
        last_close = float(df["close"].iloc[-1])
        return (atr / last_close * 100.0) if last_close else None
    except Exception as exc:
        logger.debug("ATR context unavailable for %s: %s", symbol, exc)
        return None


def _fetch_yfinance_profile(symbol: str) -> tuple[float | None, int | None]:
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        info = yf.Ticker(symbol).info or {}
        market_cap = _as_float(info.get("marketCap"))
        avg_volume = _as_float(info.get("averageVolume") or info.get("averageDailyVolume10Day"))
        return (
            market_cap / 1_000_000 if market_cap is not None else None,
            int(avg_volume) if avg_volume is not None else None,
        )
    except Exception as exc:
        logger.debug("yfinance profile unavailable for %s: %s", symbol, exc)
        return None, None


def _fetch_yfinance_5d_return(symbol: str) -> float | None:
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        history = yf.download(symbol, period="10d", interval="1d", progress=False, auto_adjust=False)
        if history.empty:
            return None
        close = history["Close"] if "Close" in history else history["close"]
        close = close.dropna()
        if len(close) < 2:
            return None
        start = float(close.iloc[max(0, len(close) - 6)])
        end = float(close.iloc[-1])
        return (end / start - 1.0) * 100.0 if start else None
    except Exception as exc:
        logger.debug("yfinance return unavailable for %s: %s", symbol, exc)
        return None


def _safe_call(fn):
    try:
        return fn()
    except Exception as exc:
        logger.debug("context provider call failed: %s", exc)
        return None


def _attr_or_key(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_millions(value: float | None) -> str:
    return "unknown" if value is None else f"${value:,.0f}M"


def _fmt_int(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _fmt_pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value:+.2f}%"


def _fmt_float(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}"


__all__ = ["ContextAssembler", "EnrichmentContext", "SECTOR_ETF_BY_SECTOR"]
