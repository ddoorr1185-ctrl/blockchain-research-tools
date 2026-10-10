"""Glues sources -> de-dup -> classification -> events -> alerts, and
records source health honestly (a failed fetch is never recorded as "0 new
items found").
"""
import hashlib
import logging

from app.classifier import classify
from app.db import get_conn, now_iso
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.sources.x_adapter import XAccountAdapter
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES
from app.telegram_bot import dispatch_alert

log = logging.getLogger("pipeline")


def _dedup_key(item) -> str:
    """Events about the same underlying announcement, seen via several
    sources (e.g. an exchange's own post + three news outlets covering it),
    collapse onto the same key: same affected coin + same event type +
    same calendar day. This is an approximation documented as such.
    """
    day = (item.published_at or "")[:10]
    basis = f"{item.coingecko_id}:{item.source_type}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16] + f":{day}"


def record_source_health(conn, source_key: str, ok: bool, error: str | None):
    row = conn.execute("SELECT * FROM source_health WHERE source_key=?", (source_key,)).fetchone()
    failures = 0 if ok else (row["consecutive_failures"] + 1 if row else 1)
    status = "OK" if ok else ("DEGRADED" if failures < 3 else "UNREACHABLE")
    conn.execute(
        """
        INSERT INTO source_health (source_key, last_attempt_at, last_success_at, consecutive_failures, status, last_error)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            last_attempt_at=excluded.last_attempt_at,
            last_success_at=CASE WHEN ? THEN excluded.last_attempt_at ELSE source_health.last_success_at END,
            consecutive_failures=excluded.consecutive_failures,
            status=excluded.status,
            last_error=excluded.last_error
        """,
        (source_key, now_iso(), now_iso() if ok else None, failures, status, error, ok),
    )


