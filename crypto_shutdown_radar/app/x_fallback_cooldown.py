"""Cooldown tracking for free X-fallback candidate methods (Task 8).

Every method tested in app.sources.x_fallback_experiments failed 0/5 on
the first real run. Re-testing the same dead public mirrors from scratch
on every validation run wastes time and requests for no new information.
This module lets a caller skip a method that failed recently, while still
periodically re-checking in case it comes back (X-adjacent services are
genuinely volatile -- see xcancel.com's multiple takedowns/revivals).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import now_iso

DEFAULT_COOLDOWN_HOURS = 24.0


def get_method_health(conn, method_key: str):
    return conn.execute(
        "SELECT * FROM x_fallback_method_health WHERE method_key=?", (method_key,)
    ).fetchone()


def record_method_result(conn, method_key: str, success: bool):
    existing = get_method_health(conn, method_key)
    consecutive_failures = 0 if success else (existing["consecutive_failures"] + 1 if existing else 1)
    conn.execute(
        """
        INSERT INTO x_fallback_method_health (method_key, last_tested_at, last_success, consecutive_failures)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(method_key) DO UPDATE SET
            last_tested_at=excluded.last_tested_at,
            last_success=excluded.last_success,
            consecutive_failures=excluded.consecutive_failures
        """,
        (method_key, now_iso(), 1 if success else 0, consecutive_failures),
    )


def is_in_cooldown(conn, method_key: str, cooldown_hours: float = DEFAULT_COOLDOWN_HOURS) -> Optional[str]:
    """Returns a human-readable reason string if this method should be
    skipped right now (recently failed, cooldown not yet elapsed), or
    None if it should be tested. A method that has never failed, or whose
    last recorded result was a success, is never in cooldown.
    """
    row = get_method_health(conn, method_key)
    if row is None or row["last_success"]:
        return None
    last_tested = datetime.strptime(row["last_tested_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - last_tested
    if elapsed < timedelta(hours=cooldown_hours):
        remaining = timedelta(hours=cooldown_hours) - elapsed
        return (
            f"cooldown active: last failed at {row['last_tested_at']} "
            f"({row['consecutive_failures']} consecutive failures), "
            f"{remaining.total_seconds()/3600:.1f}h remaining"
        )
    return None
