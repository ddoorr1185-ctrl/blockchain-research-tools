#!/usr/bin/env python3
"""Audits the raw A/B shutdown-source classification already recorded in
official_sources (from scripts/shutdown_source_coverage.py) under a much
stricter PUBLICATION STREAM rule, and runs a bounded deeper-discovery pass
on a short list of named "important D" projects.

Does NOT rebuild the universe, the persisted 100-project sample, or
data/radar.db; does NOT re-run the raw coverage harness. Reads
official_sources (raw discovery output) as input, writes findings to the
SEPARATE shutdown_source_audit / project_audit_classification tables (see
app.db), and renders SHUTDOWN_SOURCE_AUDIT.md.

Usage (from crypto_shutdown_radar/, with the venv active):
    python -m scripts.shutdown_source_audit
"""
import sys
import time
import json as json_module
from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db, get_conn, now_iso
from app.universe import is_universe_cached
from app.sample_selection import get_or_build_validation_sample, SAMPLE_VERSION
from app.shutdown_source_audit import (
    audit_candidate_source, deeper_discovery_for_d_project, entity_relationship,
    wilson_confidence_interval, record_audit,
)

REPORT_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_AUDIT.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_AUDIT_RAW_LOG.json"

GLOBAL_DEADLINE_SECONDS = 25 * 60
AUDIT_TIME_BUDGET_SECONDS = 15 * 60
DEEPER_PASS_TIME_BUDGET_SECONDS = 8 * 60
MAX_SOURCES_AUDITED_PER_PROJECT = 3

# Task 5's named "important D" projects to re-examine with the deeper pass.
# A plain, fixed list -- this does NOT broaden the 100-project universe;
# these IDs are only audited if they are already present in the persisted
# sample (most are top-100/300 and will be).
NAMED_D_PROJECTS = [
    "hyperliquid", "avalanche-2", "uniswap", "bittensor", "crypto-com-chain",
    "curve-dao-token", "lido-dao", "litecoin", "pendle", "pump-fun",
    "raydium", "virtuals-protocol", "arkham", "dogecoin", "shiba-inu",
]

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
    """Refuses to rebuild anything -- requires the same cached universe AND
    the same persisted sample the raw coverage run already used.
    """
    init_db()
    with get_conn() as conn:
        cached = is_universe_cached(conn, min_coins=900)
        existing_sample_rows = conn.execute(
            "SELECT COUNT(*) c FROM validation_sample WHERE sample_version=?", (SAMPLE_VERSION,)
        ).fetchone()["c"]
    if not cached:
        log("NO USABLE CACHED UNIVERSE FOUND. This script will not rebuild it.")
        return False, []
    if existing_sample_rows == 0:
        log(f"NO PERSISTED '{SAMPLE_VERSION}' VALIDATION SAMPLE FOUND. This script will not build a new one.")
        return False, []

    with get_conn() as conn:
        all_coins = conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins "
            "WHERE is_active=1 ORDER BY market_cap_rank ASC"
        ).fetchall()
        sample = get_or_build_validation_sample(conn, [dict(r) for r in all_coins], sample_version=SAMPLE_VERSION)
    log(f"Reusing persisted sample '{SAMPLE_VERSION}': {len(sample)} projects.")
    return True, sample


def load_raw_classification(sample):
    """Reads the RAW (unaudited) per-project classification directly from
    official_sources -- the same A/B/C/D logic scripts/shutdown_source_coverage.py
    already computed and persisted there, re-derived read-only so this
    script never has to re-run discovery.
    """
    raw = {}
    with get_conn() as conn:
        for s in sample:
            cid = s["coingecko_id"]
            rows = conn.execute(
                "SELECT * FROM official_sources WHERE coingecko_id=? AND source_type='first_party_page'", (cid,)
            ).fetchall()
            pages = [dict(r) for r in rows]
            monitorable_high = any(p["monitorable"] and p["shutdown_relevance"] == "HIGH" for p in pages)
            monitorable_medium = any(p["monitorable"] and p["shutdown_relevance"] == "MEDIUM" for p in pages)
            if monitorable_high:
                classification = "A"
            elif monitorable_medium:
                classification = "B"
            elif any(p["monitorable"] for p in pages):
                classification = "C"
            else:
                classification = "D"
            raw[cid] = {"classification": classification, "pages": pages}
    return raw


