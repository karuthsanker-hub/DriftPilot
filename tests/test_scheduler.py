from __future__ import annotations

from trading_bot.scheduler import create_scheduler, register_jobs
from trading_bot.scheduler import TradingSchedulerService


def test_scheduler_registers_core_jobs() -> None:
    scheduler = create_scheduler()

    register_jobs(
        scheduler,
        pead_scan_job=lambda: None,
        pending_entry_job=lambda: None,
        momentum_scan_job=lambda: None,
    )

    assert {job.id for job in scheduler.get_jobs()} == {"daily_pead_scan", "pending_entry_scan", "weekly_momentum_scan"}


def test_trading_scheduler_status_lists_jobs() -> None:
    service = TradingSchedulerService(env_path=".env.example")

    status = service.status()

    assert status["running"] is False
    assert {job["id"] for job in status["jobs"]} == {"daily_pead_scan", "pending_entry_scan", "weekly_momentum_scan"}


def test_trading_scheduler_momentum_placeholder_records_run() -> None:
    service = TradingSchedulerService(env_path=".env.example")

    result = service.run_momentum_scan()

    assert result["status"] == "not_implemented"
    assert service.status()["last_runs"][0]["job"] == "weekly_momentum_scan"
