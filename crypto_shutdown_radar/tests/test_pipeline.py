from app.db import get_conn
from app.pipeline import ingest_items, record_source_health
from app.sources.base import CollectedItem
from tests import fixtures as f


def _item(text, external_id, coingecko_id="examplechain", source_type="rss_announcement", published_at="2026-01-01T00:00:00Z"):
    return CollectedItem(
        source_type=source_type,
        source_name="test-source",
        external_id=external_id,
        author="ExampleChain Labs",
        coingecko_id=coingecko_id,
        published_at=published_at,
        text=text,
        url="https://example.com/post/" + external_id,
    )


def test_duplicate_raw_item_is_not_double_inserted():
    item = _item(f.SYNTHETIC_PERMANENT_SHUTDOWN[0], "post-1")
    with get_conn() as conn:
        ids_1 = ingest_items(conn, [item], is_primary_source=True, source_credibility=0.9)
    with get_conn() as conn:
        ids_2 = ingest_items(conn, [item], is_primary_source=True, source_credibility=0.9)
        raw_count = conn.execute("SELECT COUNT(*) c FROM raw_items WHERE external_id='post-1'").fetchone()["c"]
    assert len(ids_1) == 1
    assert len(ids_2) == 0  # second ingest is a pure duplicate, nothing new
    assert raw_count == 1


def test_same_announcement_from_two_sources_collapses_to_one_event():
    item_a = _item(f.SYNTHETIC_DUPLICATE_PAIR[0], "post-a", source_type="rss_announcement")
    item_b = _item(f.SYNTHETIC_DUPLICATE_PAIR[1], "post-b", source_type="rss_announcement")
    with get_conn() as conn:
        ingest_items(conn, [item_a], is_primary_source=True, source_credibility=0.9)
        ingest_items(conn, [item_b], is_primary_source=True, source_credibility=0.9)
        event_count = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE coingecko_id='examplechain'"
        ).fetchone()["c"]
    # Both raw items are kept (two distinct source posts), but they dedup to one event.
    assert event_count == 1


def test_non_actionable_text_produces_no_event():
    item = _item(f.SYNTHETIC_TEMPORARY_OUTAGE[0], "post-outage")
    with get_conn() as conn:
        ids = ingest_items(conn, [item], is_primary_source=True, source_credibility=0.9)
        event_count = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert ids == []
    assert event_count == 0


def test_source_outage_recorded_as_unreachable_not_as_zero_items():
    with get_conn() as conn:
        record_source_health(conn, "rss:test-feed", ok=False, error="connection timed out")
        record_source_health(conn, "rss:test-feed", ok=False, error="connection timed out")
        record_source_health(conn, "rss:test-feed", ok=False, error="connection timed out")
        row = conn.execute("SELECT * FROM source_health WHERE source_key='rss:test-feed'").fetchone()
    assert row["status"] == "UNREACHABLE"
    assert row["consecutive_failures"] == 3
    assert row["last_error"] == "connection timed out"


def test_source_recovers_to_ok_after_success():
    with get_conn() as conn:
        record_source_health(conn, "rss:flaky-feed", ok=False, error="timeout")
        record_source_health(conn, "rss:flaky-feed", ok=True, error=None)
        row = conn.execute("SELECT * FROM source_health WHERE source_key='rss:flaky-feed'").fetchone()
    assert row["status"] == "OK"
    assert row["consecutive_failures"] == 0
