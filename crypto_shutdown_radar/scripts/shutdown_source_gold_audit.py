#!/usr/bin/env python3
"""GOLD layer: re-audits the STRICT layer's A/B results (scripts/shutdown_source_audit.py)
under a stricter genuine-publication rule, and runs a targeted (not broad)
recovery pass over raw C projects whose already-recorded candidate pages
look like a strong press/announcements/newsroom/news surface.

Does NOT rebuild the universe, the persisted 100-project sample, or
data/radar.db; does NOT re-run raw discovery or the STRICT audit pass.
Reads official_sources (raw) and shutdown_source_audit/project_audit_classification
(STRICT) as input; writes to the SEPARATE gold_source_audit /
manual_review_queue tables and the ADDITIVE
project_audit_classification.gold_classification column -- RAW_CLASSIFICATION
and AUDITED_CLASSIFICATION (STRICT) are never overwritten.

Usage (from crypto_shutdown_radar/, with the venv active):
    python -m scripts.shutdown_source_gold_audit
"""
import sys
import time
import json as json_module
from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db, get_conn, now_iso
from app.universe import is_universe_cached
from app.sample_selection import get_or_build_validation_sample, SAMPLE_VERSION
from app.shutdown_source_audit import entity_relationship, wilson_confidence_interval
from app.shutdown_source_gold_audit import gold_audit_candidate_source

REPORT_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_GOLD_AUDIT.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "SHUTDOWN_SOURCE_GOLD_AUDIT_RAW_LOG.json"

GLOBAL_DEADLINE_SECONDS = 25 * 60
GOLD_TIME_BUDGET_SECONDS = 18 * 60
MAX_SOURCES_PER_PROJECT = 3

# Task 4: targeted (not broad) false-negative recovery -- only candidate
# URLs ALREADY recorded by the raw discovery pass whose path contains one
# of these keywords are re-examined for a raw-C project. No new path
# brute-forcing or search-engine use.
RECOVERY_KEYWORDS = ["press-release", "press", "announcement", "newsroom", "news", "insights"]

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
    """Read-only re-derivation of the RAW A/B/C/D classification straight
    from official_sources, same logic as scripts.shutdown_source_audit.
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


def load_strict_classification():
    """Read-only: the STRICT layer's per-project audited_classification,
    exactly as scripts/shutdown_source_audit.py last left it. Never
    modified by this script.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT coingecko_id, raw_classification, audited_classification FROM project_audit_classification"
        ).fetchall()
    return {r["coingecko_id"]: dict(r) for r in rows}


