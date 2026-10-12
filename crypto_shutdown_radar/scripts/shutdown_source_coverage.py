#!/usr/bin/env python3
"""Measures SHUTDOWN-EVENT SOURCE COVERAGE for the SAME persisted 100-project
`v1` sample used by scripts/source_coverage_validation.py.

This does NOT rebuild the universe or the sample, and does NOT discard any
already-cached metadata/discovery results:
- The 1000-coin universe and 944 X mappings in data/radar.db are read-only
  here (Task 1).
- The persisted `validation_sample` (v1) is reused as-is.
- coin_link_metadata entries already OK are never re-fetched (Task 2);
  only coins still missing/deferred get a bounded enrichment pass.
- official_sources rows already recorded for a coin's first_party_page
  candidates are reused, not re-probed, on a later run (resumability).

Usage (from crypto_shutdown_radar/, with the venv active):
    python -m scripts.shutdown_source_coverage
"""
import sys
import time
import json as json_module
from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db, get_conn
from app.universe import get_universe_status, is_universe_cached
from app.sample_selection import get_or_build_validation_sample, SAMPLE_VERSION
from app.source_discovery import (
    enrich_missing_link_metadata, get_cached_link_metadata, check_url_reachable, first_link,
)
from app.shutdown_source_discovery import (
    discover_shutdown_sources_for_project, classify_project_coverage, get_cached_shutdown_pages,
)
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES

REPORT_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_COVERAGE.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_COVERAGE_RAW_LOG.json"

GLOBAL_DEADLINE_SECONDS = 25 * 60

ENRICHMENT_REQUEST_BUDGET = 35
ENRICHMENT_TIME_BUDGET_SECONDS = 90
DISCOVERY_TIME_BUDGET_SECONDS = 18 * 60
MAX_CANDIDATES_PER_PROJECT = 5

results = {"started_at": datetime.now(timezone.utc).isoformat()}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_raw_log():
    with open(RAW_LOG_PATH, "w") as f:
        json_module.dump(results, f, indent=2, default=str)


def remaining(deadline):
    return max(0.0, deadline - time.monotonic())


def check_cached_universe_and_sample():
    """Task 1: refuse to rebuild anything. Requires the same cached
    universe AND the same persisted v1 sample that source_coverage_validation
    already created; never triggers a full refresh or a fresh sample build.
    """
    init_db()
    with get_conn() as conn:
        status = get_universe_status(conn)
        cached = is_universe_cached(conn, min_coins=900)
        existing_sample_rows = conn.execute(
            "SELECT COUNT(*) c FROM validation_sample WHERE sample_version=?", (SAMPLE_VERSION,)
        ).fetchone()["c"]
    results["universe_status"] = status
    results["universe_cached"] = cached
    results["existing_sample_rows"] = existing_sample_rows

    if not cached:
        log("NO USABLE CACHED UNIVERSE FOUND. Run 'python -m scripts.refresh_universe' once first, "
            "or restore a prior data/radar.db, then re-run this.")
        return False, []
    if existing_sample_rows == 0:
        log(f"NO PERSISTED '{SAMPLE_VERSION}' VALIDATION SAMPLE FOUND. Run "
            "'python -m scripts.source_coverage_validation' once first (it creates and persists the "
            "100-project sample); this script reuses that exact sample and will not build a new one.")
        return False, []

    with get_conn() as conn:
        all_coins = conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins "
            "WHERE is_active=1 ORDER BY market_cap_rank ASC"
        ).fetchall()
        sample = get_or_build_validation_sample(conn, [dict(r) for r in all_coins], sample_version=SAMPLE_VERSION)
    log(f"Reusing persisted sample '{SAMPLE_VERSION}': {len(sample)} projects "
        f"(universe: {status['total_coins_cached']} coins, {status['x_accounts_verified']} X mappings cached).")
    return True, sample


def run_enrichment_pass(sample, deadline):
    """Task 2: bounded, resumable metadata enrichment for whichever of the
    100 sampled coins are still missing/deferred -- never the whole 1000.
    """
    coin_ids = [s["coingecko_id"] for s in sample]
    log(f"Enrichment pass (<= {ENRICHMENT_REQUEST_BUDGET} requests / {ENRICHMENT_TIME_BUDGET_SECONDS}s) "
        "for coins in the sample still missing link metadata...")
    summary = enrich_missing_link_metadata(
        coin_ids, request_budget=ENRICHMENT_REQUEST_BUDGET,
        time_budget_seconds=ENRICHMENT_TIME_BUDGET_SECONDS, deadline=deadline,
    )
    results["enrichment"] = summary
    log(f"  enrichment: {summary['attempted']} attempted, {summary['newly_ok']} newly OK, "
        f"{summary['deferred_rate_limited']} deferred, {summary['already_cached_skipped']} already cached, "
        f"{len(summary.get('malformed_coin_ids', []))} malformed")


