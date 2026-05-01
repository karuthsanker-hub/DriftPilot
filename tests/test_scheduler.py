from __future__ import annotations

from trading_bot.scheduler import create_scheduler, register_jobs
from trading_bot.scheduler import TradingSchedulerService


CORE_JOBS = {
    "daily_pead_scan",
    "pending_entry_scan",
    "position_management",
    "weekly_momentum_scan",
    "operator_candidate_refresh",
    "operator_universe_refresh",
    "realtime_entry_monitor",
    "realtime_exit_monitor",
}


def test_scheduler_registers_core_jobs() -> None:
    scheduler = create_scheduler()

    register_jobs(
        scheduler,
        pead_scan_job=lambda: None,
        pending_entry_job=lambda: None,
        position_management_job=lambda: None,
        momentum_scan_job=lambda: None,
        operator_candidate_refresh_job=lambda: None,
        operator_universe_refresh_job=lambda: None,
        realtime_entry_monitor_job=lambda: None,
        realtime_exit_monitor_job=lambda: None,
    )

    assert {job.id for job in scheduler.get_jobs()} == CORE_JOBS


def test_trading_scheduler_status_lists_jobs() -> None:
    service = TradingSchedulerService(env_path=".env.example")

    status = service.status()

    assert status["running"] is False
    assert {job["id"] for job in status["jobs"]} == CORE_JOBS


def test_trading_scheduler_pead_scan_reports_missing_universe(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PEAD_UNIVERSE_FILE=missing.csv\nPEAD_SCAN_TICKERS=\n")
    service = TradingSchedulerService(env_path=str(env_path))

    result = service.run_pead_scan()

    assert result == [{"status": "no_universe", "message": "No PEAD universe configured."}]
    assert service.status()["last_runs"][0]["ok"] is False


def test_trading_scheduler_momentum_scan_reports_missing_universe(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PEAD_UNIVERSE_FILE=missing.csv\nPEAD_SCAN_TICKERS=\n")
    service = TradingSchedulerService(env_path=str(env_path))

    result = service.run_momentum_scan()

    assert result == {"status": "no_universe", "message": "No momentum universe configured."}
    assert service.status()["last_runs"][0]["ok"] is False
