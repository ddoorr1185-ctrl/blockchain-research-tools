# Crypto Shutdown Radar

A free, local-only monitoring system that watches for cryptocurrency
projects/exchanges/protocols announcing a **permanent shutdown,
bankruptcy, insolvency, or winding down** — structural events that can
depress a token's price for days or weeks, as opposed to routine
incidents (hacks, outages, maintenance).

**Zero recurring cost.** Everything runs on your own machine, against
free/keyless public APIs: CoinGecko (market data), public RSS feeds
(news + exchange announcements), the Telegram Bot API (free), and (best
effort — see Limitations) unofficial free X/Twitter read methods. No
paid API keys, no paid hosting, no paid AI calls are used anywhere.

## What's actually built vs. what's aspirational

This was built against the full spec in phases, with real tests at each
step (38 unit/integration tests + 3 real-world-event validation tests, all
passing — see `pytest` output below). One limitation is structural and
**could not be worked around**: the sandboxed container this was
developed in blocks outbound network access to everything except a small
allowlist (PyPI, GitHub, Anthropic). CoinGecko, X/Twitter, Telegram, and
every exchange API returned `403` at the network-policy layer, before any
application code ran. That is **not** a bug in this code — it's documented
in `/root/.ccr/README.md` in that environment. It means:

- All logic that depends on real external responses (universe refresh,
  account mapping, RSS collection, X collection, market-data snapshots,
  Telegram delivery) is implemented to make real HTTP calls and is
  covered by tests that mock those calls realistically (including
  failure modes) — but could not be exercised against the live internet
  from the build environment.
- You **must** run `python -m scripts.feasibility_check` yourself, on a
  normal network, before trusting any "real coverage" number. Instructions
  below.

## Architecture

```
app/
  config.py        central settings (poll intervals, RSS source list, etc.)
  db.py             SQLite schema (coins, accounts, raw_items, events, alerts, source_health, scheduler_state)
  universe.py       CoinGecko top-1000 ingestion + X-handle mapping (VERIFIED/UNVERIFIED)
  sources/
    base.py         SourceAdapter / CollectedItem / CollectionResult interfaces
    x_adapter.py     free X access attempts (Nitter mirrors, syndication widget endpoint)
    rss_adapter.py   generic RSS/Atom adapter (news + exchange announcements)
  classifier.py      rule-based hybrid classifier (keywords, negation, rumor, scope, A-J types)
  market_data.py     price/1h/24h/7d + real per-exchange futures-availability checks
  telegram_bot.py    free Telegram Bot API alert delivery + dedup/escalation
  pipeline.py        glues sources -> dedup -> classify -> events -> alerts
  scheduler.py        persistent adaptive-interval background scheduler
  dashboard/          FastAPI + Jinja2 local web UI
scripts/
  feasibility_check.py   RUN THIS FIRST on a real network
  init_db.py
  refresh_universe.py
  run.py                  starts the scheduler (blocking)
tests/                    38 tests (classifier scenarios, dedup, source
                           outage handling, market-data UNKNOWN handling,
                           Telegram delivery, scheduler backoff/persistence,
                           universe mapping) + 3 historical-event tests
```

## Installation

```bash
cd crypto_shutdown_radar
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scripts.init_db
```

## Step 1 — verify real free-source feasibility (do this before anything else)

```bash
python -m scripts.feasibility_check
```

This makes real requests to the configured RSS feeds and attempts both
free X-access strategies against a handful of well-known accounts. It
prints exactly what worked and what didn't — no fabricated results. Expect:

- **RSS/news/announcement feeds: likely mostly working.** These are
  normal public RSS endpoints with no anti-bot measures.
