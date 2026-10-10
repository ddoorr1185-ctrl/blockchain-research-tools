from unittest.mock import patch

from app import telegram_bot
from app.db import get_conn, now_iso
from app.pipeline import ingest_items
from app.sources.base import CollectedItem
from tests import fixtures as f


class _FakeResponse:
    def __init__(self, status_code, ok=True):
        self.status_code = status_code
        self._ok = ok

    def json(self):
        return {"ok": self._ok}

    @property
    def text(self):
        return "error body"


def _make_event():
    item = CollectedItem(
        source_type="rss_announcement",
        source_name="test",
        external_id="telegram-test-1",
        author="ExampleChain Labs",
        coingecko_id=None,
        published_at=now_iso(),
        text=f.SYNTHETIC_PERMANENT_SHUTDOWN[0],
        url="https://example.com/x",
    )
    with get_conn() as conn:
        ids = ingest_items(conn, [item], is_primary_source=True, source_credibility=0.9)
    return ids[0]


def test_telegram_not_configured_records_failure_not_fake_success():
    event_id = _make_event()
    with patch("app.config.TELEGRAM_BOT_TOKEN", ""), patch("app.config.TELEGRAM_CHAT_ID", ""):
        ok, error = telegram_bot.send_telegram_message("hello")
    assert ok is False
    assert "not configured" in error


def test_telegram_dispatch_records_alert_row():
    event_id = _make_event()
    with patch("app.telegram_bot.is_configured", return_value=True), \
         patch("app.telegram_bot.send_telegram_message", return_value=(True, None)):
        telegram_bot.dispatch_alert(event_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE event_id=?", (event_id,)).fetchone()
    assert row is not None
    assert row["telegram_ok"] == 1


def test_telegram_delivery_failure_is_recorded_honestly():
    event_id = _make_event()
    with patch("app.telegram_bot.is_configured", return_value=True), \
         patch("app.telegram_bot.send_telegram_message", return_value=(False, "HTTP 401: unauthorized")):
        telegram_bot.dispatch_alert(event_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE event_id=?", (event_id,)).fetchone()
    assert row["telegram_ok"] == 0
    assert "401" in row["telegram_error"]


def test_no_repeat_alert_unless_severity_increases():
    event_id = _make_event()
    with patch("app.telegram_bot.is_configured", return_value=True), \
         patch("app.telegram_bot.send_telegram_message", return_value=(True, None)):
        telegram_bot.dispatch_alert(event_id)
        telegram_bot.dispatch_alert(event_id)  # same severity, should not re-send
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM alerts WHERE event_id=?", (event_id,)).fetchone()["c"]
    assert count == 1
