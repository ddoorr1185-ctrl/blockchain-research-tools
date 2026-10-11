"""SQLite persistence layer. Plain sqlite3, no ORM, so the schema is easy to audit."""
import sqlite3
import time
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    coingecko_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    market_cap_rank INTEGER,
    website TEXT,
    current_price REAL,
    market_cap REAL,
    last_updated TEXT,
    is_active INTEGER DEFAULT 1,
    first_seen_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS coin_rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coingecko_id TEXT NOT NULL,
    market_cap_rank INTEGER,
    market_cap REAL,
    price REAL,
    recorded_at TEXT NOT NULL
);

-- An "official account" can be an X handle, a blog RSS, etc. One coin can
-- have several (main project + subsidiaries + products), and several coins
-- can point at the same account (same company, multiple tokens).
CREATE TABLE IF NOT EXISTS official_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coingecko_id TEXT NOT NULL,
    platform TEXT NOT NULL,              -- 'x', 'rss', 'blog'
    handle_or_url TEXT NOT NULL,
    verification_status TEXT NOT NULL,   -- 'VERIFIED' | 'UNVERIFIED'
    verification_method TEXT,            -- how it was derived
    added_at TEXT NOT NULL,              -- first time this row was created
    last_verified_at TEXT,               -- most recent time this mapping was re-checked
    UNIQUE(coingecko_id, platform, handle_or_url)
);

-- Non-X official sources discovered per coin (homepage, blog/RSS, GitHub,
-- subreddit, announcement page, ...). Kept separate from official_accounts
-- (which is X-specific and predates this table) so existing X-mapping logic
-- is untouched. One coin can have several rows, one per source_type+url.
-- Caches the EXTRA CoinGecko /coins/{id} link fields (github, subreddit,
-- announcement, forum) beyond homepage/twitter -- which are already
-- persisted on coins.website and official_accounts respectively. This is
-- the fix for a real bug: the original 1000-coin enrichment run already
-- paid the CoinGecko rate-limit cost of fetching this data once, then
-- discarded everything except homepage+twitter. Without this cache, any
-- later step that wants github/subreddit/announcement info re-pays that
-- same cost -- which is exactly what caused repeated 429s. One row per
-- coin; fetch_status records whether we have real data or are still
-- missing/deferred/erroring, so a caller never has to guess.
CREATE TABLE IF NOT EXISTS coin_link_metadata (
    coingecko_id TEXT PRIMARY KEY,
    github_url TEXT,
    subreddit_url TEXT,
    announcement_url TEXT,
    forum_url TEXT,
    fetch_status TEXT NOT NULL,          -- OK | RATE_LIMITED | ERROR
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS official_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coingecko_id TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- homepage | rss_feed | github | subreddit | announcement_page | forum
    url TEXT NOT NULL,
    verification_status TEXT NOT NULL,   -- VERIFIED | UNVERIFIED
    evidence TEXT NOT NULL,              -- provenance: WHY it's considered official, e.g. 'coingecko_metadata:links.repos_url.github'
    monitorable INTEGER DEFAULT 0,       -- 1 only if this is a changing publication/update stream we can poll -- NOT just "page exists" (see app.source_discovery.DIRECTLY_MONITORABLE_TYPES)
    http_status INTEGER,                 -- the actual HTTP status observed when last checked, if any
    discovery_method TEXT,               -- HOW the (would-be) feed was found, e.g. 'declared in homepage <link rel=alternate>'
    last_verified_at TEXT NOT NULL,
    UNIQUE(coingecko_id, source_type, url)
);

-- The exact, deterministic 100-project validation sample (see
-- app.sample_selection), persisted so every re-run of the source-coverage
-- validation uses the identical set of coins rather than a fresh,
-- potentially-different sample each time.
CREATE TABLE IF NOT EXISTS validation_sample (
    sample_version TEXT NOT NULL,        -- e.g. 'v1' -- bump to intentionally rebuild
    coingecko_id TEXT NOT NULL,
    rank_at_selection INTEGER,
    stratum TEXT NOT NULL,               -- '1-100' | '101-300' | '301-600' | '601-1000'
    selected_at TEXT NOT NULL,
    PRIMARY KEY (sample_version, coingecko_id)
);

