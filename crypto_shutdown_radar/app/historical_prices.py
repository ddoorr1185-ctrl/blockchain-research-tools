"""Incremental, cached historical price lookups (Task 7).

Each (coingecko_id, date) point is fetched at most once, ever, across all
runs: a successful lookup is persisted immediately and never re-queried; a
rate-limited/errored point is persisted too (so it doesn't silently vanish
from the record) and is retried on a *later* run, never blocked on now.
Uses app.universe.get_json_no_retry -- the same non-blocking primitive
used everywhere else in this project -- so a 429 here cannot stall
anything either.
"""
import logging
import time
from typing import Optional

from app.config import COINGECKO_BASE
from app.db import get_conn, now_iso
from app.universe import get_json_no_retry

log = logging.getLogger("historical_prices")


def get_cached_price(conn, coingecko_id: str, date_ddmmyyyy: str):
    return conn.execute(
        "SELECT * FROM historical_price_cache WHERE coingecko_id=? AND date_ddmmyyyy=?",
        (coingecko_id, date_ddmmyyyy),
    ).fetchone()


def record_price(conn, coingecko_id: str, date_ddmmyyyy: str, status: str, price_usd: Optional[float]):
    conn.execute(
        """
        INSERT INTO historical_price_cache (coingecko_id, date_ddmmyyyy, status, price_usd, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, date_ddmmyyyy) DO UPDATE SET
            status=excluded.status, price_usd=excluded.price_usd, fetched_at=excluded.fetched_at
        """,
        (coingecko_id, date_ddmmyyyy, status, price_usd, now_iso()),
    )


def get_or_fetch_price(coingecko_id: str, date_ddmmyyyy: str) -> dict:
    """Returns {"status": "OK"|"RATE_LIMITED"|"ERROR"|"NOT_FOUND", "price_usd": float|None, "from_cache": bool}.

    Never re-queries a point that already succeeded (status == 'OK') on a
    prior run. A point cached as RATE_LIMITED/ERROR from a prior run IS
    retried (that status isn't terminal -- a 429 now doesn't mean 429
    forever), exactly mirroring the policy in
    app.source_discovery.enrich_missing_link_metadata.
    """
    with get_conn() as conn:
        cached = get_cached_price(conn, coingecko_id, date_ddmmyyyy)
    if cached is not None and cached["status"] == "OK":
        return {"status": "OK", "price_usd": cached["price_usd"], "from_cache": True}

    status, data = get_json_no_retry(
        f"{COINGECKO_BASE}/coins/{coingecko_id}/history",
        params={"date": date_ddmmyyyy, "localization": "false"},
    )
    price_usd = None
    final_status = status
    if status == "OK":
        price = (data or {}).get("market_data", {}).get("current_price", {}).get("usd")
        if price is None:
            final_status = "NOT_FOUND"
        else:
            price_usd = price

    with get_conn() as conn:
        record_price(conn, coingecko_id, date_ddmmyyyy, final_status, price_usd)

    return {"status": final_status, "price_usd": price_usd, "from_cache": False}


def get_or_fetch_price_series(
    coingecko_id: str,
    dates_ddmmyyyy: list,
    request_budget: Optional[int] = None,
    time_budget_seconds: Optional[float] = None,
    deadline: Optional[float] = None,
) -> dict:
    """Fetches a series of date points for one coin, honoring an optional
    request/time/deadline budget. A point skipped because the budget ran
    out is reported as 'DEFERRED (budget)' rather than silently omitted.
    Historical price completion is explicitly allowed to be partial --
    this must never be the thing that blocks the rest of source-coverage
    validation (Task 7).
    """
    t0 = time.monotonic()
    out = {}
    attempted = 0
    for date_str in dates_ddmmyyyy:
        if request_budget is not None and attempted >= request_budget:
            out[date_str] = {"status": "DEFERRED (budget)", "price_usd": None, "from_cache": False}
            continue
        if time_budget_seconds is not None and (time.monotonic() - t0) > time_budget_seconds:
            out[date_str] = {"status": "DEFERRED (budget)", "price_usd": None, "from_cache": False}
            continue
        if deadline is not None and time.monotonic() > deadline:
            out[date_str] = {"status": "DEFERRED (budget)", "price_usd": None, "from_cache": False}
            continue
        result = get_or_fetch_price(coingecko_id, date_str)
        if not result["from_cache"]:
            attempted += 1
        out[date_str] = result
    return out