- **X/Twitter free access: likely unreliable or 0%.** X has had no free
  read API since 2023; the unofficial fallbacks here (public Nitter
  mirrors, the syndication widget endpoint used for embedded tweets) are
  known to be fragile and can be blocked by X at any time, for any
  account. If this comes back at 0%, the system still works — it just
  means your real-world coverage is driven by RSS/news/exchange-announcement
  sources, not X, until a more durable free method appears. The dashboard's
  "Source Health" and "Monitored X Accounts" pages always show the true,
  current number — never a fabricated one.

## Step 2 — build the coin universe

```bash
# Fast: market data for all 1000 coins, skip account mapping
python -m scripts.refresh_universe --no-details

# Full: also attempts X-handle mapping for every coin (CoinGecko's
# per-coin detail endpoint is rate-limited on the free tier, so this
# takes roughly 35-40 minutes for 1000 coins)
python -m scripts.refresh_universe

# Partial, for a quick test:
python -m scripts.refresh_universe --details-limit 50
```

## Step 3 — set up Telegram alerts (free)

1. Open Telegram, message **@BotFather**, send `/newbot`, follow the
   prompts, and copy the bot token it gives you.
2. Send any message to your new bot (or add it to a group and send a
   message there).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
   browser and find the `"chat":{"id": ...}` value.
4. Export both values before starting the scheduler:
   ```bash
   export CSR_TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
   export CSR_TELEGRAM_CHAT_ID="123456789"
   ```

If these are unset, the system still runs and still records every
detected event in the dashboard — it just logs (and records in the
`alerts` table) that Telegram delivery was skipped, rather than pretending
to send anything.

## Follow-up: source-coverage validation (non-X source discovery + X fallback research)

After the first full run above has populated `data/radar.db` (top-1000
universe + X-handle mapping), a second, much faster validation is
available: `bash run_source_coverage_validation.sh`. It:

- **Never re-runs the 1000-coin CoinGecko detail fetch.** It reads the
  existing cache and fails fast with instructions if no cache is found.
- Discovers non-X official sources (homepage, blog/RSS feed, GitHub
  activity feed, subreddit feed) for a 100-coin sample, with every entry's
  provenance recorded in the `official_sources` table.
- Tests new, previously-untried free X-reading candidates (xcancel.com,
  two additional Nitter-codebase mirrors, RSSHub's public demo instance,
  and optionally your own self-hosted RSS-Bridge via
  `CSR_RSSBRIDGE_BASE_URL`) against 5 well-known accounts.
- Re-validates 10 real historical crypto shutdown/bankruptcy/wind-down
  cases against real CoinGecko price history.
- Targets ≤30 minutes total and writes `SOURCE_COVERAGE_VALIDATION.md`.

## Step 4 — run it

```bash
# Terminal 1: the scheduler (RSS/X polling every ~10-30 min, adaptive;
# full universe refresh daily)
python -m scripts.run

# Terminal 2: the dashboard
uvicorn app.dashboard.main:app --port 8000
```

Open http://localhost:8000 — Overview, Coins, X Accounts, Events,
Alerts, Source Health, and Config pages.

Both processes read/write the same `data/radar.db` SQLite file, so state
(coins, accounts, events, alert history, scheduler backoff state) survives
restarts of either process.

## Running the tests

```bash
pip install httpx   # only needed for the dashboard smoke test below
python -m pytest tests/ -v
```

All 41 tests passed in this build (38 unit/integration + 3 historical
real-event validation tests), covering: bankruptcy, permanent shutdown,
exchange closure, protocol termination, product discontinuation (scoped
down, non-actionable), temporary outages (correctly NOT flagged),
negated/denied shutdowns (correctly NOT flagged), rumors (MEDIUM, not
confirmed), historical references (not flagged as current), unrelated
companies (not flagged), subsidiary scoping, duplicate-announcement
dedup, source-outage honesty (never silently reported as "0 new items"),
missing/unreachable market data (`UNKNOWN`, never fabricated), futures
availability (never assumed from a spot listing), Telegram delivery
success/failure recording, no-repeat-alert-unless-severity-increases, and
scheduler backoff + persistence across a simulated restart.