def run_shutdown_discovery(sample, deadline):
    """Tasks 3/4/7: homepage-based first-party news/blog/announcement
    discovery for each of the 100 sampled projects, with per-project
    resilience and an overall time budget. Never aborts the batch for one
    broken site (Task 7); records exactly how many were actually resolved
    vs. left unresolved due to budget (Task 9's "mark unresolved" rule).
    """
    phase_deadline = min(deadline, time.monotonic() + DISCOVERY_TIME_BUDGET_SECONDS)
    t0 = time.monotonic()
    per_project = {}
    unresolved = []

    for i, s in enumerate(sample):
        cid = s["coingecko_id"]
        if time.monotonic() > phase_deadline:
            unresolved.append(cid)
            continue

        try:
            with get_conn() as conn:
                cached_pages = get_cached_shutdown_pages(conn, cid)
            if cached_pages:
                disc_result = discover_shutdown_sources_for_project(cid, s.get("website", ""), None, skip_if_cached=True)
            else:
                homepage = s.get("website", "")
                homepage_html = None
                if homepage:
                    status, body = check_url_reachable(homepage)
                    if status == 200:
                        homepage_html = body
                disc_result = discover_shutdown_sources_for_project(
                    cid, homepage, homepage_html, max_candidates=MAX_CANDIDATES_PER_PROJECT, skip_if_cached=False,
                )

            with get_conn() as conn:
                meta = get_cached_link_metadata(conn, cid)
            github_found = bool(meta and meta["fetch_status"] == "OK" and first_link(meta["github_url"]))
            subreddit_found = bool(meta and meta["fetch_status"] == "OK" and first_link(meta["subreddit_url"]))

            classification = classify_project_coverage(disc_result["pages"], github_found, subreddit_found)
            per_project[cid] = {**disc_result, **classification,
                                 "x_official_account_known": False, "x_ingestible": False}
        except Exception as exc:
            # Belt-and-suspenders on top of discover_shutdown_sources_for_project's
            # own internal try/except: a failure anywhere else in this
            # iteration (DB read, classification) still must not abort the
            # other 99 projects (Task 7).
            log(f"  project {cid} raised outside discovery itself, recording as errored: {exc}")
            per_project[cid] = {
                "coingecko_id": cid, "candidates_found": 0, "candidates_probed": 0, "pages": [],
                "from_cache": False, "error": str(exc), "classification": "D",
                "shutdown_relevant_first_party_coverage": False, "technical_monitorable_coverage": False,
                "x_official_account_known": False, "x_ingestible": False,
            }

        if (i + 1) % 10 == 0:
            elapsed = time.monotonic() - t0
            log(f"  processed {i+1}/{len(sample)} projects | elapsed {elapsed:.0f}s | "
                f"remaining budget {remaining(deadline):.0f}s")
        time.sleep(0.1)

    # X official-account-known, read from already-cached official_accounts -- never an extra network call.
    with get_conn() as conn:
        x_rows = conn.execute(
            "SELECT coingecko_id FROM official_accounts WHERE platform='x' AND verification_status='VERIFIED'"
        ).fetchall()
    x_known_ids = {r["coingecko_id"] for r in x_rows}
    for cid in per_project:
        per_project[cid]["x_official_account_known"] = cid in x_known_ids
        per_project[cid]["x_ingestible"] = False  # Task 5: never count X as monitorable right now

    results["per_project"] = per_project
    results["unresolved"] = unresolved
    results["discovery_elapsed_seconds"] = time.monotonic() - t0
    log(f"Shutdown-source discovery done: {len(per_project)}/{len(sample)} resolved, "
        f"{len(unresolved)} unresolved (time budget).")


def run_rss_recheck():
    log("Quick re-check of the 6 configured generic news/announcement RSS feeds (Layer 2)...")
    out = {}
    for adapter in build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES):
        r = adapter.collect()
        out[adapter.name] = {"ok": r.ok, "item_count": len(r.items) if r.ok else 0, "error": r.error}
    results["rss_recheck"] = out
    working = sum(1 for v in out.values() if v["ok"])
    log(f"  RSS re-check: {working}/{len(out)} working")


