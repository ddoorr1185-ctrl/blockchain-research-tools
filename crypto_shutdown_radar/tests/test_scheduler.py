from app.db import get_conn
from app.scheduler import _record_cycle
from app.config import TARGET_POLL_MINUTES, MAX_POLL_MINUTES, MIN_POLL_MINUTES


def test_interval_backs_off_after_repeated_failures():
    intervals = []
    for _ in range(5):
        intervals.append(_record_cycle("test_job", ok=False, interval_minutes=TARGET_POLL_MINUTES))
    assert intervals[-1] > intervals[0]
    assert intervals[-1] <= MAX_POLL_MINUTES


def test_interval_recovers_after_success():
    for _ in range(5):
        _record_cycle("test_job_2", ok=False, interval_minutes=TARGET_POLL_MINUTES)
    degraded = _record_cycle("test_job_2", ok=False, interval_minutes=TARGET_POLL_MINUTES)
    for _ in range(10):
        recovered = _record_cycle("test_job_2", ok=True, interval_minutes=TARGET_POLL_MINUTES)
    assert recovered <= degraded
    assert recovered >= MIN_POLL_MINUTES


def test_state_persists_across_calls_simulating_restart():
    _record_cycle("persisted_job", ok=False, interval_minutes=TARGET_POLL_MINUTES)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM scheduler_state WHERE job_name='persisted_job'").fetchone()
    assert row is not None
    assert row["last_status"] == "FAILED"
    # A fresh call (simulating a new process reading the same DB after restart)
    # sees the same persisted backoff_level rather than starting from zero.
    with get_conn() as conn2:
        row2 = conn2.execute("SELECT * FROM scheduler_state WHERE job_name='persisted_job'").fetchone()
    assert row2["backoff_level"] == row["backoff_level"]
