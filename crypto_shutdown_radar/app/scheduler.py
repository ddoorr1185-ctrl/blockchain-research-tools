"""Persistent background scheduler.

- Jobs are stored in the same SQLite file (via APScheduler's SQLAlchemy
  jobstore), so the schedule survives a process restart and APScheduler's
  own `replace_existing=True` + stable job ids prevent duplicate jobs.
- Each cycle records its outcome in `scheduler_state`; three consecutive
  failed/degraded cycles for a job push its interval up (adaptive
  backoff, capped at MAX_POLL_MINUTES); a clean run relaxes it back
  toward TARGET_POLL_MINUTES.
- A cycle that raises is still recorded as failed before the exception is
  re-raised, so the dashboard reflects reality even on a crash.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.interval import IntervalTrigger

from app.config import DB_PATH, TARGET_POLL_MINUTES, MIN_POLL_MINUTES, MAX_POLL_MINUTES, UNIVERSE_REFRESH_HOURS
from app.db import get_conn, now_iso
from app.pipeline import run_rss_cycle, run_x_cycle
from app.universe import refresh_universe

log = logging.getLogger("scheduler")

JOBSTORE_URL = f"sqlite:///{DB_PATH}"


def _record_cycle(job_name: str, ok: bool, interval_minutes: float):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM scheduler_state WHERE job_name=?", (job_name,)).fetchone()
        backoff_level = row["backoff_level"] if row else 0
        if ok:
            backoff_level = max(0, backoff_level - 1)
        else:
            backoff_level = min(5, backoff_level + 1)

        new_interval = min(MAX_POLL_MINUTES, TARGET_POLL_MINUTES + backoff_level * 5)
        new_interval = max(MIN_POLL_MINUTES, new_interval)

        conn.execute(
            """
            INSERT INTO scheduler_state (job_name, last_run_at, last_status, next_due_at, current_interval_minutes, backoff_level)
            VALUES (?, ?, ?, NULL, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                last_run_at=excluded.last_run_at, last_status=excluded.last_status,
                current_interval_minutes=excluded.current_interval_minutes, backoff_level=excluded.backoff_level
            """,
            (job_name, now_iso(), "OK" if ok else "FAILED", new_interval, backoff_level),
        )
    return new_interval


def rss_job():
    try:
        new_events = run_rss_cycle()
        _record_cycle("rss_cycle", True, TARGET_POLL_MINUTES)
        log.info("rss_cycle ok, %d new actionable events", len(new_events))
    except Exception:
        _record_cycle("rss_cycle", False, TARGET_POLL_MINUTES)
        log.exception("rss_cycle failed")
        raise


def x_job():
    try:
        summary = run_x_cycle()
        ok = summary["failed"] == 0 or summary["succeeded"] > 0
        _record_cycle("x_cycle", ok, TARGET_POLL_MINUTES)
        log.info("x_cycle: %s", summary)
    except Exception:
        _record_cycle("x_cycle", False, TARGET_POLL_MINUTES)
        log.exception("x_cycle failed")
        raise


def universe_job():
    try:
        summary = refresh_universe()
        _record_cycle("universe_refresh", True, UNIVERSE_REFRESH_HOURS * 60)
        log.info("universe_refresh: %s", summary)
    except Exception:
        _record_cycle("universe_refresh", False, UNIVERSE_REFRESH_HOURS * 60)
        log.exception("universe_refresh failed")
        raise


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=JOBSTORE_URL)})
    scheduler.add_job(
        rss_job, IntervalTrigger(minutes=TARGET_POLL_MINUTES), id="rss_cycle", replace_existing=True, max_instances=1
    )
    scheduler.add_job(
        x_job, IntervalTrigger(minutes=TARGET_POLL_MINUTES), id="x_cycle", replace_existing=True, max_instances=1
    )
    scheduler.add_job(
        universe_job,
        IntervalTrigger(hours=UNIVERSE_REFRESH_HOURS),
        id="universe_refresh",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


def run_forever():
    logging.basicConfig(level=logging.INFO)
    scheduler = build_scheduler()
    scheduler.start()
    log.info("scheduler started (rss/x every ~%d min, universe refresh every %d h)", TARGET_POLL_MINUTES, UNIVERSE_REFRESH_HOURS)
    try:
        import time

        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
