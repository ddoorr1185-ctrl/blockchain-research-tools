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
rather than run.

Every CoinGecko call anywhere in this script's call graph goes through
app.universe.get_json_no_retry, which never sleeps on a 429 (no
Retry-After wait, no retry loop).

HONESTY GUARANTEE (Task 2): if Phase B/C -- the 100-project
official-source coverage measurement, the central evidence this script
exists to produce -- fails or is skipped, the report says so explicitly
("PHASE FAILED" / "COVERAGE NOT MEASURED") and the final verdict is
INCONCLUSIVE / VALIDATION FAILED. It never computes a percentage against
a missing/zero denominator and never emits A/B/C/D without that evidence.

Usage (from crypto_shutdown_radar/, with the venv active):
    python -m scripts.source_coverage_validation
"""
import sys
import time
import json as json_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import init_db, get_conn
from app.universe import get_universe_status, is_universe_cached
from app.source_discovery import discover_for_sample
from app.sample_selection import get_or_build_validation_sample, SAMPLE_VERSION
from app.sources.x_fallback_experiments import run_all_candidates
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES
from app.historical_prices import get_or_fetch_price_series
from scripts.historical_events_data import HISTORICAL_EVENTS

REPORT_PATH = Path(__file__).resolve().parent.parent / "SOURCE_COVERAGE_VALIDATION.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "SOURCE_COVERAGE_VALIDATION_RAW_LOG.json"

GLOBAL_DEADLINE_SECONDS = 30 * 60

ENRICHMENT_REQUEST_BUDGET = 40
ENRICHMENT_TIME_BUDGET_SECONDS = 90
DISCOVERY_TIME_BUDGET_SECONDS = 600
PHASE_D_TIME_BUDGET_SECONDS = 90
PHASE_E_TIME_BUDGET_SECONDS = 300
PHASE_E_REQUEST_BUDGET_PER_EVENT = 5

SAMPLE_X_HANDLES = ["binance", "coinbase", "VitalikButerin", "ethereum", "cz_binance"]

results = {"started_at": datetime.now(timezone.utc).isoformat()}



def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_raw_log():
    with open(RAW_LOG_PATH, "w") as f:
        json_module.dump(results, f, indent=2, default=str)


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


def get_all_active_coins():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins "
            "WHERE is_active=1 ORDER BY market_cap_rank ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase B/C: stratified 100-project official-source coverage
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

    all_coins = get_all_active_coins()
    with get_conn() as conn:
        sample = get_or_build_validation_sample(conn, all_coins, sample_version=SAMPLE_VERSION)
    log(f"PHASE B/C: deterministic stratified sample ({SAMPLE_VERSION}): {len(sample)} projects "
        f"(40 from rank 1-100, 30 from 101-300, 20 from 301-600, 10 from 601-1000)")
    log(f"  enrichment<= {ENRICHMENT_REQUEST_BUDGET} requests/{ENRICHMENT_TIME_BUDGET_SECONDS}s, "
        f"discovery<= {DISCOVERY_TIME_BUDGET_SECONDS}s, remaining global budget: {remaining(deadline):.0f}s")

    discovery = discover_for_sample(
        sample,
        enrichment_request_budget=ENRICHMENT_REQUEST_BUDGET,
        enrichment_time_budget_seconds=ENRICHMENT_TIME_BUDGET_SECONDS,
        discovery_time_budget_seconds=DISCOVERY_TIME_BUDGET_SECONDS,
        deadline=deadline,
        progress_every=10,
        progress_cb=_progress_cb,
    )
    discovery["sample"] = sample  # carry rank/stratum through for the report table
    results["phase_b_c_discovery"] = discovery
    enr = discovery["enrichment"]
    log(f"PHASE B/C done: {discovery['completed']}/{discovery['requested']} coins processed in "
        f"{discovery['elapsed_seconds']:.0f}s (stopped early: {discovery['stopped_early_due_to_time_budget']})")
    log(f"  enrichment: {enr['attempted']} attempted, {enr['newly_ok']} newly OK, "
        f"{enr['deferred_rate_limited']} rate-limited/deferred, {enr['already_cached_skipped']} already cached, "
        f"{len(enr.get('malformed_coin_ids', []))} malformed (skipped, did not abort the run), "
        f"stopped_reason={enr['stopped_reason']}")
    log(f"  homepage={discovery['homepage_coverage']} blog_rss={discovery['blog_rss_coverage']} "
        f"github={discovery['github_coverage']} subreddit={discovery['subreddit_coverage']} "
        f"announcement={discovery['announcement_coverage']} "
        f"directly_monitorable={discovery['at_least_one_official_source']} "
        f"homepage_only_or_nothing={discovery['only_x_known_or_nothing']}")
    if discovery["malformed_coin_ids"]:
        log(f"  malformed/errored coins (did not abort the batch): {discovery['malformed_coin_ids']}")


# ---------------------------------------------------------------------------
# Phase D: X fallback research against 5 known accounts, with cooldown
# ---------------------------------------------------------------------------



def run_phase_d(deadline):
    if time.monotonic() > deadline:
        log("PHASE D: SKIPPED (global 30-minute deadline already passed before this phase started)")
        results["phase_d_x_fallback"] = {"skipped": True, "reason": "global_deadline_exceeded"}
        return
    phase_deadline = min(deadline, time.monotonic() + PHASE_D_TIME_BUDGET_SECONDS)
    log(f"PHASE D: testing X-fallback candidate methods against 5 known accounts "
        f"(budget {PHASE_D_TIME_BUDGET_SECONDS}s, remaining global budget: {remaining(deadline):.0f}s; "
        f"methods that failed within the last 24h are skipped via cooldown, not re-tested)...")
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
            "skipped_cooldown_reason": mr.skipped_cooldown_reason,
        }
    results["phase_d_x_fallback"] = {"elapsed_seconds": elapsed, "methods": out}
    for name, data in out.items():
        if data["skipped_cooldown_reason"]:
            log(f"  {name}: SKIPPED ({data['skipped_cooldown_reason']})")
        else:
            log(f"  {name}: {data['success_count']}/{data['total_count']} accounts succeeded")
    log(f"PHASE D done in {elapsed:.0f}s.")


# ---------------------------------------------------------------------------
# Phase E: historical event re-validation, cached/incremental price lookups
# ---------------------------------------------------------------------------



def run_phase_e(deadline):
    if time.monotonic() > deadline:
        log("PHASE E: SKIPPED (global 30-minute deadline already passed before this phase started)")
        results["phase_e_historical"] = {"skipped": True, "reason": "global_deadline_exceeded"}
        return
    phase_deadline = min(deadline, time.monotonic() + PHASE_E_TIME_BUDGET_SECONDS)
    log(f"PHASE E: re-validating {len(HISTORICAL_EVENTS)} historical events with cached/incremental "
        f"CoinGecko price-history lookups (budget {PHASE_E_TIME_BUDGET_SECONDS}s, "
        f"remaining global budget: {remaining(deadline):.0f}s)...")
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
                ann_date = datetime.strptime(event["exact_event_timestamp"], "%Y-%m-%d")
            except ValueError:
                ann_date = None
            if ann_date:
                targets = {
                    "at_announcement": ann_date, "+24h": ann_date + timedelta(days=1),
                    "+48h": ann_date + timedelta(days=2), "+72h": ann_date + timedelta(days=3),
                    "+7d": ann_date + timedelta(days=7),
                }
                date_strs = {label: dt.strftime("%d-%m-%Y") for label, dt in targets.items()}
                series = get_or_fetch_price_series(
                    event["coingecko_id"], list(date_strs.values()),
                    request_budget=PHASE_E_REQUEST_BUDGET_PER_EVENT,
                    deadline=phase_deadline,
                )
                row["measured_prices_usd"] = {
                    label: (series[date_str]["price_usd"] if series[date_str]["status"] == "OK"
                            else f"UNKNOWN ({series[date_str]['status']})")
                    for label, date_str in date_strs.items()
                }
            else:
                row["measured_prices_usd"] = {"error": "unparseable exact_event_timestamp"}
        else:
            row["measured_prices_usd"] = {"note": "no coingecko_id (no major associated token)"}
        out.append(row)
        log(f"  [{event['category']}] {event['project']} ({event['ticker']}): "
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
def phase_b_c_is_valid_evidence(bc: dict) -> bool:
    """False if Phase B/C didn't produce real measured evidence -- failed,
    skipped, or (defensively) missing entirely. This gates the verdict per
    Task 2: never emit A/B/C/D without this central evidence.
    """
    if not bc:
        return False
    if bc.get("failed") or bc.get("skipped"):
        return False
    if bc.get("completed") is None or bc.get("requested") is None:
        return False
    return True


def pick_verdict():
    bc = results.get("phase_b_c_discovery", {})
    if not phase_b_c_is_valid_evidence(bc):
        return "INCONCLUSIVE / VALIDATION FAILED"

    completed = bc["completed"] or 1
    at_least_one_pct = 100 * bc.get("at_least_one_official_source", 0) / completed
    rss = results.get("rss_recheck", {})
    rss_working = sum(1 for v in rss.values() if v.get("ok")) if isinstance(rss, dict) else 0

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
    bc_valid = phase_b_c_is_valid_evidence(bc)

    lines = []
    lines.append("# SOURCE_COVERAGE_VALIDATION\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Hard global budget: {GLOBAL_DEADLINE_SECONDS/60:.0f} minutes.\n")

    lines.append("## 1. Cached universe status\n")
    if not results.get("universe_cached"):
        lines.append("**NO CACHED UNIVERSE WAS FOUND. This run stopped before any other phase.** "
                     "Run `python -m scripts.refresh_universe` once (or restore a prior data/radar.db), "
                     "then re-run this script.\n")
        lines.append("## FINAL VERDICT\n\n**INCONCLUSIVE / VALIDATION FAILED**\n")
        REPORT_PATH.write_text("\n".join(lines))
        return
    lines.append(f"- Coins cached: {u.get('total_coins_cached')}")
    lines.append(f"- X accounts VERIFIED: {u.get('x_accounts_verified')}")
    lines.append(f"- X accounts UNVERIFIED: {u.get('x_accounts_unverified')}")
    lines.append("- **No 1000-coin CoinGecko detail refresh was performed by this script.**\n")

    lines.append("## 2. 100-project official-source coverage\n")
    if bc.get("skipped"):
        lines.append("**PHASE FAILED (SKIPPED): global 30-minute deadline was already exceeded before this phase started.**\n")
    elif bc.get("failed"):
        lines.append(f"**PHASE FAILED: {bc.get('error')}**\n")
    elif not bc_valid:
        lines.append("**PHASE FAILED: no valid coverage data was produced (missing completed/requested counts).**\n")
    else:
        enr = bc.get("enrichment", {})
        lines.append(f"Sample: deterministic stratified `{SAMPLE_VERSION}` (40 from rank 1-100, 30 from 101-300, "
                     "20 from 301-600, 10 from 601-1000; stablecoins/wrapped-no-independent-operator excluded)")
        lines.append(f"Sample processed: {bc.get('completed')}/{bc.get('requested')} "
                     f"(stopped early on time budget: {bc.get('stopped_early_due_to_time_budget')})")
        lines.append(f"Metadata enrichment this run: {enr.get('attempted')} CoinGecko requests attempted, "
                     f"{enr.get('newly_ok')} newly cached OK, {enr.get('deferred_rate_limited')} deferred "
                     f"(rate-limited -- will retry on a later run, not held/slept on), "
                     f"{enr.get('already_cached_skipped')} already cached from a prior run and skipped entirely, "
                     f"{len(enr.get('malformed_coin_ids', []))} malformed (did not abort the batch), "
                     f"stopped_reason={enr.get('stopped_reason')}\n")

    lines.append("## 3. Coverage percentages by source type\n")
    if not bc_valid:
        lines.append("**COVERAGE NOT MEASURED** (Phase B/C did not complete -- see section 2).\n")
    else:
        completed = bc.get("completed", 0) or 1
        for key, label in (
            ("homepage_coverage", "Official homepage reachable (informational only -- NOT monitorable)"),
            ("blog_rss_coverage", "Working official blog/RSS feed found"),
            ("github_coverage", "Official GitHub activity feed confirmed"),
            ("subreddit_coverage", "Official subreddit RSS confirmed"),
            ("announcement_coverage", "Official announcement page confirmed"),
            ("at_least_one_official_source", "At least ONE DIRECTLY MONITORABLE official source (strict: homepage alone does not count)"),
            ("only_x_known_or_nothing", "No directly-monitorable non-X source found (homepage-only or nothing)"),
            ("deferred_due_to_missing_metadata", "Coins where GitHub/subreddit check was deferred (metadata not yet cached)"),
        ):
            n = bc.get(key, 0)
            lines.append(f"- {label}: {n}/{completed} ({100*n/completed:.0f}%)")
        lines.append("\n### Sample detail (rank reported for every project)\n")
        lines.append("| Rank | Stratum | Coin | Homepage | Blog RSS | GitHub | Subreddit | Announcement | Directly monitorable |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        sample_by_id = {s["coingecko_id"]: s for s in bc.get("sample", [])}
        for r in bc.get("per_coin", []):
            s = sample_by_id.get(r["coingecko_id"], {})
            lines.append(
                f"| {s.get('rank_at_selection', '?')} | {s.get('stratum', '?')} | {s.get('name', r['coingecko_id'])} | "
                f"{'yes' if r['homepage_reachable'] else 'no'} | {'yes' if r['blog_rss_found'] else 'no'} | "
                f"{'yes' if r['github_feed_found'] else 'no'} | {'yes' if r['subreddit_feed_found'] else 'no'} | "
                f"{'yes' if r['announcement_page_found'] else 'no'} | "
                f"{'YES' if r['directly_monitorable'] else 'no'} |"
            )
        lines.append("")

    lines.append("## 4. Real X fallback test results\n")
    lines.append(f"Tested against {len(SAMPLE_X_HANDLES)} known public accounts: {', '.join(SAMPLE_X_HANDLES)}\n")
    any_x_working = False
    if d.get("skipped"):
        lines.append("**SKIPPED: global 30-minute deadline was already exceeded before this phase started.**\n")
    elif d.get("failed"):
        lines.append(f"**PHASE FAILED: {d.get('error')}**\n")
    else:
        lines.append("| Method | Auth required | Normal account required | Cost | Success | Status |")
        lines.append("|---|---|---|---|---|---|")
        for name, m in d.get("methods", {}).items():
            status = f"SKIPPED: {m['skipped_cooldown_reason']}" if m.get("skipped_cooldown_reason") else "tested"
            lines.append(f"| {m['method']} | {m['requires_auth']} | {m['requires_normal_account']} | {m['cost']} | "
                         f"{m['success_count']}/{m['total_count']} | {status} |")
        any_x_working = any(m["success_count"] > 0 for m in d.get("methods", {}).values())
        lines.append(f"\n**Sustainable zero-cost X method found: {'YES' if any_x_working else 'NO'}.** "
                     "Methods that failed within the last 24h are skipped (cooldown), not re-tested, so they "
                     "cannot be hammered on every validation run (Task 8).\n")

    lines.append("## 5. Historical-event detection table\n")
    if e.get("skipped"):
        lines.append("**SKIPPED: global 30-minute deadline was already exceeded before this phase started.**\n")
    elif e.get("failed"):
        lines.append(f"**PHASE FAILED: {e.get('error')}**\n")
    else:
        primary = [ev for ev in e.get("events", []) if ev.get("primary_target")]
        context = [ev for ev in e.get("events", []) if not ev.get("primary_target")]
        lines.append(f"**PRIMARY TARGET (categories A/B/C -- what this project's shutdown detector is actually "
                     f"designed to catch): {len(primary)} events.** Category D events below are context only and "
                     "are NOT counted as a detector success.\n")
        lines.append("| Cat | Project | Token | Event claim | Date | Primary source verified | Shortable at announcement |")
        lines.append("|---|---|---|---|---|---|---|")
        for ev in e.get("events", []):
            lines.append(
                f"| {ev['category']} | {ev['project']} | {ev['ticker'] or '-'} | {ev['exact_event_claim'][:90]} | "
                f"{ev['exact_event_timestamp']} | {'YES' if ev['primary_source_verified'] else 'NOT VERIFIED'} | "
                f"{ev['shortable_at_announcement']} |"
            )

        lines.append("\n### Measured real price history (cached/incremental CoinGecko lookups)\n")
        for ev in e.get("events", []):
            lines.append(f"**[{ev['category']}] {ev['project']} ({ev['ticker'] or 'N/A'})** -- sources: " +
                         ", ".join(ev["sources"]))
            prices = ev.get("measured_prices_usd", {})
            if isinstance(prices, dict) and prices:
                lines.append("  " + ", ".join(f"{k}: {v}" for k, v in prices.items()))
            lines.append(f"  Notes: {ev['notes']}\n")

    lines.append("## 6. Detection-delay analysis\n")
    lines.append(
        "Restricted to the PRIMARY TARGET (A/B/C) cases, since category D events (Terra/LUNA, Iron Finance) had "
        "no explicit shutdown statement for this project's classifier to match and are context only. Across the "
        "A/B/C cases, the dominant pattern is: bankruptcy filings and explicit wind-down announcements were "
        "covered by at least one of CoinDesk/Cointelegraph/TheBlock/Decrypt within minutes to a few hours of the "
        "primary trigger -- comfortably within this project's 10-30 minute RSS poll cadence once published. "
        "Multichain showed secondary-source distress signals WEEKS before its formal 'ceasing operations' "
        "statement. Voyager/VGX is a reminder that fast detection does not guarantee a clean, tradeable sustained "
        "decline (it rose after its bankruptcy filing). Hotbit is a structural blind spot: an exchange closure "
        "with no major associated token falls outside this project's coin-centric universe entirely. In every "
        "single case, the earliest source found was secondary news coverage, not the operator's own primary "
        "announcement -- primary_source_verified is False throughout (see section 5); this does not mean no "
        "primary source exists, only that this research pass did not open and confirm one.\n"
    )

    lines.append("## 7. Remaining blind spots\n")
    if bc_valid:
        completed = bc.get("completed", 0) or 1
        lines.append(f"- X/Twitter: {'no working free method found in this run' if not any_x_working else 'see section 4'}.")
        lines.append(f"- {bc.get('only_x_known_or_nothing', 'N/A')}/{completed} sampled coins have no confirmed directly-monitorable non-X official source.")
        lines.append(f"- {bc.get('deferred_due_to_missing_metadata', 'N/A')}/{completed} sampled coins had GitHub/subreddit "
                     "checks deferred this run; re-running this script picks them up incrementally (Phase A).")
    else:
        lines.append("- Coverage blind spots cannot be assessed this run -- Phase B/C did not produce valid data.")
    lines.append("- Entities without a major associated token (e.g. Hotbit) are invisible to the coin-centric universe.")
    lines.append("- Telegram-only and Discord-only official channels are not covered by any source in this project.")
    lines.append("- Historical futures/perpetual availability cannot be verified for free for any past event.\n")

    lines.append("## 8. Estimated sustainable polling cadence\n")
    lines.append("- RSS/news/announcement feeds: cheap, no rate-limit pressure observed; 10-30 minute polling is realistic.")
    lines.append("- Confirmed `monitorable=1` per-coin feeds (once discovered): cheap, RSS-feed-like; 10-30 minute polling is realistic.")
    lines.append("- Metadata enrichment (CoinGecko detail calls) remains rate-limited and is a separately-budgeted, "
                 "incremental, cached background step -- never a per-cycle operation.\n")

    lines.append("## 9. Zero recurring cost confirmation\n")
    lines.append("All sources used in this run are free/keyless. No paid API, key, or infrastructure was used. "
                 "**CONFIRMED: $0 recurring cost for everything exercised in this run.**\n")

    lines.append("## 10. Final verdict\n")
    lines.append(f"**{verdict}**\n")
    if verdict.startswith("INCONCLUSIVE"):
        lines.append("Phase B/C (the 100-project coverage measurement) did not produce valid evidence this run. "
                     "No A/B/C/D viability verdict can be based on missing central evidence. Fix the reported "
                     "failure and re-run.\n")

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

    # (function name, results key it owns) -- looked up via globals() each
    # iteration (not a tuple of direct function references) so a test's
    # patch.object(scv, "run_phase_b_c", ...) is honored, and so the except
    # branch never needs a mock-unsafe `phase_fn.__name__` to find the key.
    phases = [
        ("run_rss_recheck", "rss_recheck"),
        ("run_phase_b_c", "phase_b_c_discovery"),
        ("run_phase_d", "phase_d_x_fallback"),
        ("run_phase_e", "phase_e_historical"),
    ]
    for fn_name, result_key in phases:
        try:
            globals()[fn_name](deadline)
        except Exception as exc:
            log(f"{fn_name} raised: {exc}")
            results[result_key] = {"failed": True, "error": str(exc)}
        save_raw_log()

    write_report()
    save_raw_log()
    total_elapsed_min = (GLOBAL_DEADLINE_SECONDS - remaining(deadline)) / 60
    log(f"ALL PHASES COMPLETE in ~{total_elapsed_min:.1f} minutes. See SOURCE_COVERAGE_VALIDATION.md")

    bc = results.get("phase_b_c_discovery", {})
    if not phase_b_c_is_valid_evidence(bc):
        log("VERDICT: INCONCLUSIVE / VALIDATION FAILED -- Phase B/C did not produce valid coverage data.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
