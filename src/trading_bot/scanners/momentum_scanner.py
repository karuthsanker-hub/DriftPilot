from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_bot.data.market_data import MarketDataProvider, MomentumFundamentals
from trading_bot.data.repositories import MomentumScoreRecord, TradingRepository
from trading_bot.strategies.momentum import MomentumInput, MomentumScore, score_momentum


@dataclass(frozen=True)
class MomentumScanResult:
    ticker: str
    score: MomentumScore | None
    persisted: bool
    skip_reason: str | None = None


class MomentumScanner:
    def __init__(self, market_data: MarketDataProvider, repository: TradingRepository | None = None) -> None:
        self.market_data = market_data
        self.repository = repository

    def scan(self, tickers: list[str], scan_date: date, *, min_score: int = 4) -> list[MomentumScanResult]:
        results = [self.scan_one(ticker, scan_date, min_score=min_score) for ticker in _clean_tickers(tickers)]
        return sorted(results, key=lambda result: (result.score.total_score if result.score else -1, result.ticker), reverse=True)

    def scan_one(self, ticker: str, scan_date: date, *, min_score: int = 4) -> MomentumScanResult:
        ticker = ticker.upper()
        try:
            history = self.market_data.daily_history(ticker, period="1y")
            if len(history) < 127:
                return MomentumScanResult(ticker=ticker, score=None, persisted=False, skip_reason="not enough price history")

            skip_reason = None
            try:
                fundamentals = self.market_data.momentum_fundamentals(ticker)
            except Exception as exc:
                fundamentals = MomentumFundamentals(
                    ticker=ticker,
                    earnings_surprises_pct=[],
                    roe=0.0,
                    debt_to_equity=99.0,
                    profit_margin=0.0,
                )
                skip_reason = f"fundamentals unavailable; price-only score used: {exc}"
            surprises = fundamentals.earnings_surprises_pct[:4]
            if len(surprises) < 4:
                suffix = f"earnings surprise history incomplete; found {len(surprises)}"
                skip_reason = f"{skip_reason}; {suffix}" if skip_reason else suffix
                surprises = surprises + [0.0] * (4 - len(surprises))
            score = score_momentum(
                MomentumInput(
                    ticker=ticker,
                    current_close=float(history["close"].iloc[-1]),
                    close_63d_ago=float(history["close"].iloc[-64]),
                    close_126d_ago=float(history["close"].iloc[-127]),
                    earnings_surprises_pct=surprises,
                    roe=fundamentals.roe,
                    debt_to_equity=fundamentals.debt_to_equity,
                    profit_margin=fundamentals.profit_margin,
                )
            )
            persisted = self._persist(score, scan_date) if score.total_score >= min_score else False
            return MomentumScanResult(ticker=ticker, score=score, persisted=persisted, skip_reason=skip_reason)
        except Exception as exc:
            return MomentumScanResult(ticker=ticker, score=None, persisted=False, skip_reason=str(exc))

    def _persist(self, score: MomentumScore, scan_date: date) -> bool:
        if self.repository is None:
            return False
        self.repository.insert_momentum_score(
            MomentumScoreRecord(
                ticker=score.ticker,
                scan_date=scan_date,
                total_score=score.total_score,
                price_momentum=score.price_momentum,
                earnings_momentum=score.earnings_momentum,
                quality_score=score.quality_score,
            )
        )
        return True


def _clean_tickers(tickers: list[str]) -> list[str]:
    return sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