def write_report(sample):
    u = results.get("universe_status", {})
    per_project = results.get("per_project", {})
    unresolved = results.get("unresolved", [])
    enr = results.get("enrichment", {})
    rss = results.get("rss_recheck", {})
    sample_by_id = {s["coingecko_id"]: s for s in sample}

    lines = []
    lines.append("# SHUTDOWN_SOURCE_COVERAGE\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Sample: persisted stratified `{SAMPLE_VERSION}` ({len(sample)} projects, reused as-is -- see "
                 "SOURCE_COVERAGE_VALIDATION.md for how it was built).\n")

    lines.append("## 1. Exact 100-project sample, classification, and sources\n")
    lines.append("| Rank | Stratum | Project | Classification | Best source(s) | Relevance | X known |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in sample:
        cid = s["coingecko_id"]
        p = per_project.get(cid)
        if p is None:
            lines.append(f"| {s['rank_at_selection']} | {s['stratum']} | {s.get('name', cid)} | UNRESOLVED | - | - | - |")
            continue
        monitorable_pages = [pg for pg in p["pages"] if pg["monitorable"]]
        best = sorted(monitorable_pages, key=lambda pg: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(pg["relevance"], 3))
        source_urls = "; ".join(pg["url"] for pg in best[:2]) or "-"
        relevances = "; ".join(pg["relevance"] for pg in best[:2]) or "-"
        lines.append(
            f"| {s['rank_at_selection']} | {s['stratum']} | {s.get('name', cid)} | {p['classification']} | "
            f"{source_urls} | {relevances} | {'yes' if p['x_official_account_known'] else 'no'} |"
        )
    lines.append("")

    resolved_count = len(per_project)
    a_count = sum(1 for p in per_project.values() if p["classification"] == "A")
    b_count = sum(1 for p in per_project.values() if p["classification"] == "B")
    c_count = sum(1 for p in per_project.values() if p["classification"] == "C")
    d_count = sum(1 for p in per_project.values() if p["classification"] == "D")
    strong_plus_moderate = a_count + b_count
    technical = sum(1 for p in per_project.values() if p["technical_monitorable_coverage"])
    x_known_count = sum(1 for p in per_project.values() if p["x_official_account_known"])

    lines.append("## 2-5. Classification summary\n")
    lines.append(f"Resolved: {resolved_count}/{len(sample)}  |  Unresolved (time budget): {len(unresolved)}/{len(sample)}\n")
    lines.append(f"- A (STRONG first-party shutdown-event coverage): {a_count}/{resolved_count if resolved_count else 'N/A'}")
    lines.append(f"- B (MODERATE first-party coverage): {b_count}/{resolved_count if resolved_count else 'N/A'}")
    lines.append(f"- C (WEAK -- only generic/technical sources, e.g. GitHub/subreddit): {c_count}/{resolved_count if resolved_count else 'N/A'}")
    lines.append(f"- D (NO direct coverage): {d_count}/{resolved_count if resolved_count else 'N/A'}")
    lines.append(f"- X official account known: {x_known_count}/{resolved_count if resolved_count else 'N/A'} "
                 "(X is NOT counted as monitorable -- see Layer 3 below; 0 free ingestion methods currently work)\n")

    lines.append("## 6. Generic news layer status (Layer 2 -- separate, does not inflate direct coverage)\n")
    rss_working = sum(1 for v in rss.values() if v.get("ok"))
    lines.append(f"Generic crypto-news RSS feeds working: {rss_working}/{len(rss)}\n")

    lines.append("## 7-8. Technical vs. shutdown-relevant coverage (the decision metric)\n")
    lines.append(f"- TECHNICAL_MONITORABLE_COVERAGE (any official machine-monitorable source, including GitHub/subreddit): "
                 f"{technical}/{resolved_count if resolved_count else 'N/A'}")
    lines.append(f"- **SHUTDOWN_RELEVANT_FIRST_PARTY_COVERAGE (decision metric, A+B only): "
                 f"{strong_plus_moderate}/{resolved_count if resolved_count else 'N/A'}**\n")

    lines.append("## 9. Per-project detail (rank, source, relevance, reason)\n")
    lines.append("| Project | Rank | Classification | Pages probed | Monitorable pages | X known |")
    lines.append("|---|---|---|---|---|---|")
    for s in sample:
        cid = s["coingecko_id"]
        p = per_project.get(cid)
        if p is None:
            lines.append(f"| {s.get('name', cid)} | {s['rank_at_selection']} | UNRESOLVED | - | - | - |")
            continue
        monitorable_pages = [pg for pg in p["pages"] if pg["monitorable"]]
        page_desc = "; ".join(f"{pg['url']} [{pg['relevance']}]" for pg in monitorable_pages) or "none"
        lines.append(
            f"| {s.get('name', cid)} | {s['rank_at_selection']} | {p['classification']} | "
            f"{p['candidates_probed']} | {page_desc} | {'yes' if p['x_official_account_known'] else 'no'} |"
        )
    lines.append("")

    lines.append("## 8 (cont). Coverage by rank stratum\n")
    lines.append("| Stratum | Resolved | A | B | C | D | A+B |")
    lines.append("|---|---|---|---|---|---|---|")
    for stratum in ("1-100", "101-300", "301-600", "601-1000"):
        ids_in_stratum = [s["coingecko_id"] for s in sample if s["stratum"] == stratum]
        resolved_in = [per_project[cid] for cid in ids_in_stratum if cid in per_project]
        a = sum(1 for p in resolved_in if p["classification"] == "A")
        b = sum(1 for p in resolved_in if p["classification"] == "B")
        c = sum(1 for p in resolved_in if p["classification"] == "C")
        d = sum(1 for p in resolved_in if p["classification"] == "D")
        lines.append(f"| {stratum} | {len(resolved_in)}/{len(ids_in_stratum)} | {a} | {b} | {c} | {d} | {a+b} |")
    lines.append("")

    lines.append("## 12. Unresolved / deferred\n")
    lines.append(f"- Projects left UNRESOLVED this run (time budget exhausted before reaching them): {len(unresolved)}")
    if unresolved:
        lines.append(f"  IDs: {', '.join(unresolved)}")
    lines.append(f"- Metadata enrichment this run: {enr.get('attempted', 'N/A')} attempted, "
                 f"{enr.get('newly_ok', 'N/A')} newly OK, {enr.get('deferred_rate_limited', 'N/A')} still deferred "
                 "(will retry on a later run, never blocked on now).\n")

    lines.append("## 13. Top remaining blind spots\n")
    lines.append("- X/Twitter: 0 working free ingestion methods currently exist; X_OFFICIAL_ACCOUNT_KNOWN is tracked "
                 "separately and never counted toward monitorable coverage.")
    lines.append("- The homepage-link-discovery heuristic only follows a small, bounded candidate path/keyword set "
                 "(Task 3) -- a project whose news surface uses an unlisted path or an unusual link structure (e.g. "
                 "a JS-rendered single-page app with no server-rendered <a> tags) will be under-counted, not "
                 "over-counted; this is a conservative floor, not a ceiling.")
    lines.append("- The monitorability heuristic (Task 4) is intentionally lightweight pattern-matching, not a real "
                 "scraper -- it establishes plausibility, not a production-ready polling implementation.")
    lines.append("- A first-party page behind a cookie/consent wall that returns non-200 to an unauthenticated "
                 "request will show as unreachable even if a browser could see it.\n")

    lines.append("## Zero-cost confirmation\n")
    lines.append("All sources used in this run are free/keyless: the cached CoinGecko metadata, each project's own "
                 "public homepage and linked pages, and the existing free RSS feeds. No paid API, scraping service, "
                 "proxy, or hosted service was used. **CONFIRMED: $0 recurring cost for everything exercised.**\n")

    lines.append("## No viability verdict\n")
    if resolved_count < len(sample):
        lines.append(f"**{len(unresolved)}/{len(sample)} projects are UNRESOLVED this run.** Per instructions, no "
                     "viability verdict is given until all 100 are resolved or explicitly marked unresolved (as "
                     "done above). Re-run this script to continue resolving the remaining projects incrementally.\n")
    else:
        lines.append(f"All {len(sample)} projects were evaluated. **STRONG + MODERATE shutdown-event coverage = "
                     f"{strong_plus_moderate}/{len(sample)}.** This is a measurement, not a viability verdict.\n")

    REPORT_PATH.write_text("\n".join(lines))
    log(f"Report written to {REPORT_PATH}")


def main():
    deadline = time.monotonic() + GLOBAL_DEADLINE_SECONDS
    log(f"Hard global deadline: {GLOBAL_DEADLINE_SECONDS/60:.0f} minutes.")

    ok, sample = check_cached_universe_and_sample()
    save_raw_log()
    if not ok:
        return 1

    try:
        run_enrichment_pass(sample, deadline)
    except Exception as exc:
        log(f"run_enrichment_pass raised: {exc}")
        results["enrichment"] = {"failed": True, "error": str(exc)}
    save_raw_log()

    try:
        run_rss_recheck()
    except Exception as exc:
        log(f"run_rss_recheck raised: {exc}")
        results["rss_recheck"] = {}
    save_raw_log()

    try:
        run_shutdown_discovery(sample, deadline)
    except Exception as exc:
        log(f"run_shutdown_discovery raised: {exc}")
        results["per_project"] = results.get("per_project", {})
        results["unresolved"] = [s["coingecko_id"] for s in sample if s["coingecko_id"] not in results["per_project"]]
    save_raw_log()

    write_report(sample)
    save_raw_log()
    total_elapsed_min = (GLOBAL_DEADLINE_SECONDS - remaining(deadline)) / 60
    log(f"DONE in ~{total_elapsed_min:.1f} minutes. See SHUTDOWN_SOURCE_COVERAGE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