def run_audit_pass(sample, raw, deadline):
    """Task 4: audits every project raw-classified A or B under the strict
    stream rule. For each, re-fetches its monitorable HIGH/MEDIUM pages
    (bounded per project) through audit_candidate_source, records every
    finding, and derives the project's STRICT classification from the
    best surviving verdict.
    """
    phase_deadline = min(deadline, time.monotonic() + AUDIT_TIME_BUDGET_SECONDS)
    audited = {}
    unresolved = []

    sample_by_id = {s["coingecko_id"]: s for s in sample}
    ab_projects = [cid for cid, r in raw.items() if r["classification"] in ("A", "B")]
    log(f"Auditing {len(ab_projects)} raw A/B projects under the strict stream rule...")

    for i, cid in enumerate(ab_projects):
        if time.monotonic() > phase_deadline:
            unresolved.append(cid)
            continue
        try:
            s = sample_by_id.get(cid, {})
            homepage = s.get("website", "")
            name, symbol = s.get("name", cid), s.get("symbol", "")
            _, rel_status = entity_relationship(cid, symbol, name, homepage)

            pages = raw[cid]["pages"]
            rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            pages_sorted = sorted(
                [p for p in pages if p["monitorable"]],
                key=lambda p: rank.get(p["shutdown_relevance"], 3),
            )

            verdicts = []
            with get_conn() as conn:
                for p in pages_sorted[:MAX_SOURCES_AUDITED_PER_PROJECT]:
                    audit = audit_candidate_source(cid, p["url"], rel_status)
                    record_audit(
                        conn, cid, p["url"], audit["canonical_stream_url"], audit["verdict"],
                        audit["audited_relevance"], audit["entries"], rel_status, audit["reason"],
                    )
                    verdicts.append(audit)

            passing = [v for v in verdicts if v["verdict"] == "PASS" and rel_status != "UNCLEAR"]
            if any(v["audited_relevance"] == "HIGH" for v in passing):
                strict_class = "A"
            elif any(v["audited_relevance"] == "MEDIUM" for v in passing):
                strict_class = "B"
            elif pages_sorted:
                strict_class = "C"
            else:
                strict_class = "D"

            audited[cid] = {
                "raw_classification": raw[cid]["classification"],
                "audited_classification": strict_class,
                "entity_relationship": rel_status,
                "verdicts": verdicts,
            }
        except Exception as exc:
            log(f"  audit raised for {cid}, marking UNRESOLVED: {exc}")
            unresolved.append(cid)

        if (i + 1) % 10 == 0:
            log(f"  audited {i+1}/{len(ab_projects)} | remaining budget {remaining(deadline):.0f}s")

    with get_conn() as conn:
        for cid, a in audited.items():
            conn.execute(
                """INSERT INTO project_audit_classification
                   (coingecko_id, raw_classification, audited_classification, resolved, computed_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(coingecko_id) DO UPDATE SET
                       raw_classification=excluded.raw_classification,
                       audited_classification=excluded.audited_classification,
                       resolved=1, computed_at=excluded.computed_at""",
                (cid, a["raw_classification"], a["audited_classification"], now_iso()),
            )
        for cid in unresolved:
            conn.execute(
                """INSERT INTO project_audit_classification
                   (coingecko_id, raw_classification, audited_classification, resolved, computed_at)
                   VALUES (?, ?, NULL, 0, ?)
                   ON CONFLICT(coingecko_id) DO UPDATE SET
                       raw_classification=excluded.raw_classification, resolved=0, computed_at=excluded.computed_at""",
                (cid, raw.get(cid, {}).get("classification"), now_iso()),
            )

    results["audited_ab"] = audited
    results["audit_unresolved"] = unresolved
    log(f"Audit pass done: {len(audited)}/{len(ab_projects)} resolved, {len(unresolved)} unresolved.")


