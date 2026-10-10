"""Telegram alert delivery via the free Telegram Bot HTTP API (no paid tier
exists for sending messages from a bot you own; this only costs nothing to
the operator by construction).

Setup (also in README):
  1. Message @BotFather on Telegram, run /newbot, copy the bot token.
  2. Message your new bot once (or add it to a group) so it can message you.
  3. Get your chat_id: visit
     https://api.telegram.org/bot<TOKEN>/getUpdates after messaging the bot,
     and read the `chat.id` field.
  4. Put both values in CSR_TELEGRAM_BOT_TOKEN / CSR_TELEGRAM_CHAT_ID.
"""
import json
import logging

import requests

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HTTP_TIMEOUT_SECONDS
from app.db import get_conn, now_iso

log = logging.getLogger("telegram")

SEVERITY_ORDER = {"NONE": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def format_alert_message(event_row, coin_row, market: dict) -> str:
    severity = event_row["severity"]
    header = {
        "CRITICAL": "🔴 CRITICAL SHUTDOWN ALERT",
        "HIGH": "🟠 HIGH SEVERITY ALERT",
        "MEDIUM": "🟡 MEDIUM SEVERITY ALERT (unconfirmed)",
    }.get(severity, "ALERT")

    price = market.get("price", {})
    futures = market.get("futures", {})
    futures_lines = "\n".join(f"  {ex.capitalize()}: {status}" for ex, status in futures.items())

    name = coin_row["name"] if coin_row else (event_row["affected_entity"] or "Unknown")
    symbol = coin_row["symbol"].upper() if coin_row else "?"

    lines = [
        header,
        "",
        f"Token: {name} ({symbol})",
        f"Event: {event_row['event_label']} [{event_row['event_type']}]",
        f"Scope: {event_row['affected_scope']}",
        f"Severity: {severity}   Confidence: {event_row['confidence']:.0%}",
        "",
        f"Price: ${price.get('price_usd', 'UNKNOWN')}",
        f"1H: {price.get('change_1h_pct', 'UNKNOWN')}",
        f"24H: {price.get('change_24h_pct', 'UNKNOWN')}",
        f"7D: {price.get('change_7d_pct', 'UNKNOWN')}",
        "",
        "Futures:",
        futures_lines,
        "",
        "Assessment (automated interpretation, verify independently):",
        f"{event_row['evidence']}",
        "",
        f"Published: {event_row['event_timestamp'] or 'unknown'}",
        f"Detected: {event_row['detection_timestamp']}",
    ]
    return "\n".join(lines)


def send_telegram_message(text: str) -> tuple[bool, str | None]:
    if not is_configured():
        return False, "Telegram not configured (missing bot token or chat id)"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code == 200 and resp.json().get("ok"):
        return True, None
    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def should_alert(conn, event_row) -> bool:
    """Avoids repeated alerts for the same dedup_key unless severity has
    increased since the last alert for that key.
    """
    prior = conn.execute(
        """
        SELECT a.severity_at_send FROM alerts a
        JOIN events e ON e.id = a.event_id
        WHERE e.dedup_key = ?
        ORDER BY a.sent_at DESC LIMIT 1
        """,
        (event_row["dedup_key"],),
    ).fetchone()
    if prior is None:
        return True
    return SEVERITY_ORDER.get(event_row["severity"], 0) > SEVERITY_ORDER.get(prior["severity_at_send"], 0)


def dispatch_alert(event_id: int):
    with get_conn() as conn:
        event_row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event_row is None:
            return
        if not should_alert(conn, event_row):
            return

        coin_row = None
        market = {}
        if event_row["coingecko_id"]:
            coin_row = conn.execute("SELECT * FROM coins WHERE coingecko_id=?", (event_row["coingecko_id"],)).fetchone()
            if coin_row:
                from app.market_data import build_market_section
                market = build_market_section(event_row["coingecko_id"], coin_row["symbol"])

        message = format_alert_message(event_row, coin_row, market)
        ok, error = send_telegram_message(message)

        conn.execute(
            """
            INSERT INTO alerts (event_id, sent_at, severity_at_send, telegram_ok, telegram_error, market_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, now_iso(), event_row["severity"], 1 if ok else 0, error, json.dumps(market)),
        )
        if not ok:
            log.warning("telegram delivery failed for event %s: %s", event_id, error)