## Real-world validation performed during this build

Run from the build sandbox (network-restricted — see above):

- ✅ Database schema creates and persists correctly; data survives a
  simulated process restart.
- ✅ Dashboard starts and all 7 pages (`/`, `/coins`, `/accounts`,
  `/events`, `/alerts`, `/sources`, `/config`) return HTTP 200, both
  empty and populated with synthetic data.
- ✅ Scheduler builds its persistent job store without duplicating jobs
  across two separate "process starts" against the same DB file.
- ✅ Classifier test suite (38 tests) passes, covering every category in
  section 10 of the spec, using fixtures explicitly labeled as synthetic.
- ✅ Two real 2026 crypto-shutdown events (Blast/BLAST L2 network winding
  down due to unsustainable costs; BitMart/BMX exchange wind-down) were
  looked up via web search, confirmed as **voluntary wind-downs, not
  bankruptcy filings** (an important distinction this project's own event
  taxonomy requires), and paraphrased test cases built from that research
  correctly classify as CRITICAL protocol-termination / exchange-closure
  events rather than being miscategorized as bankruptcy.
- ✅ Price/futures/X/RSS code paths were exercised against the *real*
  internet and *correctly* returned `UNKNOWN`/failure states when the
  sandbox's network policy blocked the destination (proof the
  fail-safe paths work, not proof the happy paths work on an open network).
- ❌ **Not verified from this environment** (network policy blocks it):
  CoinGecko universe refresh against live data, real X-account reachability,
  real exchange futures-symbol lookups, real Telegram message delivery.
  **You must run `scripts/feasibility_check.py` yourself on a normal
  network before trusting those numbers.**

## Known limitations (read before relying on this for decisions)

1. **Free X/Twitter monitoring is inherently unreliable.** X's official
   API has had no free read tier since 2023. The fallbacks here (Nitter
   mirrors, the syndication widget endpoint) are unofficial, can break or
   be blocked at any time without notice, and were not verifiable from
   the build sandbox. Treat RSS/news/exchange-announcement sources as the
   reliable backbone, and X coverage as a bonus that may be 0% at any
   given time. The dashboard reports true, not assumed, coverage.
2. **No single free source covers 1,000 accounts reliably at a fixed
   10-30 minute cadence.** The scheduler is adaptive: intervals widen
   automatically under repeated failures. Expect real cadence to vary
   source-by-source rather than being a fixed guarantee.
3. **The classifier is rule-based, not an LLM.** It is transparent and
   reproducible, but it will miss paraphrased announcements its patterns
   don't cover and can be fooled by unusual phrasing. It is tuned to avoid
   false CRITICALs (e.g. explicit denial/negation handling, rumor
   downgrading) more than it is tuned for perfect recall.
4. **Severity for subsidiary/scoped events is currently conservative.**
   A bankruptcy of a named subsidiary from a primary source is still
   scored CRITICAL under the current rules (see
   `test_subsidiary_bankruptcy_scoped_to_subsidiary`); the scope is
   recorded on the event for manual review, but automatic downgrading of
   subsidiary-only events is a possible future refinement, not yet done.
5. **Event deduplication** groups items by `(coin, source_type, day)`,
   which is a reasonable approximation, not perfect entity resolution —
   two unrelated announcements about the same coin on the same day would
   currently collapse into one event.
6. **CoinGecko's free tier is itself not contractually guaranteed to stay
   free or at this rate limit indefinitely.** If it changes terms, this
   system will start reporting failures via Source Health rather than
   silently breaking — but someone should keep an eye on it.

## Confirmation: recurring monetary cost is zero

Every dependency is open-source (FastAPI, SQLite, APScheduler, feedparser,
requests) or a free public API tier (CoinGecko, Telegram Bot API, public
RSS feeds, public exchange REST endpoints used read-only). There is no
paid service, API key, or subscription anywhere in this codebase.