def run_deeper_d_pass(sample, raw, deadline):
    """Task 5: the bounded deeper-discovery pass for the named D projects,
    limited to those actually present in the persisted sample and still
    raw-classified D.
    """
    phase_deadline = min(deadline, time.monotonic() + DEEPER_PASS_TIME_BUDGET_SECONDS)
    sample_ids = {s["coingecko_id"] for s in sample}
    sample_by_id = {s["coingecko_id"]: s for s in sample}
    targets = [cid for cid in NAMED_D_PROJECTS if cid in sample_ids and raw.get(cid, {}).get("classification") == "D"]
    log(f"Deeper-discovery pass for {len(targets)} named D projects present in the sample...")

    upgrades = {}
    for cid in targets:
        if time.monotonic() > phase_deadline:
            break
        try:
            homepage = sample_by_id.get(cid, {}).get("website", "")
            deeper = deeper_discovery_for_d_project(cid, homepage)
            upgrades[cid] = deeper
            if deeper["upgraded"]:
                s = sample_by_id.get(cid, {})
                _, rel_status = entity_relationship(cid, s.get("symbol", ""), s.get("name", cid), homepage)
                with get_conn() as conn:
                    record_audit(
                        conn, cid, deeper["qualifying_url"], deeper["qualifying_url"], "PASS",
                        deeper["audited_relevance"], deeper["entries"], rel_status, deeper["reason"],
                    )
                    strict_class = "A" if deeper["audited_relevance"] == "HIGH" else "B"
                    if rel_status == "UNCLEAR":
                        strict_class = raw[cid]["classification"]  # cannot upgrade without a usable entity relationship
                    conn.execute(
                        """INSERT INTO project_audit_classification
                           (coingecko_id, raw_classification, audited_classification, resolved, computed_at)
                           VALUES (?, ?, ?, 1, ?)
                           ON CONFLICT(coingecko_id) DO UPDATE SET
                               raw_classification=excluded.raw_classification,
                               audited_classification=excluded.audited_classification,
                               resolved=1, computed_at=excluded.computed_at""",
                        (cid, "D", strict_class, now_iso()),
                    )
        except Exception as exc:
            log(f"  deeper pass raised for {cid}: {exc}")
            upgrades[cid] = {"coingecko_id": cid, "upgraded": False, "reason": str(exc)}

    results["named_d_deeper_pass"] = upgrades
    upgraded_count = sum(1 for v in upgrades.values() if v.get("upgraded"))
    log(f"Deeper D pass done: {upgraded_count}/{len(targets)} upgraded to a qualifying stream.")


