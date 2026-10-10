"""Central configuration. Everything here is free-tier / no-cost by construction."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = os.environ.get("CSR_DB_PATH", str(DATA_DIR / "radar.db"))

# CoinGecko public API (no key required for this usage level).
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_MIN_INTERVAL_SECONDS = 2.0  # stay under the free ~30 req/min limit

UNIVERSE_SIZE = 1000
UNIVERSE_PAGE_SIZE = 250  # CoinGecko max per_page
UNIVERSE_REFRESH_HOURS = 24

# Target collection cadence. Actual cadence is adaptive (see scheduler.py) and
# will fall back to slower polling under rate limits or repeated failures.
TARGET_POLL_MINUTES = 15
MIN_POLL_MINUTES = 10
MAX_POLL_MINUTES = 30

TELEGRAM_BOT_TOKEN = os.environ.get("CSR_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("CSR_TELEGRAM_CHAT_ID", "")

HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "CryptoShutdownRadar/0.1 (personal, non-commercial, free-tier monitoring; contact: operator)"

# Free RSS/news sources used to supplement X monitoring.
NEWS_RSS_SOURCES = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "credibility": 0.85},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "credibility": 0.75},
    {"name": "TheBlock", "url": "https://www.theblock.co/rss.xml", "credibility": 0.85},
    {"name": "Decrypt", "url": "https://decrypt.co/feed", "credibility": 0.75},
    {"name": "CryptoPanic-Rising", "url": "https://cryptopanic.com/news/rss/", "credibility": 0.5},
]

# Free exchange/public-status announcement feeds (primary sources, higher
# credibility than secondary news coverage of the same event).
ANNOUNCEMENT_RSS_SOURCES = [
    # Binance publishes a public RSS feed for its announcement center.
    {"name": "Binance Announcements", "url": "https://www.binance.com/en/support/announcement/rss", "credibility": 0.95},
]

# Exchange public REST endpoints used ONLY to check whether a perpetual
# futures market exists for a symbol. All are free, keyless, public endpoints.
EXCHANGE_FUTURES_ENDPOINTS = {
    "binance": "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "bybit": "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000",
    "okx": "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
    "bitget": "https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures",
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/contracts",
}