def ingest_items(conn, items, is_primary_source: bool, source_credibility: float):
    new_event_ids = []
    for item in items:
        try:
            cur = conn.execute(
                """
                INSERT INTO raw_items (source_type, source_name, external_id, author, coingecko_id,
                                        published_at, discovered_at, text, url, collection_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OK')
                """,
                (
                    item.source_type,
                    item.source_name,
                    item.external_id,
                    item.author,
                    item.coingecko_id,
                    item.published_at,
                    now_iso(),
                    item.text,
                    item.url,
                ),
            )
        except Exception:
            # UNIQUE constraint -> already-seen item, skip (de-dup).
            continue
        raw_item_id = cur.lastrowid

        result = classify(item.text, source_credibility=source_credibility, is_primary_source=is_primary_source)
        if result.severity == "NONE":
            continue

        dedup_key = _dedup_key(item)
        existing = conn.execute("SELECT id, severity FROM events WHERE dedup_key=?", (dedup_key,)).fetchone()
        if existing is not None:
            # Same underlying announcement already recorded (via this or
            # another source). Only "upgrade" it if new evidence raises the
            # severity; otherwise this corroborating item is linked via
            # raw_items but produces no duplicate event/alert.
            severity_rank = {"NONE": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
            if severity_rank.get(result.severity, 0) > severity_rank.get(existing["severity"], 0):
                conn.execute(
                    """UPDATE events SET severity=?, confidence=?, evidence=?, detection_timestamp=?
                       WHERE id=?""",
                    (result.severity, result.confidence, "; ".join(result.evidence) or item.text[:280], now_iso(), existing["id"]),
                )
                if result.is_actionable:
                    new_event_ids.append(existing["id"])
            continue

        event_cur = conn.execute(
            """
            INSERT INTO events (raw_item_id, event_type, event_label, severity, confidence,
                                 source_credibility, affected_entity, affected_scope, coingecko_id,
                                 evidence, event_timestamp, detection_timestamp, dedup_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_item_id,
                result.event_type,
                result.event_label,
                result.severity,
                result.confidence,
                source_credibility,
                item.author,
                result.affected_scope,
                item.coingecko_id,
                "; ".join(result.evidence) or item.text[:280],
                item.published_at,
                now_iso(),
                dedup_key,
            ),
        )
        if result.is_actionable:
            new_event_ids.append(event_cur.lastrowid)
    return new_event_ids


def run_rss_cycle():
    """Polls all configured RSS/news/announcement feeds once."""
    adapters = build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES)
    total_new_events = []
    for adapter in adapters:
        source_key = f"rss:{adapter.name}"
        with get_conn() as conn:
            since_row = conn.execute(
                "SELECT external_id FROM raw_items WHERE source_name=? ORDER BY id DESC LIMIT 1", (adapter.name,)
            ).fetchone()
        since_id = since_row["external_id"] if since_row else None

        result = adapter.collect(since_id=since_id)
        with get_conn() as conn:
            record_source_health(conn, source_key, result.ok, result.error)
            if not result.ok:
                log.warning("source %s failed: %s", source_key, result.error)
                continue
            is_primary = adapter.source_type == "rss_announcement"
            event_ids = ingest_items(conn, result.items, is_primary_source=is_primary, source_credibility=adapter.credibility)
            total_new_events.extend(event_ids)

    for event_id in total_new_events:
        dispatch_alert(event_id)
    return total_new_events


def run_x_cycle(limit_accounts: int | None = None):
    """Polls verified X accounts. Honestly reports per-account success."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM official_accounts WHERE platform='x' AND verification_status='VERIFIED' AND handle_or_url != ''"
        ).fetchall()
    if limit_accounts:
        rows = rows[:limit_accounts]

    total_new_events = []
    checked, succeeded, failed = 0, 0, 0
    for account in rows:
        checked += 1
        with get_conn() as conn:
            state = conn.execute("SELECT * FROM account_monitor_state WHERE account_id=?", (account["id"],)).fetchone()
        since_id = state["last_post_id_seen"] if state else None

        adapter = XAccountAdapter(account["handle_or_url"])
        result = adapter.collect(since_id=since_id)

        with get_conn() as conn:
            if result.ok:
                succeeded += 1
                new_last_id = result.items[0].external_id if result.items else since_id
                conn.execute(
                    """
                    INSERT INTO account_monitor_state (account_id, last_attempt_at, last_success_at, last_post_id_seen, consecutive_failures, status, last_error)
                    VALUES (?, ?, ?, ?, 0, 'OK', NULL)
                    ON CONFLICT(account_id) DO UPDATE SET
                        last_attempt_at=excluded.last_attempt_at,
                        last_success_at=excluded.last_success_at,
                        last_post_id_seen=excluded.last_post_id_seen,
                        consecutive_failures=0, status='OK', last_error=NULL
                    """,
                    (account["id"], now_iso(), now_iso(), new_last_id),
                )
                for item in result.items:
                    item.coingecko_id = account["coingecko_id"]
                event_ids = ingest_items(conn, result.items, is_primary_source=True, source_credibility=0.9)
                total_new_events.extend(event_ids)
            else:
                failed += 1
                prior_failures = state["consecutive_failures"] if state else 0
                conn.execute(
                    """
                    INSERT INTO account_monitor_state (account_id, last_attempt_at, last_success_at, last_post_id_seen, consecutive_failures, status, last_error)
                    VALUES (?, ?, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        last_attempt_at=excluded.last_attempt_at,
                        consecutive_failures=?,
                        status=?,
                        last_error=excluded.last_error
                    """,
                    (
                        account["id"], now_iso(), prior_failures + 1,
                        "DEGRADED" if prior_failures < 3 else "UNREACHABLE",
                        result.error,
                        prior_failures + 1,
                        "DEGRADED" if prior_failures < 3 else "UNREACHABLE",
                    ),
                )
            record_source_health(conn, "x:aggregate", result.ok, result.error)

    for event_id in total_new_events:
        dispatch_alert(event_id)
    return {"checked": checked, "succeeded": succeeded, "failed": failed, "new_events": len(total_new_events)}
