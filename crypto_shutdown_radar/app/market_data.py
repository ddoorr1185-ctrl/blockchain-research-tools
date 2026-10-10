"""Free market-data snapshot for an alert: price/1h/24h/7d from CoinGecko,
plus real perpetual-futures availability checks against exchange public
REST endpoints (not assumed from a spot listing).

Every field that cannot be retrieved is reported as the string "UNKNOWN" --
never fabricated -- per the project's hard requirement.
"""
import logging
import time

import requests

from app.config import (
    COINGECKO_BASE,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
    EXCHANGE_FUTURES_ENDPOINTS,
)

log = logging.getLogger("market_data")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

UNKNOWN = "UNKNOWN"

# Cache of exchange symbol sets so we don't re-fetch the full instrument
# list on every single alert. TTL-based, in-process only.
_futures_cache = {}
_FUTURES_CACHE_TTL_SECONDS = 15 * 60


def get_price_snapshot(coingecko_id: str) -> dict:
    try:
        resp = _session.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": coingecko_id,
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("price snapshot failed for %s: %s", coingecko_id, exc)
        return _unknown_snapshot()

    if resp.status_code != 200:
        return _unknown_snapshot()
    data = resp.json()
    if not data:
        return _unknown_snapshot()
    row = data[0]
    return {
        "price_usd": row.get("current_price", UNKNOWN),
        "change_1h_pct": row.get("price_change_percentage_1h_in_currency", UNKNOWN),
        "change_24h_pct": row.get("price_change_percentage_24h_in_currency", UNKNOWN),
        "change_7d_pct": row.get("price_change_percentage_7d_in_currency", UNKNOWN),
        "market_cap": row.get("market_cap", UNKNOWN),
        "volume_24h": row.get("total_volume", UNKNOWN),
        "retrieved_at_unknown": False,
    }


def _unknown_snapshot():
    return {
        "price_usd": UNKNOWN,
        "change_1h_pct": UNKNOWN,
        "change_24h_pct": UNKNOWN,
        "change_7d_pct": UNKNOWN,
        "market_cap": UNKNOWN,
        "volume_24h": UNKNOWN,
        "retrieved_at_unknown": True,
    }


def _fetch_exchange_symbols(exchange: str) -> set | None:
    """Returns the real set of perpetual/swap symbols currently listed on
    `exchange`, or None if the endpoint could not be reached/parsed. None
    must be treated as UNKNOWN, never as "no futures available".
    """
    cached = _futures_cache.get(exchange)
    if cached and (time.monotonic() - cached[0]) < _FUTURES_CACHE_TTL_SECONDS:
        return cached[1]

    url = EXCHANGE_FUTURES_ENDPOINTS.get(exchange)
    if not url:
        return None
    try:
        resp = _session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("futures endpoint failed for %s: %s", exchange, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    symbols = set()
    try:
        if exchange == "binance":
            for s in data.get("symbols", []):
                if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING":
                    symbols.add(s["baseAsset"].upper())
        elif exchange == "bybit":
            for s in data.get("result", {}).get("list", []):
                symbols.add(s["baseCoin"].upper())
        elif exchange == "okx":
            for s in data.get("data", []):
                if s.get("state") == "live":
                    symbols.add(s["ctValCcy"].upper())
        elif exchange == "bitget":
            for s in data.get("data", []):
                symbols.add(s["baseCoin"].upper())
        elif exchange == "gate":
            for s in data:
                name = s.get("name", "")  # e.g. "BTC_USDT"
                if "_" in name:
                    symbols.add(name.split("_")[0].upper())
    except (KeyError, TypeError) as exc:
        log.warning("unexpected %s futures response shape: %s", exchange, exc)
        return None

    _futures_cache[exchange] = (time.monotonic(), symbols)
    return symbols


def get_futures_availability(symbol: str) -> dict:
    """Checks each configured exchange for a live perpetual market in
    `symbol` (base asset, e.g. 'BTC'). Returns 'Available' / 'Unavailable' /
    'UNKNOWN' per exchange -- 'Unavailable' is only used when we actually
    retrieved the exchange's symbol list and the symbol was absent from it.
    """
    symbol = symbol.upper()
    availability = {}
    for exchange in EXCHANGE_FUTURES_ENDPOINTS:
        symbols = _fetch_exchange_symbols(exchange)
        if symbols is None:
            availability[exchange] = UNKNOWN
        else:
            availability[exchange] = "Available" if symbol in symbols else "Unavailable"
    return availability


def build_market_section(coingecko_id: str, symbol: str) -> dict:
    return {
        "price": get_price_snapshot(coingecko_id),
        "futures": get_futures_availability(symbol),
    }
