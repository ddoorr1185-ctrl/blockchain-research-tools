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
CREATE TABLE IF NOT EXISTS official_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coingecko_id TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- homepage | rss_feed | github | subreddit | announcement_page | forum
    url TEXT NOT NULL,
    verification_status TEXT NOT NULL,   -- VERIFIED | UNVERIFIED
    evidence TEXT NOT NULL,              -- provenance, e.g. 'coingecko_metadata:links.repos_url.github'
    monitorable INTEGER DEFAULT 0,       -- 1 if this URL was confirmed to be a working, pollable feed/endpoint
    last_verified_at TEXT NOT NULL,
    UNIQUE(coingecko_id, source_type, url)
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


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