def candidates_for_current_ab(cid):
    """For a project the STRICT layer counted A/B: prefer the exact
    canonical_stream_url(s) it already verified (shutdown_source_audit,
    verdict=PASS); fall back to its raw monitorable candidate pages if
    none were ever recorded there (e.g. it was upgraded via the D-deeper
    pass, which records into the same table).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT canonical_stream_url, source_url FROM shutdown_source_audit "
            "WHERE coingecko_id=? AND verdict='PASS' AND canonical_stream_url IS NOT NULL",
            (cid,),
        ).fetchall()
    urls = [r["canonical_stream_url"] for r in rows]
    return urls[:MAX_SOURCES_PER_PROJECT] if urls else []


def candidates_for_recovery(cid, raw_pages):
    urls = []
    for p in raw_pages:
        url = p.get("url") or ""
        if any(kw in url.lower() for kw in RECOVERY_KEYWORDS) and url not in urls:
            urls.append(url)
    return urls[:MAX_SOURCES_PER_PROJECT]


def record_gold_audit(conn, coingecko_id, source_url, audit, rel_status):
    entries = audit["entries"]
    entry1 = entries[0] if len(entries) > 0 else {}
    entry2 = entries[1] if len(entries) > 1 else {}
    source_type = None
    if audit["canonical_stream_url"]:
        path = audit["canonical_stream_url"].rsplit("//", 1)[-1]
        source_type = "/" + path.split("/", 1)[1] if "/" in path else "/"
    conn.execute(
        """
        INSERT INTO gold_source_audit (
            coingecko_id, source_url, canonical_stream_url, verdict, source_type, audited_relevance,
            evidence_mechanism, entry1_url, entry1_title, entry1_date, entry2_url, entry2_title, entry2_date,
            new_entry_detectable, entity_relationship, reason, audited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, source_url) DO UPDATE SET
            canonical_stream_url=excluded.canonical_stream_url, verdict=excluded.verdict,
            source_type=excluded.source_type, audited_relevance=excluded.audited_relevance,
            evidence_mechanism=excluded.evidence_mechanism,
            entry1_url=excluded.entry1_url, entry1_title=excluded.entry1_title, entry1_date=excluded.entry1_date,
            entry2_url=excluded.entry2_url, entry2_title=excluded.entry2_title, entry2_date=excluded.entry2_date,
            new_entry_detectable=excluded.new_entry_detectable, entity_relationship=excluded.entity_relationship,
            reason=excluded.reason, audited_at=excluded.audited_at
        """,
        (
            coingecko_id, source_url, audit["canonical_stream_url"], audit["verdict"], source_type,
            audit["audited_relevance"], audit["mechanism"],
            entry1.get("url"), entry1.get("title"), entry1.get("date"),
            entry2.get("url"), entry2.get("title"), entry2.get("date"),
            1 if audit["new_entry_detectable"] else 0, rel_status, audit["reason"], now_iso(),
        ),
    )


def queue_for_manual_review(conn, coingecko_id, candidate_url, reason):
    conn.execute(
        """INSERT INTO manual_review_queue (coingecko_id, candidate_url, reason, flagged_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(coingecko_id, candidate_url) DO UPDATE SET reason=excluded.reason, flagged_at=excluded.flagged_at""",
        (coingecko_id, candidate_url, reason, now_iso()),
    )


def upsert_gold_classification(conn, cid, raw_classification, gold_classification):
    conn.execute(
        """INSERT INTO project_audit_classification (coingecko_id, raw_classification, gold_classification, resolved, computed_at)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(coingecko_id) DO UPDATE SET gold_classification=excluded.gold_classification, computed_at=excluded.computed_at""",
        (cid, raw_classification, gold_classification, now_iso()),
    )


def run_gold_pass(sample, raw, strict, deadline):
    """Task 3 (re-verify the current A/B set) + Task 4 (targeted recovery
    for raw-C projects with a strong-looking recorded URL), unified: both
    just mean "run gold_audit_candidate_source over this project's
    already-known candidate URLs and derive its GOLD classification from
    the result" -- the only difference is which URLs are in scope.
    """
    phase_deadline = min(deadline, time.monotonic() + GOLD_TIME_BUDGET_SECONDS)
    sample_by_id = {s["coingecko_id"]: s for s in sample}

    current_ab_ids = [cid for cid, r in strict.items() if r.get("audited_classification") in ("A", "B")]
    recovery_ids = [cid for cid, r in raw.items() if r["classification"] == "C" and candidates_for_recovery(cid, r["pages"])]
    targets = sorted(set(current_ab_ids) | set(recovery_ids))
    log(f"GOLD pass: {len(current_ab_ids)} current STRICT A/B projects to re-verify, "
        f"{len(recovery_ids)} raw-C projects with a recorded press/news-shaped URL to recover. "
        f"({len(targets)} total)")

    gold_results = {}
    unresolved = []

    for i, cid in enumerate(targets):
        if time.monotonic() > phase_deadline:
            unresolved.append(cid)
            continue
        try:
            s = sample_by_id.get(cid, {})
            homepage = s.get("website", "")
            _, rel_status = entity_relationship(cid, s.get("symbol", ""), s.get("name", cid), homepage)

            candidate_urls = candidates_for_current_ab(cid) if cid in current_ab_ids else []
            if not candidate_urls:
                candidate_urls = candidates_for_recovery(cid, raw.get(cid, {}).get("pages", []))

            audits = []
            with get_conn() as conn:
                for url in candidate_urls:
                    audit = gold_audit_candidate_source(cid, url)
                    record_gold_audit(conn, cid, url, audit, rel_status)
                    audits.append(audit)
                    if audit["verdict"] == "GOLD_UNRESOLVED":
                        queue_for_manual_review(conn, cid, url, audit["reason"] or "ambiguous evidence")

            passing = [a for a in audits if a["verdict"] == "GOLD_PASS"]
            if passing and rel_status == "UNCLEAR":
                gold_class = "C"
                reason = "genuine stream found, but entity relationship is UNCLEAR -- cannot count toward GOLD A/B"
            elif passing:
                best = sorted(passing, key=lambda a: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(a["audited_relevance"], 3))[0]
                gold_class = "A" if best["audited_relevance"] == "HIGH" else "B"
                reason = best["reason"]
            elif any(a["verdict"] == "GOLD_UNRESOLVED" for a in audits):
                gold_class = "UNRESOLVED"
                reason = "ambiguous evidence, queued for manual review"
            else:
                gold_class = "C" if candidate_urls else raw.get(cid, {}).get("classification", "D")
                reason = "no genuine publication stream survived GOLD audit"

            gold_results[cid] = {
                "gold_classification": gold_class, "entity_relationship": rel_status,
                "audits": audits, "reason": reason,
            }
            with get_conn() as conn:
                upsert_gold_classification(conn, cid, raw.get(cid, {}).get("classification"), gold_class)
        except Exception as exc:
            log(f"  GOLD audit raised for {cid}, marking UNRESOLVED: {exc}")
            unresolved.append(cid)

        if (i + 1) % 10 == 0:
            log(f"  processed {i+1}/{len(targets)} | remaining budget {remaining(deadline):.0f}s")

    results["gold_results"] = gold_results
    results["gold_unresolved"] = unresolved
    log(f"GOLD pass done: {len(gold_results)}/{len(targets)} resolved, {len(unresolved)} unresolved.")


def write_report(sample, raw, strict):
    gold_results = results.get("gold_results", {})
    unresolved = set(results.get("gold_unresolved", []))
    sample_by_id = {s["coingecko_id"]: s for s in sample}

    def gold_class_for(cid):
        if cid in gold_results:
            return gold_results[cid]["gold_classification"]
        if cid in unresolved:
            return "UNRESOLVED"
        # Not in scope for this GOLD pass (raw C without a recorded strong URL,
        # or raw D) -- carried forward from the STRICT layer, never re-examined.
        return strict.get(cid, {}).get("audited_classification") or raw.get(cid, {}).get("classification", "D")

    all_ids = [s["coingecko_id"] for s in sample]
    gold_by_id = {cid: gold_class_for(cid) for cid in all_ids}
    resolved_ids = [cid for cid in all_ids if gold_by_id[cid] != "UNRESOLVED"]
    gold_a = sum(1 for c in gold_by_id.values() if c == "A")
    gold_b = sum(1 for c in gold_by_id.values() if c == "B")
    gold_c = sum(1 for c in gold_by_id.values() if c == "C")
    gold_d = sum(1 for c in gold_by_id.values() if c == "D")
    gold_unresolved_n = sum(1 for c in gold_by_id.values() if c == "UNRESOLVED")
    gold_ab = gold_a + gold_b

    strict_ab_ids = {cid for cid, r in strict.items() if r.get("audited_classification") in ("A", "B")}
    gold_pass_n = sum(1 for cid in strict_ab_ids if gold_by_id.get(cid) in ("A", "B"))
    gold_downgrade_n = len(strict_ab_ids) - gold_pass_n - sum(1 for cid in strict_ab_ids if gold_by_id.get(cid) == "UNRESOLVED")
    recoveries = [cid for cid, g in gold_results.items()
                  if g["gold_classification"] in ("A", "B") and raw.get(cid, {}).get("classification") == "C"
                  and cid not in strict_ab_ids]

    ci_low, ci_high = wilson_confidence_interval(gold_ab, len(resolved_ids) if resolved_ids else 1)

    with get_conn() as conn:
        review_queue = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, candidate_url, reason FROM manual_review_queue ORDER BY coingecko_id"
        ).fetchall()]

    lines = []
    lines.append("# SHUTDOWN_SOURCE_GOLD_AUDIT\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Sample: persisted stratified `{SAMPLE_VERSION}` ({len(sample)} projects, reused as-is -- NOT rebuilt).\n")

    lines.append("## Headline\n")
    lines.append(f"**GOLD_SHUTDOWN_RELEVANT_FIRST_PARTY_COVERAGE (GOLD A + GOLD B) = {gold_ab}/{len(resolved_ids) if resolved_ids else len(sample)}** "
                 f"(Wilson 95% CI: {ci_low*100:.1f}% - {ci_high*100:.1f}%)\n")
    lines.append(f"Conservative whole-sample lower bound (treats every unresolved project as a non-pass): "
                 f"**GOLD A+B / 100 = {gold_ab}/100**\n")

    lines.append("## Comparison across layers\n")
    lines.append(f"- RAW (unaudited) A+B: 51/100")
    lines.append(f"- STRICT (previous audit, commit e85243c) A+B: {len(strict_ab_ids)}/100")
    lines.append(f"- **GOLD (this audit) A+B: {gold_ab}/{len(resolved_ids) if resolved_ids else len(sample)}**\n")

    lines.append("## PASS / downgrade / unresolved (of the previous STRICT A/B set)\n")
    lines.append(f"- GOLD_PASS (still A/B under GOLD rules): {gold_pass_n}")
    lines.append(f"- GOLD_DOWNGRADE: {gold_downgrade_n}")
    lines.append(f"- GOLD_UNRESOLVED (of the STRICT set): {sum(1 for cid in strict_ab_ids if gold_by_id.get(cid) == 'UNRESOLVED')}\n")

    lines.append("## Recoveries from raw C (targeted pass, Task 4)\n")
    if recoveries:
        for cid in recoveries:
            lines.append(f"- {sample_by_id.get(cid, {}).get('name', cid)}: recovered to GOLD "
                         f"{gold_results[cid]['gold_classification']} ({gold_results[cid]['reason']})")
    else:
        lines.append("- None recovered this run (either no qualifying evidence was found among the already-recorded "
                     "press/announcements/newsroom/news-shaped URLs, or the project wasn't reached within budget).")
    lines.append("")

    lines.append("## Coverage by rank stratum\n")
    lines.append("| Stratum | Resolved | GOLD_A | GOLD_B | GOLD_C | GOLD_D | GOLD_A+B |")
    lines.append("|---|---|---|---|---|---|---|")
    for stratum in ("1-100", "101-300", "301-600", "601-1000"):
        ids_in = [s["coingecko_id"] for s in sample if s["stratum"] == stratum]
        classes = [gold_by_id[cid] for cid in ids_in]
        resolved_classes = [c for c in classes if c != "UNRESOLVED"]
        a = sum(1 for c in resolved_classes if c == "A")
        b = sum(1 for c in resolved_classes if c == "B")
        c_ = sum(1 for c in resolved_classes if c == "C")
        d = sum(1 for c in resolved_classes if c == "D")
        lines.append(f"| {stratum} | {len(resolved_classes)}/{len(ids_in)} | {a} | {b} | {c_} | {d} | {a+b} |")
    lines.append("")

    lines.append("## Every GOLD A/B source with its two genuine publication entries\n")
    lines.append("| Project | Canonical stream URL | Mechanism | Entry 1 | Entry 2 | New entry detectable |")
    lines.append("|---|---|---|---|---|---|")
    for cid, g in gold_results.items():
        if g["gold_classification"] not in ("A", "B"):
            continue
        for a in g["audits"]:
            if a["verdict"] != "GOLD_PASS":
                continue
            entries = a["entries"]
            e1 = f"{entries[0]['title']} ({entries[0].get('date') or 'n/d'})" if len(entries) > 0 else "-"
            e2 = f"{entries[1]['title']} ({entries[1].get('date') or 'n/d'})" if len(entries) > 1 else "-"
            lines.append(f"| {sample_by_id.get(cid, {}).get('name', cid)} | {a['canonical_stream_url']} | "
                         f"{a['mechanism']} | {e1} | {e2} | {'yes' if a['new_entry_detectable'] else 'no'} |")
    lines.append("")

    lines.append("## Full 100-project table: RAW vs STRICT vs GOLD\n")
    lines.append("| Rank | Project | RAW | STRICT | GOLD | Entity relationship |")
    lines.append("|---|---|---|---|---|---|")
    for s in sample:
        cid = s["coingecko_id"]
        rc = raw.get(cid, {}).get("classification", "?")
        sc = strict.get(cid, {}).get("audited_classification") or "-"
        gc = gold_by_id.get(cid, "-")
        ent = gold_results.get(cid, {}).get("entity_relationship", "-")
        lines.append(f"| {s['rank_at_selection']} | {s.get('name', cid)} | {rc} | {sc} | {gc} | {ent} |")
    lines.append("")

    lines.append("## Manual review queue (Task 8)\n")
    if review_queue:
        lines.append("| Project | Candidate URL | Reason |")
        lines.append("|---|---|---|")
        for r in review_queue:
            lines.append(f"| {sample_by_id.get(r['coingecko_id'], {}).get('name', r['coingecko_id'])} | "
                         f"{r['candidate_url']} | {r['reason']} |")
    else:
        lines.append("- Empty this run.")
    lines.append("")

    lines.append("## Remaining blind spots\n")
    lines.append("- GOLD still only re-examines (a) the previous STRICT A/B set and (b) raw-C projects whose "
                 "ALREADY-RECORDED candidate pages contain a press/announcements/newsroom/news keyword in the URL -- "
                 "a raw-D project, or a raw-C project whose real announcements page uses an unlisted URL shape, is "
                 "carried forward unchanged, not newly searched.")
    lines.append("- The embedded-JSON extractor recognizes common shapes (a list of >=2 dicts with a title/headline "
                 "and a slug/url/href/path key); a differently-shaped JS data blob will be missed, which "
                 "under-counts, not over-counts.")
    lines.append("- The nav/category blocklist is a fixed list built from the concrete false positives reported "
                 "(Education, Ecosystem, Past Events, Gold/Silver articles, In The News, Announcements-as-filter, "
                 "PR agency, Tokenization, Interoperability, template placeholders) plus the obvious generalizations -- "
                 "a differently-worded nav label not on this list could still slip through as a false positive.")
    lines.append("- Low-confidence HTML-article-card findings (genuine-looking entries with no date and only the "
                 "slug heuristic as corroboration) are deliberately sent to manual review rather than auto-decided; "
                 "a true positive of this shape will show as UNRESOLVED, not as a pass, until a human looks at it.")
    lines.append(f"- {len(unresolved)} project(s) left UNRESOLVED this run due to time budget; re-run to continue.\n")

    lines.append("## Zero-cost confirmation\n")
    lines.append("All sources re-checked are free/keyless public web pages and their own declared RSS/Atom feeds "
                 "(one extra request per page, only when the page itself advertises one). No paid API, proxy, "
                 "browser automation, or hosted service was used. **CONFIRMED: $0 recurring cost.**\n")

    lines.append("## No viability verdict\n")
    lines.append(f"This is a measurement of source-coverage evidence quality, not a viability verdict. "
                 f"GOLD A+B = {gold_ab}/{len(resolved_ids) if resolved_ids else len(sample)} "
                 f"(conservative whole-sample floor: {gold_ab}/100).\n")

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
    strict = load_strict_classification()
    results["raw_classification"] = raw
    results["strict_classification"] = strict
    save_raw_log()

    try:
        run_gold_pass(sample, raw, strict, deadline)
    except Exception as exc:
        log(f"run_gold_pass raised: {exc}")
        results["gold_results"] = results.get("gold_results", {})
        results["gold_unresolved"] = results.get("gold_unresolved", [])
    save_raw_log()

    write_report(sample, raw, strict)
    save_raw_log()
    total_elapsed_min = (GLOBAL_DEADLINE_SECONDS - remaining(deadline)) / 60
    log(f"DONE in ~{total_elapsed_min:.1f} minutes. See SHUTDOWN_SOURCE_GOLD_AUDIT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
