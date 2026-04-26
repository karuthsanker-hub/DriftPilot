from __future__ import annotations

from trading_bot.scheduler import create_scheduler, register_jobs


def test_scheduler_registers_core_jobs() -> None:
    scheduler = create_scheduler()

    register_jobs(
        scheduler,
        pead_scan_job=lambda: None,
        pending_entry_job=lambda: None,
        momentum_scan_job=lambda: None,
    )

    assert {job.id for job in scheduler.get_jobs()} == {"daily_pead_scan", "pending_entry_scan", "weekly_momentum_scan"}

