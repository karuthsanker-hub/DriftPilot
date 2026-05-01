from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from trading_bot.data.provider_factory import create_market_data_provider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository, WatchlistRecord
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.data.macro_data import FredMacroDataProvider
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.strategies.risk import evaluate_daily_pause
from trading_bot.scanners.momentum_scanner import MomentumScanner
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.sentiment import FinBERTSentimentScorer, KeywordSentimentScorer
from trading_bot.settings import AppSettings, load_settings
from trading_bot.universe import load_pead_universe


def create_scheduler(*, timezone: str = "America/New_York") -> BackgroundScheduler:
    return BackgroundScheduler(timezone=timezone)


def register_jobs(
    scheduler: BackgroundScheduler,
    *,
    pead_scan_job,
    pending_entry_job,
    position_management_job,
    momentum_scan_job,
    operator_candidate_refresh_job,
    operator_universe_refresh_job,
    realtime_entry_monitor_job,
    realtime_exit_monitor_job,
    operator_refresh_interval_minutes: int = 5,
    operator_universe_refresh_interval_minutes: int = 5,
    operator_monitor_interval_minutes: int = 5,
) -> None:
    scheduler.add_job(pead_scan_job, CronTrigger(day_of_week="mon-fri", hour=16, minute=30), id="daily_pead_scan", replace_existing=True)
    scheduler.add_job(pending_entry_job, CronTrigger(day_of_week="mon-fri", hour=9, minute=25), id="pending_entry_scan", replace_existing=True)
    scheduler.add_job(position_management_job, CronTrigger(day_of_week="mon-fri", hour=15, minute=50), id="position_management", replace_existing=True)
    scheduler.add_job(momentum_scan_job, CronTrigger(day_of_week="mon", hour=6, minute=0), id="weekly_momentum_scan", replace_existing=True)
    scheduler.add_job(
        operator_universe_refresh_job,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{operator_universe_refresh_interval_minutes}"),
        id="operator_universe_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        realtime_entry_monitor_job,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{operator_monitor_interval_minutes}"),
        id="realtime_entry_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        realtime_exit_monitor_job,
        CronTrigger(day_of_week="mon-fri", hour="9-16", minute=f"*/{operator_monitor_interval_minutes}"),
        id="realtime_exit_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        operator_candidate_refresh_job,
        IntervalTrigger(minutes=operator_refresh_interval_minutes),
        id="operator_candidate_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


@dataclass
class SchedulerState:
    running: bool = False
    last_runs: list[dict[str, Any]] = field(default_factory=list)
    operator_universe_cursor: int = 0

    def record(self, job: str, ok: bool, payload: Any) -> None:
        self.last_runs.insert(
            0,
            {
                "job": job,
                "ok": ok,
                "ran_at": datetime.now(UTC).isoformat(),
                "payload": payload,
            },
        )
        self.last_runs = self.last_runs[:20]