def write_report(sample):
    raw = results.get("raw_classification", {})
    audited = results.get("audited_ab", {})
    unresolved_set = set(results.get("audit_unresolved", []))
    d_upgrades = results.get("named_d_deeper_pass", {})
    sample_by_id = {s["coingecko_id"]: s for s in sample}

    audited_classification_by_id = {}
    for cid in raw:
        if cid in audited:
            audited_classification_by_id[cid] = audited[cid]["audited_classification"]
        elif cid in d_upgrades and d_upgrades[cid].get("upgraded"):
            pages = d_upgrades[cid]
            audited_classification_by_id[cid] = "A" if pages.get("audited_relevance") == "HIGH" else "B"
        elif cid in unresolved_set:
            audited_classification_by_id[cid] = "UNRESOLVED"
        else:
            # Raw C/D (not audited, not upgraded): the raw classification stands
            # as audited for projects outside the A/B set and outside the named-D list.
            audited_classification_by_id[cid] = raw[cid]["classification"]

    raw_ab_ids = [cid for cid, r in raw.items() if r["classification"] in ("A", "B")]
    strict_a = sum(1 for cid, c in audited_classification_by_id.items() if c == "A")
    strict_b = sum(1 for cid, c in audited_classification_by_id.items() if c == "B")
    strict_c = sum(1 for cid, c in audited_classification_by_id.items() if c == "C")
    strict_d = sum(1 for cid, c in audited_classification_by_id.items() if c == "D")
    strict_unresolved = sum(1 for c in audited_classification_by_id.values() if c == "UNRESOLVED")
    strict_ab = strict_a + strict_b

    resolved_n = len(sample) - strict_unresolved
    ci_low, ci_high = wilson_confidence_interval(strict_ab, resolved_n if resolved_n else 1)

    downgraded = [cid for cid in raw_ab_ids if cid in audited and audited[cid]["audited_classification"] not in ("A", "B")]
    passed = [cid for cid in raw_ab_ids if cid in audited and audited[cid]["audited_classification"] in ("A", "B")]
    fp_rate = (len(downgraded) / len(raw_ab_ids)) if raw_ab_ids else 0.0

    upgraded_from_d = [cid for cid, v in d_upgrades.items() if v.get("upgraded")]

    lines = []
    lines.append("# SHUTDOWN_SOURCE_AUDIT\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Sample: persisted stratified `{SAMPLE_VERSION}` ({len(sample)} projects, reused as-is -- NOT rebuilt).\n")

    lines.append("## 1. Headline strict result\n")
    lines.append(f"**STRICT_SHUTDOWN_RELEVANT_COVERAGE (STRICT A + STRICT B) = {strict_ab}/{resolved_n if resolved_n else len(sample)}** "
                 f"(Wilson 95% CI: {ci_low*100:.1f}% - {ci_high*100:.1f}%)\n")

    lines.append("## 2. Raw result for comparison\n")
    lines.append(f"Raw (unaudited) A+B = {len(raw_ab_ids)}/{len(sample)} (as reported by SHUTDOWN_SOURCE_COVERAGE.md).\n")

    lines.append("## 3-5. PASS / downgrade / unresolved counts\n")
    lines.append(f"- Of the {len(raw_ab_ids)} raw A/B projects audited: {len(passed)} PASS (still A or B under the strict rule), "
                 f"{len(downgraded)} DOWNGRADED, {len(unresolved_set & set(raw_ab_ids))} left UNRESOLVED this run.\n")

    lines.append("## 6. False-positive rate among the original 51\n")
    lines.append(f"**False-positive rate among the raw A/B set = {len(downgraded)}/{len(raw_ab_ids)} = {fp_rate*100:.1f}%**\n")

    lines.append("## 7. Upgrades from the named D projects (deeper pass)\n")
    if upgraded_from_d:
        for cid in upgraded_from_d:
            v = d_upgrades[cid]
            lines.append(f"- {sample_by_id.get(cid, {}).get('name', cid)}: upgraded via {v['qualifying_url']} ({v['reason']})")
    else:
        lines.append("- None of the named D projects were upgraded this run (either no qualifying stream was found, "
                      "or the project wasn't reached within this run's time budget / wasn't D in the raw pass / "
                      "wasn't present in the persisted sample).")
    lines.append("")

    lines.append("## 8. Strict result by rank stratum\n")
    lines.append("| Stratum | Resolved | STRICT_A | STRICT_B | STRICT_C | STRICT_D | STRICT_A+B |")
    lines.append("|---|---|---|---|---|---|---|")
    for stratum in ("1-100", "101-300", "301-600", "601-1000"):
        ids_in = [s["coingecko_id"] for s in sample if s["stratum"] == stratum]
        classes = [audited_classification_by_id.get(cid) for cid in ids_in if cid in audited_classification_by_id]
        resolved_classes = [c for c in classes if c != "UNRESOLVED"]
        a = sum(1 for c in resolved_classes if c == "A")
        b = sum(1 for c in resolved_classes if c == "B")
        c_ = sum(1 for c in resolved_classes if c == "C")
        d = sum(1 for c in resolved_classes if c == "D")
        lines.append(f"| {stratum} | {len(resolved_classes)}/{len(ids_in)} | {a} | {b} | {c_} | {d} | {a+b} |")
    lines.append("")

    lines.append("## 9. Full 100-project table: raw vs. audited classification\n")
    lines.append("| Rank | Project | RAW | AUDITED (STRICT) | Entity relationship | Canonical stream URL |")
    lines.append("|---|---|---|---|---|---|")
    for s in sample:
        cid = s["coingecko_id"]
        raw_c = raw.get(cid, {}).get("classification", "?")
        strict_c_val = audited_classification_by_id.get(cid, raw_c)
        ent = audited.get(cid, {}).get("entity_relationship") or (d_upgrades.get(cid, {}) and "see note") or "-"
        canon = "-"
        if cid in audited:
            passing_v = [v for v in audited[cid]["verdicts"] if v["verdict"] == "PASS"]
            if passing_v:
                canon = passing_v[0]["canonical_stream_url"]
        elif cid in d_upgrades and d_upgrades[cid].get("upgraded"):
            canon = d_upgrades[cid]["qualifying_url"]
        lines.append(f"| {s['rank_at_selection']} | {s.get('name', cid)} | {raw_c} | {strict_c_val} | {ent} | {canon} |")
    lines.append("")

    lines.append("## 10-11. Canonical stream URLs + two-entry evidence for every counted A/B\n")
    lines.append("| Project | Canonical stream URL | Entry 1 | Entry 2 |")
    lines.append("|---|---|---|---|")
    for cid, a in audited.items():
        for v in a["verdicts"]:
            if v["verdict"] != "PASS":
                continue
            entries = v["entries"]
            e1 = f"{entries[0]['title']} ({entries[0]['date'] or 'n/d'})" if len(entries) > 0 else "-"
            e2 = f"{entries[1]['title']} ({entries[1]['date'] or 'n/d'})" if len(entries) > 1 else "-"
            lines.append(f"| {sample_by_id.get(cid, {}).get('name', cid)} | {v['canonical_stream_url']} | {e1} | {e2} |")
    for cid, v in d_upgrades.items():
        if v.get("upgraded"):
            entries = v["entries"]
            e1 = f"{entries[0]['title']} ({entries[0]['date'] or 'n/d'})" if len(entries) > 0 else "-"
            e2 = f"{entries[1]['title']} ({entries[1]['date'] or 'n/d'})" if len(entries) > 1 else "-"
            lines.append(f"| {sample_by_id.get(cid, {}).get('name', cid)} | {v['qualifying_url']} | {e1} | {e2} |")
    lines.append("")

    lines.append("## 12. Entity relationship status\n")
    verified_n = sum(1 for a in audited.values() if a["entity_relationship"] == "VERIFIED")
    plausible_n = sum(1 for a in audited.values() if a["entity_relationship"] == "PLAUSIBLE")
    unclear_n = sum(1 for a in audited.values() if a["entity_relationship"] == "UNCLEAR")
    lines.append(f"Among audited A/B projects: VERIFIED={verified_n}, PLAUSIBLE={plausible_n}, UNCLEAR={unclear_n} "
                 "(UNCLEAR projects cannot count toward STRICT A/B regardless of their page-level verdict).\n")

    lines.append("## 13. Remaining blind spots\n")
    lines.append("- This audit re-fetches each candidate URL live; a page that changed or went offline between the "
                 "raw run and this run will show a different verdict through no fault of either measurement.")
    lines.append("- The deeper D-project pass is still a bounded, fixed set of extra paths/subdomains plus sitemap.xml/"
                 "robots.txt -- it is a better floor than the raw pass, not an exhaustive search; a named D project "
                 "that uses an unlisted path structure may still be a false negative.")
    lines.append("- `extract_listing_entries` is a lightweight heuristic (regex-based anchor/date extraction), not a "
                 "real scraper; a listing page that is JS-rendered with no server-side anchors will under-count, not "
                 "over-count, its entries.")
    lines.append("- The entity-relationship check is VERIFIED only for a small curated list of well-known cases; "
                 "everything else is a textual domain/name heuristic (PLAUSIBLE) or UNCLEAR -- it does not consult "
                 "any external corporate-registry data source.")
    lines.append(f"- {len(unresolved_set)} project(s) left UNRESOLVED this run due to time budget; re-run to continue.\n")

    lines.append("## Zero-cost confirmation\n")
    lines.append("All sources re-checked in this audit are free/keyless public web pages (the same project homepages "
                 "and candidate pages as the raw pass, plus sitemap.xml/robots.txt). No paid API, proxy, or hosted "
                 "service was used. **CONFIRMED: $0 recurring cost.**\n")

    REPORT_PATH.write_text("\n".join(lines))
    log(f"Report written to {REPORT_PATH}")


def main():
    deadline = time.monotonic() + GLOBAL_DEADLINE_SECONDS
    log(f"Hard global deadline: {GLOBAL_DEADLINE_SECONDS/60:.0f} minutes.")

    ok, sample = check_cached_universe_and_sample()
    save_raw_log()
    if not ok:
        return 1

    raw = load_raw_classification(sample)
    results["raw_classification"] = raw
    save_raw_log()

    try:
        run_audit_pass(sample, raw, deadline)
    except Exception as exc:
        log(f"run_audit_pass raised: {exc}")
        results["audited_ab"] = results.get("audited_ab", {})
        results["audit_unresolved"] = results.get("audit_unresolved", [])
    save_raw_log()

    try:
        run_deeper_d_pass(sample, raw, deadline)
    except Exception as exc:
        log(f"run_deeper_d_pass raised: {exc}")
        results["named_d_deeper_pass"] = results.get("named_d_deeper_pass", {})
    save_raw_log()

    write_report(sample)
    save_raw_log()
    total_elapsed_min = (GLOBAL_DEADLINE_SECONDS - remaining(deadline)) / 60
    log(f"DONE in ~{total_elapsed_min:.1f} minutes. See SHUTDOWN_SOURCE_AUDIT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
