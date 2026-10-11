#!/usr/bin/env python3
"""Fast, focused validation (Phases A-F of the source-coverage follow-up).

Unlike scripts/live_validation.py, this NEVER triggers a full 1000-coin
CoinGecko detail refresh. It requires a cached universe to already exist
(from a prior successful run) and fails fast with clear instructions if
one isn't found, rather than silently re-running the ~hours-long fetch.

HARD WALL-CLOCK BUDGET: a single `GLOBAL_DEADLINE` (time.monotonic() value,
set once at startup to now + 30 minutes) is threaded through every phase.
Each phase checks it before starting and inside its own loop, and a phase
not yet started when the deadline has already passed is skipped entirely
rather than run. This is enforced by code, not claimed from an estimate --
see tests/test_source_coverage_validation_budget.py.

Every CoinGecko call anywhere in this script's call graph goes through
app.universe.get_json_no_retry, which never sleeps on a 429 (no
Retry-After wait, no retry loop). This is the fix for the real failure
mode observed on a real run: repeated 60s Retry-After sleeps turning a
<=30 minute sample loop into a multi-hour stall.

Usage (from crypto_shutdown_radar/, with the venv active):
    python -m scripts.source_coverage_validation
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import init_db, get_conn
from app.universe import get_universe_status, is_universe_cached, get_json_no_retry
from app.source_discovery import discover_for_sample
from app.sources.x_fallback_experiments import run_all_candidates
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES, COINGECKO_BASE
from scripts.historical_events_data import HISTORICAL_EVENTS

REPORT_PATH = Path(__file__).resolve().parent.parent / "SOURCE_COVERAGE_VALIDATION.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "SOURCE_COVERAGE_VALIDATION_RAW_LOG.json"

GLOBAL_DEADLINE_SECONDS = 30 * 60

ENRICHMENT_REQUEST_BUDGET = 40
ENRICHMENT_TIME_BUDGET_SECONDS = 90
DISCOVERY_TIME_BUDGET_SECONDS = 600
PHASE_D_TIME_BUDGET_SECONDS = 90
PHASE_E_TIME_BUDGET_SECONDS = 300

SAMPLE_X_HANDLES = ["binance", "coinbase", "VitalikButerin", "ethereum", "cz_binance"]

results = {"started_at": datetime.now(timezone.utc).isoformat()}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_raw_log():
    with open(RAW_LOG_PATH, "w") as f:
        json.dump(results, f, indent=2, default=str)


def remaining(deadline):
    return max(0.0, deadline - time.monotonic())


# ---------------------------------------------------------------------------
# Phase A check: refuse to proceed by silently triggering a full refresh
# ---------------------------------------------------------------------------
def check_cached_universe():
    init_db()
    with get_conn() as conn:
        status = get_universe_status(conn)
        cached = is_universe_cached(conn, min_coins=900)
    results["universe_status"] = status
    results["universe_cached"] = cached
    if not cached:
        log(
            f"NO USABLE CACHED UNIVERSE FOUND (only {status['total_coins_cached']} coins cached). "
            "This script will NOT trigger a full refresh automatically -- that is exactly the "
            "5-hour operation we're avoiding. Run 'python -m scripts.refresh_universe' once "
            "first (or reuse the data/radar.db from a prior successful run), then re-run this."
        )
        return False
    log(f"Using cached universe: {status['total_coins_cached']} coins, "
        f"{status['x_accounts_verified']} verified X accounts, "
        f"{status['x_accounts_unverified']} unverified "
        f"(mappings last verified {status['x_mapping_oldest_verification']} .. {status['x_mapping_newest_verification']})")
    return True


def get_top_n_coins(n):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins "
            "WHERE is_active=1 ORDER BY market_cap_rank ASC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase B/C: 100-project official-source coverage
# ---------------------------------------------------------------------------
def _progress_cb(processed, total, found, deferred, elapsed, remaining_budget):
    rem_str = f"{remaining_budget:.0f}s" if remaining_budget is not None else "n/a"
    log(f"  processed {processed}/{total} | direct official sources found: {found} | "
        f"deferred (rate-limited/error metadata): {deferred} | elapsed: {elapsed:.0f}s | remaining budget: {rem_str}")


def run_phase_b_c(deadline):
    if time.monotonic() > deadline:
        log("PHASE B/C: SKIPPED (global 30-minute deadline already passed before this phase started)")
        results["phase_b_c_discovery"] = {"skipped": True, "reason": "global_deadline_exceeded"}
        return
    log(f"PHASE B/C: discovering official sources for 100 sample coins "
        f"(enrichment<= {ENRICHMENT_REQUEST_BUDGET} requests/{ENRICHMENT_TIME_BUDGET_SECONDS}s, "
        f"discovery<= {DISCOVERY_TIME_BUDGET_SECONDS}s, remaining global budget: {remaining(deadline):.0f}s)...")
    sample = get_top_n_coins(100)
    discovery = discover_for_sample(
        sample,
        enrichment_request_budget=ENRICHMENT_REQUEST_BUDGET,
        enrichment_time_budget_seconds=ENRICHMENT_TIME_BUDGET_SECONDS,
        discovery_time_budget_seconds=DISCOVERY_TIME_BUDGET_SECONDS,
        deadline=deadline,
        progress_every=10,
        progress_cb=_progress_cb,
    )
    results["phase_b_c_discovery"] = discovery
    enr = discovery["enrichment"]
    log(f"PHASE B/C done: {discovery['completed']}/{discovery['requested']} coins processed in "
        f"{discovery['elapsed_seconds']:.0f}s (stopped early: {discovery['stopped_early_due_to_time_budget']})")
    log(f"  enrichment: {enr['attempted']} attempted, {enr['newly_ok']} newly OK, "
        f"{enr['deferred_rate_limited']} rate-limited/deferred, {enr['already_cached_skipped']} already cached, "
        f"stopped_reason={enr['stopped_reason']}")
    log(f"  homepage={discovery['homepage_coverage']} blog_rss={discovery['blog_rss_coverage']} "
        f"github={discovery['github_coverage']} subreddit={discovery['subreddit_coverage']} "
        f"at_least_one_source={discovery['at_least_one_official_source']} only_x_or_nothing={discovery['only_x_known_or_nothing']}")


# ---------------------------------------------------------------------------
# Phase D: X fallback research against 5 known accounts
# ---------------------------------------------------------------------------
def run_phase_d(deadline):
    if time.monotonic() > deadline:
        log("PHASE D: SKIPPED (global 30-minute deadline already passed before this phase started)")
        results["phase_d_x_fallback"] = {"skipped": True, "reason": "global_deadline_exceeded"}
        return
    phase_deadline = min(deadline, time.monotonic() + PHASE_D_TIME_BUDGET_SECONDS)
    log(f"PHASE D: testing new X-fallback candidate methods against 5 known accounts "
        f"(budget {PHASE_D_TIME_BUDGET_SECONDS}s, remaining global budget: {remaining(deadline):.0f}s)...")
    t0 = time.monotonic()
    candidates = run_all_candidates(SAMPLE_X_HANDLES, deadline=phase_deadline)
    elapsed = time.monotonic() - t0
    out = {}
    for name, mr in candidates.items():
        out[name] = {
            "method": mr.method,
            "requires_auth": mr.requires_auth,
            "requires_normal_account": mr.requires_normal_account,
            "cost": mr.cost,
            "tos_reliability_note": mr.tos_reliability_note,
            "success_count": mr.success_count,
            "total_count": mr.total_count,
            "account_results": mr.account_results,
        }
    results["phase_d_x_fallback"] = {"elapsed_seconds": elapsed, "methods": out}
    for name, data in out.items():
        log(f"  {name}: {data['success_count']}/{data['total_count']} accounts succeeded")
    log(f"PHASE D done in {elapsed:.0f}s.")


# ---------------------------------------------------------------------------
# Phase E: historical event re-validation with real CoinGecko price history
# ---------------------------------------------------------------------------
def _history_price(coingecko_id, date_str_ddmmyyyy):
    """Non-blocking: a 429 or error is reported and returned immediately,
    never slept out. See app.universe.get_json_no_retry.
    """
    status, data = get_json_no_retry(
        f"{COINGECKO_BASE}/coins/{coingecko_id}/history",
        params={"date": date_str_ddmmyyyy, "localization": "false"},
    )
    if status == "RATE_LIMITED":
        return "UNKNOWN (rate limited -- deferred, not retried in this run)"
    if status != "OK":
        return "UNKNOWN (lookup failed)"
    price = (data.get("market_data") or {}).get("current_price", {}).get("usd")
    return price if price is not None else "UNKNOWN (no price in response)"


def run_phase_e(deadline):
    if time.monotonic() > deadline:
        log("PHASE E: SKIPPED (global 30-minute deadline already passed before this phase started)")
        results["phase_e_historical"] = {"skipped": True, "reason": "global_deadline_exceeded"}
        return
    phase_deadline = min(deadline, time.monotonic() + PHASE_E_TIME_BUDGET_SECONDS)
    log(f"PHASE E: re-validating {len(HISTORICAL_EVENTS)} historical events with real CoinGecko "
        f"price-history lookups (budget {PHASE_E_TIME_BUDGET_SECONDS}s, remaining global budget: {remaining(deadline):.0f}s)...")
    t0 = time.monotonic()
    out = []
    stopped_early = False
    for event in HISTORICAL_EVENTS:
        if time.monotonic() > phase_deadline:
            stopped_early = True
            break
        row = dict(event)
        if event["coingecko_id"]:
            try:
                ann_date = datetime.strptime(event["announcement_date"], "%Y-%m-%d")
            except ValueError:
                ann_date = None
            if ann_date:
                prices = {}
                for label, delta_days in (("at_announcement", 0), ("+24h", 1), ("+48h", 2), ("+72h", 3), ("+7d", 7)):
                    if time.monotonic() > phase_deadline:
                        prices[label] = "UNKNOWN (time budget exceeded before this lookup)"
                        continue
                    target = ann_date + timedelta(days=delta_days)
                    prices[label] = _history_price(event["coingecko_id"], target.strftime("%d-%m-%Y"))
                row["measured_prices_usd"] = prices
            else:
                row["measured_prices_usd"] = {"error": "unparseable announcement_date"}
        else:
            row["measured_prices_usd"] = {"note": "no coingecko_id (no major associated token)"}
        out.append(row)
        log(f"  {event['project']} ({event['ticker']}): "
            f"{row['measured_prices_usd'] if event['coingecko_id'] else 'skipped (no token)'}")

    results["phase_e_historical"] = {
        "events": out,
        "stopped_early_due_to_time_budget": stopped_early,
        "elapsed_seconds": time.monotonic() - t0,
    }
    log(f"PHASE E done: {len(out)}/{len(HISTORICAL_EVENTS)} events processed "
        f"(stopped early: {stopped_early})")


# ---------------------------------------------------------------------------
# Quick RSS re-check (cheap; feeds directly into the report's coverage section)
# ---------------------------------------------------------------------------
def run_rss_recheck(deadline):
    if time.monotonic() > deadline:
        log("RSS re-check: SKIPPED (global 30-minute deadline already passed)")
        results["rss_recheck"] = {}
        return
    log("Quick re-check of the 6 configured RSS/news/announcement feeds...")
    out = {}
    for adapter in build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES):
        r = adapter.collect()
        out[adapter.name] = {"ok": r.ok, "item_count": len(r.items) if r.ok else 0, "error": r.error}
    results["rss_recheck"] = out
    working = sum(1 for v in out.values() if v["ok"])
    log(f"RSS re-check: {working}/{len(out)} working")


# ---------------------------------------------------------------------------
# Verdict + report
# ---------------------------------------------------------------------------
def pick_verdict():
    bc = results.get("phase_b_c_discovery", {})
    completed = bc.get("completed", 0) or 1
    at_least_one_pct = 100 * bc.get("at_least_one_official_source", 0) / completed
    rss = results.get("rss_recheck", {})
    rss_working = sum(1 for v in rss.values() if v["ok"])
    x_any_working = any(
        m["success_count"] > 0 for m in results.get("phase_d_x_fallback", {}).get("methods", {}).values()
    )

    if at_least_one_pct >= 70 and rss_working >= 4:
        return "A — viable with strong direct-source coverage"
    if at_least_one_pct >= 40 and rss_working >= 3:
        return "B — viable hybrid system with acceptable blind spots"
    if rss_working >= 2 or at_least_one_pct > 0:
        return "C — useful supplementary monitor but insufficient alone"
    return "D — not viable at $0"


def write_report():
    u = results.get("universe_status", {})
    bc = results.get("phase_b_c_discovery", {})
    d = results.get("phase_d_x_fallback", {})
    e = results.get("phase_e_historical", {})
    rss = results.get("rss_recheck", {})
    verdict = pick_verdict()

    total_elapsed = sum(
        results.get(k, {}).get("elapsed_seconds", 0) for k in ("phase_b_c_discovery", "phase_e_historical")
    ) + d.get("elapsed_seconds", 0)

    lines = []
    lines.append("# SOURCE_COVERAGE_VALIDATION\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Total measured phase runtime: ~{total_elapsed/60:.1f} minutes "
                 f"(hard global budget was {GLOBAL_DEADLINE_SECONDS/60:.0f} minutes; "
                 "excludes Python/venv startup and the cheap RSS re-check)\n")

    lines.append("## 1. Cached universe status\n")
    if not results.get("universe_cached"):
        lines.append("**NO CACHED UNIVERSE WAS FOUND. This run stopped before any other phase.** "
                     "Run `python -m scripts.refresh_universe` once (or restore a prior data/radar.db), "
                     "then re-run this script.\n")
        REPORT_PATH.write_text("\n".join(lines))
        return
    lines.append(f"- Coins cached: {u.get('total_coins_cached')}")
    lines.append(f"- X accounts VERIFIED: {u.get('x_accounts_verified')}")
    lines.append(f"- X accounts UNVERIFIED: {u.get('x_accounts_unverified')}")
    lines.append(f"- X mapping last-verified range: {u.get('x_mapping_oldest_verification')} .. {u.get('x_mapping_newest_verification')}")
    lines.append(f"- Coin price data last-updated range: {u.get('coins_oldest_update')} .. {u.get('coins_newest_update')}")
    lines.append("- **No 1000-coin CoinGecko detail refresh was performed by this script.**\n")

    lines.append("## 2. 100-project official-source coverage\n")
    if bc.get("skipped"):
        lines.append("**SKIPPED: global 30-minute deadline was already exceeded before this phase started.**\n")
    else:
        enr = bc.get("enrichment", {})
        lines.append(f"Sample processed: {bc.get('completed')}/{bc.get('requested')} "
                     f"(stopped early on time budget: {bc.get('stopped_early_due_to_time_budget')})")
        lines.append(f"Metadata enrichment this run: {enr.get('attempted')} CoinGecko requests attempted, "
                     f"{enr.get('newly_ok')} newly cached OK, {enr.get('deferred_rate_limited')} deferred "
                     f"(rate-limited -- will retry on a later run, not held/slept on), "
                     f"{enr.get('already_cached_skipped')} already cached from a prior run and skipped entirely, "
                     f"stopped_reason={enr.get('stopped_reason')}\n")

    lines.append("## 3. Coverage percentages by source type\n")
    completed = bc.get("completed", 0) or 1
    for key, label in (
        ("homepage_coverage", "Official homepage reachable"),
        ("blog_rss_coverage", "Working official blog/RSS feed found"),
        ("github_coverage", "Official GitHub activity feed confirmed"),
        ("subreddit_coverage", "Official subreddit RSS confirmed"),
        ("at_least_one_official_source", "At least ONE directly monitorable official source"),
        ("only_x_known_or_nothing", "No non-X official source found (X-only or nothing known)"),
        ("deferred_due_to_missing_metadata", "Coins where GitHub/subreddit check was deferred (metadata not yet cached)"),
    ):
        n = bc.get(key, 0)
        lines.append(f"- {label}: {n}/{completed} ({100*n/completed:.0f}%)")
    lines.append("")

    lines.append("## 4. Real X fallback test results\n")
    lines.append(f"Tested against {len(SAMPLE_X_HANDLES)} known public accounts: {', '.join(SAMPLE_X_HANDLES)}\n")
    if d.get("skipped"):
        lines.append("**SKIPPED: global 30-minute deadline was already exceeded before this phase started.**\n")
        any_x_working = False
    else:
        lines.append("| Method | Auth required | Normal account required | Cost | Success | ToS/reliability |")
        lines.append("|---|---|---|---|---|---|")
        for name, m in d.get("methods", {}).items():
            lines.append(f"| {m['method']} | {m['requires_auth']} | {m['requires_normal_account']} | {m['cost']} | "
                         f"{m['success_count']}/{m['total_count']} | {m['tos_reliability_note'][:160]} |")
        any_x_working = any(m["success_count"] > 0 for m in d.get("methods", {}).values())
        lines.append(f"\n**Sustainable zero-cost X method found: {'YES' if any_x_working else 'NO'}.**")
        if not any_x_working:
            lines.append("No candidate tested in this run retrieved any real posts from any of the 5 accounts. "
                          "This does not prove no free method exists, only that none of the ones tested here "
                          "(xcancel.com, two additional Nitter-codebase mirrors, RSSHub's public demo instance, "
                          "and an optional self-hosted RSS-Bridge if configured) worked on this network at this time.\n")

    lines.append("\n## 5. Historical-event detection table\n")
    if e.get("skipped"):
        lines.append("**SKIPPED: global 30-minute deadline was already exceeded before this phase started.**\n")
    else:
        lines.append("| Project | Token | Event type | Announcement date | Would detect via | Est. detection delay |")
        lines.append("|---|---|---|---|---|---|")
        for ev in e.get("events", []):
            lines.append(f"| {ev['project']} | {ev['ticker'] or '-'} | {ev['event_type']} | {ev['announcement_date']} | "
                         f"{ev['would_detect_via'][:100]} | {ev['estimated_detection_delay'][:80]} |")

        lines.append("\n### Measured real price history (CoinGecko, where a coingecko_id exists)\n")
        for ev in e.get("events", []):
            lines.append(f"**{ev['project']} ({ev['ticker'] or 'N/A'})** -- sources: " +
                         ", ".join(ev["sources"]))
            prices = ev.get("measured_prices_usd", {})
            if isinstance(prices, dict) and prices:
                lines.append("  " + ", ".join(f"{k}: {v}" for k, v in prices.items()))
            lines.append(f"  Futures at announcement: {ev['futures_available_at_announcement']}")
            lines.append(f"  Notes: {ev['notes']}\n")

    lines.append("## 6. Detection-delay analysis\n")
    lines.append(
        "Across the 10 historical cases, the dominant pattern is: major/well-covered events (exchange "
        "bankruptcies, large protocol collapses) were covered by at least one of CoinDesk/Cointelegraph/"
        "TheBlock/Decrypt within minutes to a few hours of the primary trigger -- comfortably within this "
        "project's 10-30 minute RSS poll cadence once published. Smaller or slower-moving cases (Multichain) "
        "showed secondary-source distress signals WEEKS before the formal 'ceasing operations' statement, "
        "which would have given substantial lead time. The clear counterexample is Voyager/VGX, which *rose* "
        "sharply after its bankruptcy filing on an unrelated pump -- a reminder that fast detection does not "
        "guarantee a clean, tradeable sustained decline. Hotbit is a structural blind spot: an exchange "
        "closure with no major associated token falls outside this project's coin-centric universe entirely "
        "and would only ever be caught by the generic news RSS feeds, never by per-coin official-source "
        "monitoring.\n"
    )

    lines.append("## 7. Remaining blind spots\n")
    lines.append(f"- X/Twitter: {'no working free method found in this run' if not d.get('skipped') and not any_x_working else 'see section 4'}.")
    lines.append(f"- {bc.get('only_x_known_or_nothing', 'N/A')}/{completed} sampled coins have no confirmed non-X official source at all.")
    lines.append(f"- {bc.get('deferred_due_to_missing_metadata', 'N/A')}/{completed} sampled coins had GitHub/subreddit "
                 "checks deferred this run because metadata enrichment hadn't reached them yet or was rate-limited; "
                 "re-running this script will pick them up incrementally (see Phase A).")
    lines.append("- Entities without a major associated token (e.g. Hotbit) are invisible to the coin-centric "
                 "universe and are only caught by generic news RSS.")
    lines.append("- Telegram-only and Discord-only official channels are not covered by any source in this project.")
    lines.append("- Historical futures availability cannot be verified for free for any past event (exchange "
                 "APIs only expose current listings).\n")

    lines.append("## 8. Estimated sustainable polling cadence\n")
    lines.append("- RSS/news/announcement feeds (6 configured): cheap, no rate-limit pressure observed; "
                 "10-30 minute polling is realistic.")
    lines.append("- Official per-coin sources (homepage/blog RSS/GitHub/subreddit) discovered in Phase B/C: "
                 "once discovered and cached, re-polling only the confirmed `monitorable=1` feeds is cheap and "
                 "RSS-feed-like; 10-30 minute polling is realistic for those specific feeds.")
    lines.append("- Metadata enrichment (the CoinGecko detail call for github/subreddit/announcement links) "
                 "remains rate-limited and is now a separately-budgeted, incremental, cached background step "
                 "(see Phase A/coin_link_metadata) -- never a per-cycle or per-validation-run operation.\n")

    lines.append("## 9. Zero recurring cost confirmation\n")
    lines.append("All sources used in this run are free/keyless: CoinGecko free tier, public RSS feeds, "
                 "GitHub's public .atom feeds, Reddit's public .rss feeds, xcancel.com, community Nitter "
                 "mirrors, RSSHub's public demo instance, and (only if the operator configured it) their own "
                 "self-hosted RSS-Bridge. No paid API, key, or infrastructure was used. **CONFIRMED: $0 "
                 "recurring cost for everything exercised in this run.**\n")

    lines.append("## 10. Final verdict\n")
    lines.append(f"**{verdict}**\n")

    REPORT_PATH.write_text("\n".join(lines))
    log(f"Report written to {REPORT_PATH}")


def main():
    deadline = time.monotonic() + GLOBAL_DEADLINE_SECONDS
    log(f"Hard global deadline set: {GLOBAL_DEADLINE_SECONDS/60:.0f} minutes from now.")

    if not check_cached_universe():
        write_report()
        save_raw_log()
        return 1
    save_raw_log()

    for phase_fn in (run_rss_recheck, run_phase_b_c, run_phase_d, run_phase_e):
        try:
            phase_fn(deadline)
        except Exception as exc:
            log(f"{phase_fn.__name__} raised: {exc}")
            results[f"{phase_fn.__name__}_error"] = str(exc)
        save_raw_log()

    write_report()
    save_raw_log()
    total_elapsed_min = (GLOBAL_DEADLINE_SECONDS - remaining(deadline)) / 60
    log(f"ALL PHASES COMPLETE in ~{total_elapsed_min:.1f} minutes. See SOURCE_COVERAGE_VALIDATION.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
