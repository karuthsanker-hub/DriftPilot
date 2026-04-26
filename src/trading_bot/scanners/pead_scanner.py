from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_bot.data.market_data import MarketDataProvider
from trading_bot.data.repositories import TradingRepository, WatchlistRecord
from trading_bot.sentiment import SentimentScorer
from trading_bot.strategies.indicators import average_volume, ema
from trading_bot.strategies.pead import PEADAction, PEADInput, PEADSignal, evaluate_pead_signal


@dataclass(frozen=True)
class PEADScanResult:
    ticker: str
    signal: PEADSignal
    persisted: bool


class PEADScanner:
    def __init__(
        self,
        market_data: MarketDataProvider,
        sentiment: SentimentScorer,
        repository: TradingRepository | None = None,
    ) -> None:
        self.market_data = market_data
        self.sentiment = sentiment
        self.repository = repository

    def scan(self, tickers: list[str], scan_date: date, *, persist_skips: bool = False) -> list[PEADScanResult]:
        results: list[PEADScanResult] = []
        for ticker in _clean_tickers(tickers):
            try:
                result = self.scan_one(ticker, scan_date, persist_skips=persist_skips)
            except Exception as exc:
                signal = PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=0, skip_reason=str(exc))
                result = PEADScanResult(ticker=ticker, signal=signal, persisted=False)
            results.append(result)
        return results

    def scan_one(self, ticker: str, scan_date: date, *, persist_skips: bool = False) -> PEADScanResult:
        profile = self.market_data.company_profile(ticker)
        history = self.market_data.daily_history(ticker)
        event = self.market_data.latest_earnings_event(ticker, scan_date)
        if event is None:
            signal = PEADSignal(ticker=ticker.upper(), action=PEADAction.SKIP, surprise_pct=0, skip_reason="no recent earnings event")
            return PEADScanResult(ticker=ticker.upper(), signal=signal, persisted=self._persist(signal, None, profile, persist_skips))

        latest_price = profile.current_price or float(history["close"].iloc[-1])
        avg_vol = profile.avg_volume or average_volume(history)
        ema50 = float(ema(history["close"], 50).iloc[-1])
        earnings_volume = float(history["volume"].iloc[-1])
        sentiment = self.sentiment.classify(event.text)
        signal = evaluate_pead_signal(
            PEADInput(
                ticker=ticker,
                actual_eps=event.actual_eps,
                estimate_eps=event.estimate_eps,
                sentiment=sentiment,
                analyst_count=profile.analyst_count,
                market_cap_m=profile.market_cap_m,
                price=latest_price,
                ema50=ema50,
                earnings_day_volume=earnings_volume,
                avg_volume_20d=avg_vol,
                is_shortable=profile.shortable,
            )
        )
        return PEADScanResult(ticker=ticker.upper(), signal=signal, persisted=self._persist(signal, event.earnings_date, profile, persist_skips))

    def _persist(self, signal: PEADSignal, earnings_date: date | None, profile, persist_skips: bool) -> bool:
        if self.repository is None:
            return False
        if signal.action == PEADAction.SKIP and not persist_skips:
            return False
        strategy = "PEAD_LONG" if signal.action == PEADAction.BUY_NEXT_DAY else "PEAD_SHORT"
        if signal.action == PEADAction.SKIP:
            strategy = "PEAD_LONG"
        self.repository.insert_watchlist_candidate(
            WatchlistRecord(
                ticker=signal.ticker,
                strategy=strategy,
                earnings_date=earnings_date,
                surprise_pct=signal.surprise_pct,
                analyst_count=profile.analyst_count,
                market_cap_m=profile.market_cap_m,
                status="pending" if signal.action != PEADAction.SKIP else "skipped",
                skip_reason=signal.skip_reason or None,
            )
        )
        return True


def _clean_tickers(tickers: list[str]) -> list[str]:
    return sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})

