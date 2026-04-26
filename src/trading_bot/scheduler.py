from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


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

