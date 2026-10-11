"""Builds and refreshes the tracked cryptocurrency universe from CoinGecko's
free public API (no API key required for this call volume).

Design notes:
- /coins/markets gives price/market-cap/rank for up to 250 coins per call;
  4 calls cover the top 1000.
- /coins/{id} gives links (homepage, twitter_screen_name) but is 1 coin per
  call and is the endpoint CoinGecko rate-limits hardest on the free tier.
  We throttle conservatively and persist progress so a run can be resumed.
- A Twitter/X handle returned directly by CoinGecko's own metadata is marked
  VERIFIED (method='coingecko_metadata'). If CoinGecko has no handle on file,
  we store nothing and leave it UNVERIFIED rather than guessing a handle from
  the coin name/symbol.
"""
import time
import logging
from typing import Optional

import requests

from app.config import (
    COINGECKO_BASE,
    COINGECKO_MIN_INTERVAL_SECONDS,
    UNIVERSE_SIZE,
    UNIVERSE_PAGE_SIZE,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.db import get_conn, now_iso

log = logging.getLogger("universe")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_last_request_at = 0.0


def _throttled_get(url, params=None, max_retries=5):
    """GET with a minimum inter-request gap and exponential backoff on 429/5xx.

    Returns the parsed JSON, or None if every retry failed (a failure is
    NEVER treated as "no data" by callers -- it is recorded as an error).
    """
    global _last_request_at
    delay = COINGECKO_MIN_INTERVAL_SECONDS
    for attempt in range(max_retries):
        wait = COINGECKO_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = _session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            _last_request_at = time.monotonic()
        except requests.RequestException as exc:
            log.warning("request error %s (attempt %d): %s", url, attempt, exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", delay))
            log.warning("rate limited on %s, sleeping %.1fs", url, retry_after)
            time.sleep(retry_after)
            delay = min(delay * 2, 60)
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        log.error("unrecoverable HTTP %s from %s", resp.status_code, url)
        return None
    return None


def fetch_top_markets(limit: int = UNIVERSE_SIZE):
    """Fetch top-N coins by market cap. Returns (coins, error_pages)."""
    coins = []
    error_pages = []
    pages = (limit + UNIVERSE_PAGE_SIZE - 1) // UNIVERSE_PAGE_SIZE
    for page in range(1, pages + 1):
        data = _throttled_get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": UNIVERSE_PAGE_SIZE,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        if data is None:
            error_pages.append(page)
            continue
        coins.extend(data)
    return coins[:limit], error_pages


def fetch_coin_detail(coin_id: str):
    return _throttled_get(
        f"{COINGECKO_BASE}/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
        },
    )


def upsert_coin(conn, market_row):
    ts = now_iso()
    cid = market_row["id"]
    existing = conn.execute("SELECT coingecko_id FROM coins WHERE coingecko_id=?", (cid,)).fetchone()
    conn.execute(
        """
        INSERT INTO coins (coingecko_id, symbol, name, market_cap_rank, website,
                            current_price, market_cap, last_updated, is_active,
                            first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, '', ?, ?, ?, 1, ?, ?)
        ON CONFLICT(coingecko_id) DO UPDATE SET
            symbol=excluded.symbol,
            name=excluded.name,
            market_cap_rank=excluded.market_cap_rank,
            current_price=excluded.current_price,
            market_cap=excluded.market_cap,
            last_updated=excluded.last_updated,
            is_active=1,
            last_seen_at=excluded.last_seen_at
        """,
        (
            cid,
            market_row.get("symbol", ""),
            market_row.get("name", ""),
            market_row.get("market_cap_rank"),
            market_row.get("current_price"),
            market_row.get("market_cap"),
            ts,
            ts if not existing else None,
            ts,
        ),
    )
    conn.execute(
        """INSERT INTO coin_rank_history (coingecko_id, market_cap_rank, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (cid, market_row.get("market_cap_rank"), market_row.get("market_cap"), market_row.get("current_price"), ts),
    )


def record_account(conn, coingecko_id, platform, handle_or_url, status, method):
    conn.execute(
        """
        INSERT INTO official_accounts (coingecko_id, platform, handle_or_url, verification_status, verification_method, added_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, platform, handle_or_url) DO UPDATE SET
            verification_status=excluded.verification_status,
            verification_method=excluded.verification_method
        """,
        (coingecko_id, platform, handle_or_url, status, method, now_iso()),
    )


def refresh_universe(limit: int = UNIVERSE_SIZE, fetch_details: bool = True, detail_limit: Optional[int] = None):
    """Full refresh cycle: markets + (optionally) per-coin account mapping.

    Returns a summary dict so callers/tests can assert on real outcomes
    instead of trusting silent success.
    """
    coins, error_pages = fetch_top_markets(limit)
    summary = {
        "requested": limit,
        "retrieved": len(coins),
        "error_pages": error_pages,
        "detail_checked": 0,
        "detail_errors": 0,
        "accounts_verified": 0,
        "accounts_unverified": 0,
    }
    with get_conn() as conn:
        for row in coins:
            upsert_coin(conn, row)

    if not fetch_details:
        return summary

    detail_targets = coins if detail_limit is None else coins[:detail_limit]
    for row in detail_targets:
        cid = row["id"]
        detail = fetch_coin_detail(cid)
        with get_conn() as conn:
            if detail is None:
                summary["detail_errors"] += 1
                continue
            summary["detail_checked"] += 1
            homepage = ""
            links = detail.get("links", {}) or {}
            homepages = links.get("homepage") or []
            if homepages and homepages[0]:
                homepage = homepages[0]
                conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?", (homepage, cid))

            twitter_handle = (links.get("twitter_screen_name") or "").strip()
            if twitter_handle:
                record_account(conn, cid, "x", twitter_handle, "VERIFIED", "coingecko_metadata")
                summary["accounts_verified"] += 1
            else:
                # No verified handle on file. We explicitly mark this
                # UNVERIFIED rather than guessing one from the coin name.
                record_account(conn, cid, "x", "", "UNVERIFIED", "no_handle_in_coingecko_metadata")
                summary["accounts_unverified"] += 1

    return summary