-- Caches one historical price lookup (coin + specific calendar date) so a
-- later run of the historical-event re-validation never re-queries a
-- point it already successfully fetched, and a 429 on one date doesn't
-- block the others.
CREATE TABLE IF NOT EXISTS historical_price_cache (
    coingecko_id TEXT NOT NULL,
    date_ddmmyyyy TEXT NOT NULL,
    status TEXT NOT NULL,                -- OK | RATE_LIMITED | ERROR | NOT_FOUND
    price_usd REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (coingecko_id, date_ddmmyyyy)
);

-- Cooldown tracking for free X-fallback candidate methods (xcancel,
-- Nitter mirrors, RSSHub, ...), so a validation run doesn't keep
-- re-testing a method that just failed minutes/hours ago.
CREATE TABLE IF NOT EXISTS x_fallback_method_health (
    method_key TEXT PRIMARY KEY,
    last_tested_at TEXT NOT NULL,
    last_success INTEGER NOT NULL,       -- 1 | 0
    consecutive_failures INTEGER DEFAULT 0
);

-- Per-account monitoring health/state, used by the scheduler to avoid
-- claiming a check happened when it didn't.
CREATE TABLE IF NOT EXISTS account_monitor_state (
    account_id INTEGER PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_post_id_seen TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    status TEXT DEFAULT 'UNKNOWN',       -- OK | DEGRADED | UNREACHABLE | UNKNOWN
    last_error TEXT,
    FOREIGN KEY(account_id) REFERENCES official_accounts(id)
);

CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,           -- 'x' | 'rss_news' | 'rss_announcement'
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,           -- post id / guid, used for de-dup
    author TEXT,
    coingecko_id TEXT,                   -- best-guess related coin, nullable
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT,
    collection_status TEXT NOT NULL,     -- 'OK' | 'PARTIAL' | 'ERROR'
    UNIQUE(source_type, source_name, external_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id INTEGER,
    event_type TEXT NOT NULL,            -- A..J classification code
    event_label TEXT NOT NULL,
    severity TEXT NOT NULL,              -- CRITICAL | HIGH | MEDIUM | NONE
    confidence REAL NOT NULL,
    source_credibility REAL NOT NULL,
    affected_entity TEXT,
    affected_scope TEXT,                 -- organization | token | subsidiary | product | unrelated
    coingecko_id TEXT,
    evidence TEXT NOT NULL,              -- matched snippet(s), JSON-ish text
    event_timestamp TEXT,
    detection_timestamp TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    FOREIGN KEY(raw_item_id) REFERENCES raw_items(id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    severity_at_send TEXT NOT NULL,
    telegram_ok INTEGER NOT NULL,
    telegram_error TEXT,
    market_snapshot_json TEXT,
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_key TEXT PRIMARY KEY,         -- e.g. 'rss:CoinDesk' or 'x:profile_scrape'
    last_attempt_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    status TEXT DEFAULT 'UNKNOWN',
    last_error TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    job_name TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_status TEXT,
    next_due_at TEXT,
    current_interval_minutes REAL,
    backoff_level INTEGER DEFAULT 0
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Additive, idempotent migrations for databases created before a given
    column/table existed (e.g. a pre-existing data/radar.db from an earlier
    run). CREATE TABLE IF NOT EXISTS in SCHEMA already handles new tables;
    this only needs to handle columns added to a table that already existed.
    """
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(official_accounts)").fetchall()}
    if "last_verified_at" not in existing_cols:
        conn.execute("ALTER TABLE official_accounts ADD COLUMN last_verified_at TEXT")

    source_cols = {row["name"] for row in conn.execute("PRAGMA table_info(official_sources)").fetchall()}
    if "http_status" not in source_cols:
        conn.execute("ALTER TABLE official_sources ADD COLUMN http_status INTEGER")
    if "discovery_method" not in source_cols:
        conn.execute("ALTER TABLE official_sources ADD COLUMN discovery_method TEXT")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
