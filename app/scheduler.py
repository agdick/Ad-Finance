"""In-process background jobs: periodic bank sync and a daily housekeeping pass."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .db import SessionLocal
from .services import alerts, recurring
from .services.dates import today
from .services.sync import sync_all

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def run_sync() -> None:
    db = SessionLocal()
    try:
        for r in sync_all(db):
            if r.ok:
                log.info("Sync connection %s: +%d ~%d -%d", r.connection_id, r.added, r.updated, r.removed)
            else:
                log.warning("Sync connection %s failed: %s", r.connection_id, r.message)
    finally:
        db.close()


def run_daily() -> None:
    """Keeps recurring flags and month-start alert state current even without new data."""
    db = SessionLocal()
    try:
        recurring.refresh_all(db, today())
        alerts.evaluate_alerts(db)
        db.commit()
    finally:
        db.close()


def start() -> None:
    global _scheduler
    s = get_settings()
    if not s.scheduler_enabled or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=s.timezone)
    if s.sync_interval_hours > 0:
        _scheduler.add_job(run_sync, "interval", hours=s.sync_interval_hours, id="sync", max_instances=1, coalesce=True)
    _scheduler.add_job(run_daily, "cron", hour=5, minute=10, id="daily", max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("Scheduler started (sync every %sh)", s.sync_interval_hours)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