class TradingSchedulerService:
    def __init__(self, *, env_path: str = ".env") -> None:
        self.env_path = env_path
        self.state = SchedulerState()
        self.scheduler = create_scheduler()
        settings = load_settings(self.env_path)
        register_jobs(
            self.scheduler,
            pead_scan_job=self.run_pead_scan,
            pending_entry_job=self.run_pending_entries,
            position_management_job=self.run_position_management,
            momentum_scan_job=self.run_momentum_scan,
            operator_candidate_refresh_job=self.run_operator_candidate_refresh,
            operator_universe_refresh_job=self.run_operator_universe_refresh,
            realtime_entry_monitor_job=self.run_realtime_entry_monitor,
            realtime_exit_monitor_job=self.run_realtime_exit_monitor,
            operator_refresh_interval_minutes=settings.operator_refresh_interval_minutes,
            operator_universe_refresh_interval_minutes=settings.operator_universe_refresh_interval_minutes,
            operator_monitor_interval_minutes=settings.operator_monitor_interval_minutes,
        )

    def start(self) -> SchedulerState:
        if not self.scheduler.running:
            self.scheduler.start()
        self.state.running = True
        return self.state

    def stop(self) -> SchedulerState:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.state.running = False
        self.scheduler = create_scheduler()
        settings = load_settings(self.env_path)
        register_jobs(
            self.scheduler,
            pead_scan_job=self.run_pead_scan,
            pending_entry_job=self.run_pending_entries,
            position_management_job=self.run_position_management,
            momentum_scan_job=self.run_momentum_scan,
            operator_candidate_refresh_job=self.run_operator_candidate_refresh,
            operator_universe_refresh_job=self.run_operator_universe_refresh,
            realtime_entry_monitor_job=self.run_realtime_entry_monitor,
            realtime_exit_monitor_job=self.run_realtime_exit_monitor,
            operator_refresh_interval_minutes=settings.operator_refresh_interval_minutes,
            operator_universe_refresh_interval_minutes=settings.operator_universe_refresh_interval_minutes,
            operator_monitor_interval_minutes=settings.operator_monitor_interval_minutes,
        )
        return self.state

    def status(self) -> dict[str, Any]:
        return {
            "running": self.scheduler.running,
            "jobs": [
                {
                    "id": job.id,
                    "next_run_time": job.next_run_time.isoformat() if getattr(job, "next_run_time", None) else None,
                }
                for job in self.scheduler.get_jobs()
            ],
            "last_runs": self.state.last_runs,
        }

    def run_pead_scan(self) -> list[dict[str, Any]]:
        try:
            settings = load_settings(self.env_path)
            tickers = load_pead_universe(settings, env_path=self.env_path)
            if not tickers:
                payload = {"status": "no_universe", "message": "No PEAD universe configured."}
                self.state.record("daily_pead_scan", False, payload)
                return [payload]
            repo = TradingRepository(create_supabase_client(settings))
            scanner = PEADScanner(
                create_market_data_provider(settings, env_path=self.env_path),
                _sentiment_scorer(settings),
                repo,
                portfolio_value=settings.paper_portfolio_value,
                risk_pct=settings.risk_per_trade_pct,
                max_position_pct=settings.max_position_pct,
                target_pct=settings.pead_target_pct,
                stop_pct=settings.pead_stop_pct,
            )
            results = scanner.scan(tickers, date.today())
            payload = [
                {
                    "ticker": result.ticker,
                    "action": result.signal.action.value,
                    "surprise_pct": result.signal.surprise_pct,
                    "skip_reason": result.signal.skip_reason,
                    "persisted": result.persisted,
                    "entry_price": result.entry_price,
                    "target_price": result.target_price,
                    "stop_loss": result.stop_loss,
                    "shares": result.shares,
                }
                for result in results
            ]
            self.state.record("daily_pead_scan", True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record("daily_pead_scan", False, payload)
            raise

    def run_pending_entries(self) -> dict[str, Any]:
        return self._run_entries(submit=False, job_id="pending_entry_scan")

    def run_realtime_entry_monitor(self) -> dict[str, Any]:
        return self._run_entries(submit=True, job_id="realtime_entry_monitor")

    def _run_entries(self, *, submit: bool, job_id: str) -> dict[str, Any]:
        try:
            settings = load_settings(self.env_path)
            client = create_supabase_client(settings)
            trading_repo = TradingRepository(client)
            config_repo = StrategyConfigRepository(client)
            vix = FredMacroDataProvider(settings).current_vix()
            daily_pnl_pct = _latest_daily_pnl_pct(trading_repo)
            pause = evaluate_daily_pause(
                trading_active=settings.trading_active and config_repo.is_trading_active(),
                vix=vix,
                daily_pnl_pct=daily_pnl_pct,
                spy_premarket_change_pct=create_market_data_provider(settings, env_path=self.env_path).spy_premarket_change_pct(),
                vix_threshold=settings.vix_pause_threshold,
                daily_loss_limit_pct=settings.daily_loss_limit_pct,
                spy_premarket_pause_pct=settings.spy_premarket_pause_pct,
            )
            if pause.paused:
                payload = {"attempted": 0, "submitted": 0, "blocked_reason": pause.reason, "skipped": 0, "vix": vix}
                trading_repo.upsert_daily_summary(
                    {
                        "date": date.today().isoformat(),
                        "vix": vix,
                        "trading_active": False,
                        "pause_reason": pause.reason,
                    }
                )
                payload["submit"] = submit
                self.state.record(job_id, False, payload)
                return payload
            engine = PaperExecutionEngine(
                trading_repo,
                config_repo,
                AlpacaBroker(settings),
            )
            summary = engine.execute_pending_watchlist(
                dry_run=not submit,
                max_total_positions=settings.max_total_positions,
                max_pead_long_positions=settings.max_pead_long_positions,
                max_pead_short_positions=settings.max_pead_short_positions,
                max_momentum_positions=settings.max_momentum_positions,
            )
            payload = summary.__dict__
            payload["vix"] = vix
            payload["submit"] = submit
            self.state.record(job_id, True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record(job_id, False, payload)
            raise

    def run_position_management(self) -> dict[str, Any]:
        return self._run_position_management(submit=False, job_id="position_management")

    def run_realtime_exit_monitor(self) -> dict[str, Any]:
        return self._run_position_management(submit=True, job_id="realtime_exit_monitor")

    def _run_position_management(self, *, submit: bool, job_id: str) -> dict[str, Any]:
        try:
            settings = load_settings(self.env_path)
            client = create_supabase_client(settings)
            engine = PaperExecutionEngine(
                TradingRepository(client),
                StrategyConfigRepository(client),
                AlpacaBroker(settings),
            )
            summary = engine.manage_open_positions(
                create_market_data_provider(settings, env_path=self.env_path),
                dry_run=not submit,
                max_hold_days=settings.pead_max_hold_days,
            )
            payload = summary.__dict__
            payload["submit"] = submit
            self.state.record(job_id, True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record(job_id, False, payload)
            raise

    def run_momentum_scan(self) -> dict[str, Any]:
        try:
            settings = load_settings(self.env_path)
            tickers = load_pead_universe(settings, env_path=self.env_path)
            if not tickers:
                payload = {"status": "no_universe", "message": "No momentum universe configured."}
                self.state.record("weekly_momentum_scan", False, payload)
                return payload

            client = create_supabase_client(settings)
            scanner = MomentumScanner(create_market_data_provider(settings, env_path=self.env_path), TradingRepository(client))
            scan_date = date.today()
            results = scanner.scan(tickers, scan_date)
            payload = {
                "status": "completed",
                "scan_date": scan_date.isoformat(),
                "universe_count": len(tickers),
                "persisted_count": sum(1 for result in results if result.persisted),
                "results": [
                    {
                        "ticker": result.ticker,
                        "total_score": result.score.total_score if result.score else None,
                        "price_momentum": result.score.price_momentum if result.score else None,
                        "earnings_momentum": result.score.earnings_momentum if result.score else None,
                        "quality_score": result.score.quality_score if result.score else None,
                        "persisted": result.persisted,
                        "skip_reason": result.skip_reason,
                    }
                    for result in results
                ],
            }
            self.state.record("weekly_momentum_scan", True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record("weekly_momentum_scan", False, payload)
            raise

    def run_operator_candidate_refresh(self) -> dict[str, Any]:
        try:
            settings = load_settings(self.env_path)
            client = create_supabase_client(settings)
            repo = TradingRepository(client)
            candidates = repo.list_candidate_watchlist()
            if len(candidates) >= settings.operator_min_candidates:
                payload = {
                    "status": "no_op",
                    "candidate_count": len(candidates),
                    "target_minimum": settings.operator_min_candidates,
                    "message": "Candidate pool already has enough pending setups.",
                }
                self.state.record("operator_candidate_refresh", True, payload)
                return payload

            active_tickers = {
                str(row.get("ticker", "")).upper()
                for row in [*candidates, *repo.list_entered_watchlist()]
                if row.get("ticker")
            }
            active_tickers.update(
                str(row.get("ticker", "")).upper()
                for row in repo.list_recent_trades(limit=100)
                if row.get("ticker")
            )
            market_data = create_market_data_provider(settings, env_path=self.env_path)
            momentum_rows = repo.list_recent_momentum_scores(limit=max(settings.operator_max_candidates * 3, 10))
            per_trade = settings.operator_paper_capital / settings.operator_trade_slots
            inserted = []
            skipped = []

            max_per_run = min(settings.operator_refresh_batch_size, 1)
            for row in momentum_rows:
                if len(inserted) >= max_per_run:
                    break
                if len(candidates) + len(inserted) >= settings.operator_min_candidates:
                    break
                ticker = str(row.get("ticker", "")).upper()
                if not ticker or ticker in active_tickers:
                    continue
                try:
                    price = _latest_price(market_data, ticker)
                    shares = int(per_trade / price) if price > 0 else 0
                    if shares <= 0:
                        skipped.append({"ticker": ticker, "reason": "share price exceeds per-bet allocation"})
                        continue
                    record = _operator_watchlist_record(ticker, price, shares, settings)
                    repo.insert_watchlist_candidate(record)
                    active_tickers.add(ticker)
                    inserted.append(
                        {
                            "ticker": ticker,
                            "entry_price": record.entry_price,
                            "target_price": record.target_price,
                            "stop_loss": record.stop_loss,
                            "shares": record.shares,
                        }
                    )
                except Exception as exc:
                    skipped.append({"ticker": ticker, "reason": str(exc)})

            payload = {
                "status": "completed",
                "candidate_before": len(candidates),
                "inserted_count": len(inserted),
                "inserted": inserted,
                "skipped": skipped[:10],
                "target_minimum": settings.operator_min_candidates,
                "batch_size": max_per_run,
            }
            self.state.record("operator_candidate_refresh", True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record("operator_candidate_refresh", False, payload)
            raise

    def run_operator_universe_refresh(self) -> dict[str, Any]:
        try:
            settings = load_settings(self.env_path)
            tickers = load_pead_universe(settings, env_path=self.env_path)
            if not tickers:
                payload = {"status": "no_universe", "message": "No universe configured."}
                self.state.record("operator_universe_refresh", False, payload)
                return payload

            client = create_supabase_client(settings)
            repo = TradingRepository(client)
            active = {
                str(row.get("ticker", "")).upper()
                for row in [*repo.list_candidate_watchlist(), *repo.list_entered_watchlist()]
                if row.get("ticker")
            }
            start = self.state.operator_universe_cursor % len(tickers)
            ordered = tickers[start:] + tickers[:start]
            ticker = next((item for item in ordered if item.upper() not in active), ordered[0])
            self.state.operator_universe_cursor = (tickers.index(ticker) + 1) % len(tickers)

            scanner = MomentumScanner(create_market_data_provider(settings, env_path=self.env_path), repo)
            result = scanner.scan_one(ticker, date.today(), min_score=1)
            promoted = self.run_operator_candidate_refresh()
            payload = {
                "status": "completed",
                "ticker": result.ticker,
                "score": result.score.total_score if result.score else None,
                "persisted": result.persisted,
                "skip_reason": result.skip_reason,
                "promoted": promoted,
            }
            self.state.record("operator_universe_refresh", True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record("operator_universe_refresh", False, payload)
            raise


def _latest_daily_pnl_pct(repo: TradingRepository) -> float:
    rows = repo.list_daily_summaries(limit=1)
    if not rows:
        return 0.0
    value = rows[0].get("pnl_pct")
    return float(value) if value is not None else 0.0


def _sentiment_scorer(settings: AppSettings):
    return FinBERTSentimentScorer() if settings.pead_sentiment == "finbert" else KeywordSentimentScorer()


def _latest_price(market_data, ticker: str) -> float:
    try:
        history = market_data.daily_history(ticker, period="10d")
        close = float(history["close"].dropna().iloc[-1])
        if close > 0:
            return close
    except Exception:
        pass
    price = market_data.company_profile(ticker).current_price
    if price <= 0:
        raise RuntimeError(f"No usable price for {ticker}")
    return float(price)


def _operator_watchlist_record(ticker: str, price: float, shares: int, settings: AppSettings) -> WatchlistRecord:
    target_price = price * (1 + settings.operator_target_pct)
    stop_loss = price * (1 - settings.operator_stop_pct)
    return WatchlistRecord(
        ticker=ticker,
        strategy="MOMENTUM",
        entry_price=round(price, 4),
        target_price=round(target_price, 4),
        stop_loss=round(stop_loss, 4),
        shares=shares,
        risk_dollars=round(abs(price - stop_loss) * shares, 2),
        position_value=round(price * shares, 2),
        status="candidate",
        skip_reason="operator candidate refresh",
    )
