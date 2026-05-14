"""volume_spike_v1 — volume anomaly detection signal.

Polls Alpaca stock snapshots for the universe and emits candidates
when a stock's intraday volume is abnormally high relative to its
EXPECTED volume at this time of day. No news required — pure volume.

Thesis: unusual volume precedes or accompanies directional moves.
Smart money moves before the headline. A stock trading at 3x its
expected pace at 10 AM is doing something — we want to be in it.

Key metric: **RVOL (relative volume)** = today_volume / expected_volume.
Expected volume = avg_daily_volume × elapsed_fraction_of_trading_day.
At 10:00 AM (30 min into session), expected = avg × 0.077 (session is
390 min). If actual volume is 3× that, RVOL = 3.0. This normalizes
across all times of day.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time, timezone
from typing import Any

from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.volume_spike_v1.config import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    VolumeSpikeConfig,
)

# US market session: 9:30 AM - 4:00 PM ET = 390 minutes
_MARKET_OPEN = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)
_SESSION_MINUTES = 390.0


def _elapsed_fraction() -> float:
    """Fraction of the trading day elapsed (0.0 to 1.0).

    Pre-market returns a small positive number (~0.02) so we don't
    divide by zero. After-hours returns 1.0.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    t = now_et.time()
    if t < _MARKET_OPEN:
        # Pre-market: use a minimum so RVOL still works
        return 0.02
    if t >= _MARKET_CLOSE:
        return 1.0
    minutes_since_open = (
        (t.hour * 60 + t.minute) - (_MARKET_OPEN.hour * 60 + _MARKET_OPEN.minute)
    )
    return max(0.02, minutes_since_open / _SESSION_MINUTES)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VolumeSpikeV1Signal:
    """Scans for unusual intraday volume across the universe.

    On each scan():
    1. Fetch Alpaca snapshots (batched, one API call for all symbols).
    2. Compare today's cumulative volume to cached avg_volume.
    3. Filter by volume ratio, price change, and minimum thresholds.
    4. Emit candidates scored by volume_ratio × |price_change|.
    """

    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def __init__(
        self,
        config: VolumeSpikeConfig | None = None,
        *,
        api_key: str = "",
        api_secret: str = "",
        symbols: list[str] | None = None,
        clock: Any = None,
    ) -> None:
        self.config = config or VolumeSpikeConfig()
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = [s.upper() for s in (symbols or [])]
        self._clock = clock or _utcnow

        # Caches
        self._avg_volume_cache: dict[str, int] = {}  # symbol → avg daily volume
        self._snapshot_cache: dict[str, Any] = {}     # symbol → snapshot object
        self._snapshot_cache_ts: float = 0.0
        self._avg_volume_loaded: bool = False

    def _load_avg_volumes(self) -> None:
        """Load average daily volumes from yfinance (one-time, cached)."""
        if self._avg_volume_loaded:
            return
        self._avg_volume_loaded = True

        try:
            import yfinance as yf  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("yfinance not installed — volume_spike_v1 cannot compute ratios")
            return

        # Batch fetch in chunks of 50 to avoid overloading yfinance
        for i in range(0, len(self._symbols), 50):
            chunk = self._symbols[i:i + 50]
            for sym in chunk:
                try:
                    info = yf.Ticker(sym).info or {}
                    avg_vol = info.get("averageVolume") or info.get("averageDailyVolume10Day")
                    if avg_vol:
                        self._avg_volume_cache[sym] = int(avg_vol)
                except Exception:
                    pass
        logger.info(
            "[SIGNAL:volume_spike] loaded avg_volume for %d/%d symbols",
            len(self._avg_volume_cache), len(self._symbols),
        )

    def _fetch_snapshots(self) -> dict[str, Any]:
        """Fetch latest snapshots from Alpaca (with TTL cache)."""
        now_t = time.monotonic()
        if now_t - self._snapshot_cache_ts < self.config.snapshot_cache_ttl_s:
            return self._snapshot_cache

        try:
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from alpaca.data.requests import StockSnapshotRequest

            client = StockHistoricalDataClient(
                api_key=self._api_key,
                secret_key=self._api_secret,
            )
            # Alpaca allows up to ~200 symbols per snapshot request
            all_snaps: dict[str, Any] = {}
            for i in range(0, len(self._symbols), 200):
                chunk = self._symbols[i:i + 200]
                req = StockSnapshotRequest(symbol_or_symbols=chunk)
                result = client.get_stock_snapshot(req)
                if isinstance(result, dict):
                    all_snaps.update(result)
            self._snapshot_cache = all_snaps
            self._snapshot_cache_ts = now_t
            return all_snaps
        except Exception as exc:
            logger.warning("[SIGNAL:volume_spike] snapshot fetch failed: %s", exc)
            return self._snapshot_cache  # return stale cache

    def scan(self, *args: Any, **kwargs: Any) -> list[Candidate]:
        """Return candidates for stocks with abnormal relative volume (RVOL).

        RVOL = today_volume / (avg_daily_volume × elapsed_fraction).
        An RVOL of 3.0 means the stock is trading at 3× its normal pace
        for this time of day.

        Each candidate is scored by RVOL × |price_change_pct|.
        Higher score = stronger volume anomaly with confirmed direction.
        """
        # Lazy-load average volumes on first scan
        self._load_avg_volumes()

        if not self._avg_volume_cache:
            return []

        snapshots = self._fetch_snapshots()
        if not snapshots:
            return []

        cfg = self.config
        elapsed = _elapsed_fraction()
        candidates: list[Candidate] = []

        for sym, snap in snapshots.items():
            try:
                daily = snap.daily_bar
                if daily is None:
                    continue

                today_vol = float(daily.volume or 0)
                avg_vol = self._avg_volume_cache.get(sym, 0)
                if avg_vol <= 0:
                    continue

                # RVOL: normalize by time of day
                expected_vol = avg_vol * elapsed
                rvol = today_vol / expected_vol if expected_vol > 0 else 0
                if rvol < cfg.min_volume_ratio:
                    continue

                # Absolute volume floor
                if today_vol < cfg.min_volume_abs:
                    continue

                # Price filters
                price = float(daily.close or 0)
                open_price = float(daily.open or 0)
                if price < cfg.min_price:
                    continue
                if cfg.max_price > 0 and price > cfg.max_price:
                    continue

                # Intraday price change
                if open_price <= 0:
                    continue
                price_change_pct = ((price - open_price) / open_price) * 100.0
                if abs(price_change_pct) < cfg.min_price_change_pct:
                    continue  # volume but no movement = chop

                # Direction: positive change = bullish volume, negative = bearish
                # We only take bullish (positive momentum) for now
                if price_change_pct <= 0:
                    continue  # skip bearish volume spikes

                # Score: RVOL strength × price movement
                score = rvol * abs(price_change_pct)

                candidates.append(
                    Candidate(
                        symbol=sym,
                        score=round(score, 3),
                        sector="",  # filled by allocator
                        allowed=True,
                        blocked_reason=None,
                        features={
                            "rvol": round(rvol, 2),
                            "today_volume": int(today_vol),
                            "avg_volume": avg_vol,
                            "expected_volume": int(expected_vol),
                            "elapsed_pct": round(elapsed * 100, 1),
                            "price_change_pct": round(price_change_pct, 2),
                            "price": round(price, 2),
                            "vwap": round(float(daily.vwap or 0), 2),
                            "signal_type": "volume_spike",
                        },
                    )
                )
            except Exception as exc:
                logger.debug("volume_spike skip %s: %s", sym, exc)
                continue

        # Sort by score descending, cap at max_candidates
        candidates.sort(key=lambda c: -c.score)
        candidates = candidates[:cfg.max_candidates]

        if candidates:
            logger.info(
                "[SIGNAL:volume_spike] %d candidates (elapsed=%.0f%% top: %s RVOL=%.1fx change=%+.1f%%)",
                len(candidates),
                elapsed * 100,
                candidates[0].symbol,
                candidates[0].features.get("rvol", 0),
                candidates[0].features.get("price_change_pct", 0),
            )
        return candidates

    def evaluate_exit(
        self,
        position: Any,
        latest_bar: Any | None = None,
        settings: Any | None = None,
    ) -> ExitDecision | None:
        """Simple time + P&L exit logic for volume spike positions."""
        now = self._clock() if callable(self._clock) else self._clock
        metadata = getattr(position, "metadata", {}) or {}

        entry_ts = (
            getattr(position, "entry_ts", None)
            or getattr(position, "entry_at", None)
            or metadata.get("entry_ts")
            or metadata.get("entry_at")
        )
        if entry_ts is None:
            return None

        if isinstance(entry_ts, str):
            from datetime import datetime
            try:
                entry_ts = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
            except ValueError:
                return None

        # Time exit
        hold_minutes = (now - entry_ts).total_seconds() / 60.0
        if hold_minutes >= self.config.max_hold_minutes:
            return ExitDecision(
                should_exit=True,
                exit_reason="TIME",
                metadata={"hold_minutes": round(hold_minutes, 1)},
            )

        # P&L exits
        unrealized_pct = getattr(position, "unrealized_pct", None)
        if unrealized_pct is None:
            entry_price = float(
                getattr(position, "entry_price", None)
                or metadata.get("entry_price")
                or 0.0
            )
            current_price = float(
                getattr(position, "current_price", None)
                or metadata.get("current_price")
                or (getattr(latest_bar, "close", entry_price) if latest_bar else entry_price)
                or entry_price
            )
            if entry_price > 0:
                unrealized_pct = ((current_price - entry_price) / entry_price) * 100.0
            else:
                unrealized_pct = 0.0

        if unrealized_pct >= self.config.profit_take_pct:
            return ExitDecision(
                should_exit=True,
                exit_reason="TARGET",
                metadata={"unrealized_pct": round(unrealized_pct, 3)},
            )
        if unrealized_pct <= -self.config.stop_loss_pct:
            return ExitDecision(
                should_exit=True,
                exit_reason="STOP",
                metadata={"unrealized_pct": round(unrealized_pct, 3)},
            )

        return None

    def bootstrap_from_db(self, db_path: str, lookback_minutes: int | None = None) -> int:
        """No-op — volume spike has no persistent events to bootstrap."""
        return 0


__all__ = ["VolumeSpikeV1Signal"]
