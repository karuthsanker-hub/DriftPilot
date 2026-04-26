from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from trading_bot.data.market_data import YFinanceMarketDataProvider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.sentiment import KeywordSentimentScorer
from trading_bot.settings import AppSettings, load_settings


def create_scheduler(*, timezone: str = "America/New_York") -> BackgroundScheduler:
    return BackgroundScheduler(timezone=timezone)


def register_jobs(
    scheduler: BackgroundScheduler,
    *,
    pead_scan_job,
    pending_entry_job,
    momentum_scan_job,
) -> None:
    scheduler.add_job(pead_scan_job, CronTrigger(day_of_week="mon-fri", hour=16, minute=30), id="daily_pead_scan", replace_existing=True)
    scheduler.add_job(pending_entry_job, CronTrigger(day_of_week="mon-fri", hour=9, minute=25), id="pending_entry_scan", replace_existing=True)
    scheduler.add_job(momentum_scan_job, CronTrigger(day_of_week="mon", hour=6, minute=0), id="weekly_momentum_scan", replace_existing=True)


@dataclass
class SchedulerState:
    running: bool = False
    last_runs: list[dict[str, Any]] = field(default_factory=list)

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
        register_jobs(
            self.scheduler,
            pead_scan_job=self.run_pead_scan,
            pending_entry_job=self.run_pending_entries,
            momentum_scan_job=self.run_momentum_scan,
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
        register_jobs(
            self.scheduler,
            pead_scan_job=self.run_pead_scan,
            pending_entry_job=self.run_pending_entries,
            momentum_scan_job=self.run_momentum_scan,
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
            repo = TradingRepository(create_supabase_client(settings))
            scanner = PEADScanner(YFinanceMarketDataProvider(), KeywordSentimentScorer(), repo)
            results = scanner.scan(settings.pead_scan_tickers, date.today())
            payload = [
                {
                    "ticker": result.ticker,
                    "action": result.signal.action.value,
                    "surprise_pct": result.signal.surprise_pct,
                    "skip_reason": result.signal.skip_reason,
                    "persisted": result.persisted,
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
        try:
            settings = load_settings(self.env_path)
            client = create_supabase_client(settings)
            engine = PaperExecutionEngine(
                TradingRepository(client),
                StrategyConfigRepository(client),
                AlpacaBroker(settings),
            )
            summary = engine.execute_pending_watchlist(dry_run=True)
            payload = summary.__dict__
            self.state.record("pending_entry_scan", True, payload)
            return payload
        except Exception as exc:
            payload = {"error": str(exc)}
            self.state.record("pending_entry_scan", False, payload)
            raise

    def run_momentum_scan(self) -> dict[str, Any]:
        payload = {"status": "not_implemented", "message": "Momentum scanner rules exist, but universe scan is not wired yet."}
        self.state.record("weekly_momentum_scan", True, payload)
        return payload
