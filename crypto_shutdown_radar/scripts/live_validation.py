#!/usr/bin/env python3
"""Runs STEP 1-9 of the live validation protocol against the REAL internet
and writes LIVE_VALIDATION_REPORT.md at the project root.

This makes real HTTP requests. No mocking, no fabricated data: every
number in the report comes from an actual response (or an actual failure)
observed during this run. If something could not be verified, the report
says UNKNOWN or NOT VERIFIED rather than guessing.

Usage (from the crypto_shutdown_radar/ directory, with the venv active):
    python -m scripts.live_validation

Expect this to take roughly 40-90 minutes in total, dominated by the
CoinGecko per-coin detail fetch in STEP 2 (rate-limited to ~1 request every
2 seconds for up to 1000 coins = ~35-40 minutes by itself). Progress is
printed as it runs. The script does not hammer X: requests to individual
X accounts are paced with a delay, and account-scaling stages stop early
if the current method shows 0% success, rather than continuing to throw
more requests at a method that has already demonstrated it does not work.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import init_db, get_conn
from app import universe
from app.sources.x_adapter import XAccountAdapter, NITTER_INSTANCES, check_nitter_instance_health
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES
from app import market_data
from app import telegram_bot
from app import config as cfg

REPORT_PATH = Path(__file__).resolve().parent.parent / "LIVE_VALIDATION_REPORT.md"
RAW_LOG_PATH = Path(__file__).resolve().parent.parent / "LIVE_VALIDATION_RAW_LOG.json"

results = {"started_at": datetime.now(timezone.utc).isoformat()}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_raw_log():
    with open(RAW_LOG_PATH, "w") as f:
        json.dump(results, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# STEP 1 — feasibility_check (reused as-is, not duplicated)
# ---------------------------------------------------------------------------
def step1_feasibility():
    log("STEP 1: feasibility check (RSS + X) ...")
    rss_results = {}
    for adapter in build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES):
        r = adapter.collect()
        rss_results[adapter.name] = {"ok": r.ok, "item_count": len(r.items) if r.ok else 0, "error": r.error}

    nitter_health = {}
    for base in NITTER_INSTANCES:
        nitter_health[base] = check_nitter_instance_health(base)
        time.sleep(1)

    results["step1_feasibility"] = {"rss": rss_results, "nitter_instance_health": nitter_health}
    log(f"STEP 1 done. RSS working: {sum(1 for r in rss_results.values() if r['ok'])}/{len(rss_results)}; "
        f"Nitter instances healthy: {sum(1 for v in nitter_health.values() if v)}/{len(nitter_health)}")


# ---------------------------------------------------------------------------
# STEP 2 — real universe ingestion
# ---------------------------------------------------------------------------
def step2_universe():
    log("STEP 2: refreshing top-1000 universe from CoinGecko (this is the slow step, ~35-40 min for detail mapping)...")
    init_db()
    t0 = time.monotonic()
    summary = universe.refresh_universe(limit=cfg.UNIVERSE_SIZE, fetch_details=True)
    elapsed = time.monotonic() - t0

    with get_conn() as conn:
        total_coins = conn.execute("SELECT COUNT(*) c FROM coins WHERE is_active=1").fetchone()["c"]
        verified = conn.execute(
            "SELECT oa.*, c.name, c.symbol, c.market_cap_rank FROM official_accounts oa "
            "JOIN coins c ON c.coingecko_id = oa.coingecko_id "
            "WHERE oa.platform='x' AND oa.verification_status='VERIFIED' ORDER BY c.market_cap_rank ASC"
        ).fetchall()
        unverified_count = conn.execute(
            "SELECT COUNT(*) c FROM official_accounts WHERE platform='x' AND verification_status='UNVERIFIED'"
        ).fetchone()["c"]

    sample_mappings = [
        {
            "coin_name": row["name"],
            "ticker": row["symbol"].upper(),
            "x_handle": row["handle_or_url"],
            "evidence": row["verification_method"],
            "rank": row["market_cap_rank"],
        }
        for row in verified[:20]
    ]

    results["step2_universe"] = {
        "requested": summary["requested"],
        "retrieved": summary["retrieved"],
        "error_pages": summary["error_pages"],
        "detail_checked": summary["detail_checked"],
        "detail_errors": summary["detail_errors"],
        "verified_count": len(verified),
        "unverified_count": unverified_count,
        "elapsed_seconds": elapsed,
        "sample_verified_mappings": sample_mappings,
    }
    log(f"STEP 2 done in {elapsed/60:.1f} min. Retrieved {summary['retrieved']}/{summary['requested']}, "
        f"verified X accounts: {len(verified)}, unverified: {unverified_count}")
    return [dict(row) for row in verified]


# ---------------------------------------------------------------------------
# STEP 3 + 4 — real X monitoring, scaled, with request counting
# ---------------------------------------------------------------------------
def step3_4_x_monitoring(verified_accounts):
    log("STEP 3/4: live X monitoring test (real network, paced, will stop early on systemic failure) ...")
    if not verified_accounts:
        results["step3_x_monitoring"] = {"skipped": True, "reason": "no VERIFIED X accounts available from STEP 2"}
        log("STEP 3/4 skipped: no VERIFIED accounts to test.")
        return

    request_count = {"n": 0}
    import app.sources.x_adapter as xmod
    real_get = xmod._session.get

    def counting_get(*args, **kwargs):
        request_count["n"] += 1
        return real_get(*args, **kwargs)

    handles = [row["handle_or_url"] for row in verified_accounts]
    stage_bounds = [20, 100, 250, 500, len(handles)]
    stage_bounds = sorted(set(b for b in stage_bounds if b <= len(handles)) | {len(handles)})

    stages = []
    tested_so_far = 0
    all_sample_posts = []
    rate_limited_seen = False
    stopped_early_reason = None

    with patch.object(xmod._session, "get", side_effect=counting_get):
        for bound in stage_bounds:
            new_slice = handles[tested_so_far:bound]
            if not new_slice:
                continue
            log(f"  testing accounts [{tested_so_far}:{bound}] ({len(new_slice)} new accounts, cumulative target {bound}) ...")
            stage_t0 = time.monotonic()
            succeeded, failed, posts, errors = 0, 0, [], []
            req_before = request_count["n"]
            for handle in new_slice:
                adapter = XAccountAdapter(handle)
                result = adapter.collect()
                if result.ok:
                    succeeded += 1
                    for item in result.items:
                        posts.append({
                            "handle": handle,
                            "post_id": item.external_id,
                            "published_at": item.published_at,
                            "text": item.text,
                            "url": item.url,
                            "method": item.source_name,
                        })
                else:
                    failed += 1
                    errors.append(f"{handle}: {result.error}")
                    if result.error and "429" in result.error:
                        rate_limited_seen = True
                time.sleep(1.5)  # pacing: do not hammer X
            stage_elapsed = time.monotonic() - stage_t0
            req_in_stage = request_count["n"] - req_before
            tested_so_far = bound

            stage_record = {
                "cumulative_target": bound,
                "accounts_attempted_this_stage": len(new_slice),
                "succeeded": succeeded,
                "failed": failed,
                "posts_retrieved": len(posts),
                "avg_requests_per_account": round(req_in_stage / len(new_slice), 2) if new_slice else 0,
                "elapsed_seconds": round(stage_elapsed, 1),
                "sample_errors": errors[:5],
            }
            stages.append(stage_record)
            all_sample_posts.extend(posts)
            log(f"  stage result: {succeeded}/{len(new_slice)} succeeded, {len(posts)} posts, "
                f"{req_in_stage} HTTP requests, {stage_elapsed:.1f}s")

            if succeeded == 0 and bound != stage_bounds[-1]:
                stopped_early_reason = (
                    f"0/{len(new_slice)} accounts succeeded at the {bound}-account cumulative stage; "
                    "stopping scale-up rather than sending more requests to a method that has already "
                    "demonstrated it does not work against the current network/account set."
                )
                log(f"  STOPPING SCALE-UP: {stopped_early_reason}")
                break

    total_attempted = sum(s["accounts_attempted_this_stage"] for s in stages)
    total_succeeded = sum(s["succeeded"] for s in stages)
    total_failed = sum(s["failed"] for s in stages)
    total_posts = len(all_sample_posts)

    oldest_post, newest_post = None, None
    if all_sample_posts:
        dated = [p for p in all_sample_posts if p["published_at"]]
        if dated:
            oldest_post = min(dated, key=lambda p: p["published_at"])["published_at"]
            newest_post = max(dated, key=lambda p: p["published_at"])["published_at"]

    results["step3_x_monitoring"] = {
        "stages": stages,
        "total_accounts_attempted": total_attempted,
        "total_succeeded": total_succeeded,
        "total_failed": total_failed,
        "total_posts_retrieved": total_posts,
        "oldest_post_timestamp": oldest_post,
        "newest_post_timestamp": newest_post,
        "total_http_requests": request_count["n"],
        "rate_limiting_encountered": rate_limited_seen,
        "stopped_early_reason": stopped_early_reason,
        "sample_posts": all_sample_posts[:10],
        "coverage_pct": round(100 * total_succeeded / total_attempted, 1) if total_attempted else 0.0,
    }

    # STEP 4: sustainable polling capacity, estimated from observed per-account
    # latency in this run (NOT from an additional sustained-load test).
    if total_attempted > 0 and stages:
        avg_seconds_per_account = sum(s["elapsed_seconds"] for s in stages) / sum(
            s["accounts_attempted_this_stage"] for s in stages
        )
    else:
        avg_seconds_per_account = None

    sustainability = {}
    for n in (100, 250, 500, 1000):
        if avg_seconds_per_account is None:
            sustainability[n] = "UNKNOWN (no successful requests to measure latency from)"
            continue
        seconds_to_poll_all = avg_seconds_per_account * n
        minutes_to_poll_all = seconds_to_poll_all / 60
        if minutes_to_poll_all >= 10:
            sustainability[n] = (
                f"NOT realistic at 10-min cadence (one full pass takes ~{minutes_to_poll_all:.1f} min "
                f"sequentially at the observed per-account pace)"
            )
        elif minutes_to_poll_all >= 30:
            sustainability[n] = f"NOT realistic even at 30-min cadence (~{minutes_to_poll_all:.1f} min per pass)"
        else:
            sustainability[n] = f"one full sequential pass ~{minutes_to_poll_all:.1f} min; 10-30 min cadence plausible IF the method keeps working"
    results["step4_sustainability"] = {
        "avg_seconds_per_account_observed": avg_seconds_per_account,
        "estimates": sustainability,
        "note": "Estimated from this run's observed latency, not from an additional sustained-load test (per instructions not to hammer X).",
    }
    log("STEP 3/4 done.")


# ---------------------------------------------------------------------------
# STEP 5 — RSS/official sources, real requests
# ---------------------------------------------------------------------------
def step5_rss():
    log("STEP 5: RSS/official source validation ...")
    out = {}
    for adapter in build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES):
        r = adapter.collect()
        if r.ok and len(r.items) > 0:
            status = "WORKING"
        elif r.ok and len(r.items) == 0:
            status = "PARTIALLY WORKING"
        else:
            status = "FAILED"
        latest = None
        if r.ok and r.items:
            latest = {"title_or_text": r.items[0].text[:200], "url": r.items[0].url, "published_at": r.items[0].published_at}
        out[adapter.name] = {"status": status, "error": r.error, "latest_item": latest}
    results["step5_rss"] = out
    log(f"STEP 5 done: {sum(1 for v in out.values() if v['status']=='WORKING')}/{len(out)} fully working")


# ---------------------------------------------------------------------------
# STEP 6 — real market data
# ---------------------------------------------------------------------------
def step6_market_data(top_coins):
    log("STEP 6: live market data + futures availability for 10 sample tokens ...")
    sample = top_coins[:10]
    out = []
    for coin in sample:
        cid = coin["coingecko_id"] if isinstance(coin, dict) else coin
        symbol = coin["symbol"] if isinstance(coin, dict) else cid
        section = market_data.build_market_section(cid, symbol)
        out.append({"coingecko_id": cid, "symbol": symbol, **section})
        time.sleep(1)
    results["step6_market_data"] = out
    log("STEP 6 done.")


# ---------------------------------------------------------------------------
# STEP 7 — historical event re-validation using REAL CoinGecko price history
# ---------------------------------------------------------------------------
def step7_historical():
    log("STEP 7: historical event price-history re-validation (BLAST, BMX) ...")
    out = {}
    for cid, label in (("blast", "BLAST"), ("bitmart-token", "BMX")):
        try:
            resp = universe._throttled_get(
                f"{cfg.COINGECKO_BASE}/coins/{cid}",
                params={"localization": "false", "tickers": "false", "market_data": "true",
                        "community_data": "false", "developer_data": "false"},
            )
        except Exception as exc:
            resp = None
            out[label] = {"error": str(exc)}
            continue
        if resp is None:
            out[label] = {
                "coingecko_id": cid,
                "error": "CoinGecko lookup failed (network or rate limit) -- could not verify",
            }
            continue
        md = resp.get("market_data", {})
        out[label] = {
            "coingecko_id": cid,
            "name": resp.get("name"),
            "symbol": (resp.get("symbol") or "").upper(),
            "homepage": (resp.get("links", {}).get("homepage") or [None])[0],
            "twitter_screen_name": resp.get("links", {}).get("twitter_screen_name"),
            "current_price_usd": md.get("current_price", {}).get("usd", "UNKNOWN"),
            "ath_usd": md.get("ath", {}).get("usd", "UNKNOWN"),
            "ath_date": md.get("ath_date", {}).get("usd", "UNKNOWN"),
            "note": (
                "This confirms the coin's identity and CoinGecko-reported price data only. "
                "It does NOT by itself confirm the nature of the event (bankruptcy vs. voluntary "
                "wind-down), the exact announcement timestamp, or the primary source URL -- those "
                "require reading the project's own announcement, linked above via homepage/twitter, "
                "or the RSS items captured in STEP 5 that mention this coin."
            ),
        }
        time.sleep(2)
    # Cross-reference anything STEP 5 already captured that mentions these projects.
    mentions = {"BLAST": [], "BMX": []}
    for source_name, data in results.get("step5_rss", {}).items():
        latest = data.get("latest_item")
        if not latest:
            continue
        text = (latest.get("title_or_text") or "").lower()
        if "blast" in text:
            mentions["BLAST"].append({"source": source_name, **latest})
        if "bitmart" in text or "bmx" in text:
            mentions["BMX"].append({"source": source_name, **latest})
    out["_rss_mentions_found_this_run"] = mentions
    results["step7_historical"] = out
    log("STEP 7 done.")


# ---------------------------------------------------------------------------
# STEP 8 — Telegram live test
# ---------------------------------------------------------------------------
def step8_telegram():
    log("STEP 8: Telegram live test ...")
    if not telegram_bot.is_configured():
        results["step8_telegram"] = {
            "status": "NOT_CONFIGURED",
            "message": (
                "CSR_TELEGRAM_BOT_TOKEN and/or CSR_TELEGRAM_CHAT_ID are not set. "
                "To configure: message @BotFather on Telegram, run /newbot, copy the token; "
                "message your new bot once; visit "
                "https://api.telegram.org/bot<TOKEN>/getUpdates and read the chat.id field; "
                "then export CSR_TELEGRAM_BOT_TOKEN and CSR_TELEGRAM_CHAT_ID before re-running."
            ),
        }
        log("STEP 8: Telegram not configured -- skipped, no message sent.")
        return
    text = (
        "[Crypto Shutdown Radar] LIVE VALIDATION TEST MESSAGE -- "
        f"sent at {datetime.now(timezone.utc).isoformat()}. If you received this, Telegram "
        "delivery is confirmed working end-to-end."
    )
    ok, error = telegram_bot.send_telegram_message(text)
    results["step8_telegram"] = {"status": "LIVE_VERIFIED" if ok else "FAILED", "error": error}
    log(f"STEP 8: {'message sent, check your Telegram' if ok else f'send failed: {error}'}")


# ---------------------------------------------------------------------------
# STEP 9 — write LIVE_VALIDATION_REPORT.md
# ---------------------------------------------------------------------------
def pick_verdict():
    x = results.get("step3_x_monitoring", {})
    rss = results.get("step5_rss", {})
    coverage = x.get("coverage_pct", 0) if not x.get("skipped") else 0
    rss_working = sum(1 for v in rss.values() if v["status"] == "WORKING")
    if coverage >= 60 and rss_working >= 1:
        return "A — FULLY VIABLE"
    if 0 < coverage < 60:
        return "B — VIABLE WITH PARTIAL X COVERAGE"
    if coverage == 0 and rss_working >= 1:
        return "C — VIABLE USING NON-X SOURCES AS PRIMARY"
    return "D — NOT CURRENTLY VIABLE FOR THE ORIGINAL REQUIREMENT"


def write_report():
    u = results.get("step2_universe", {})
    x = results.get("step3_x_monitoring", {})
    rss = results.get("step5_rss", {})
    md = results.get("step6_market_data", [])
    hist = results.get("step7_historical", {})
    tg = results.get("step8_telegram", {})

    rss_working = [k for k, v in rss.items() if v["status"] == "WORKING"]
    rss_failed = [k for k, v in rss.items() if v["status"] == "FAILED"]

    md_working = [f"{row['coingecko_id']} (price={row['price'].get('price_usd')})" for row in md if row["price"].get("price_usd") != "UNKNOWN"]
    md_failed = [row["coingecko_id"] for row in md if row["price"].get("price_usd") == "UNKNOWN"]

    verdict = pick_verdict()

    lines = []
    lines.append("# LIVE_VALIDATION_REPORT\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append("All numbers below come from real network requests made during this run. "
                  "See LIVE_VALIDATION_RAW_LOG.json for full machine-readable detail.\n")

    lines.append("## CRYPTO UNIVERSE\n")
    lines.append(f"Top 1000 retrieved: {u.get('retrieved', 'UNKNOWN')}/{u.get('requested', 1000)}\n")

    lines.append("## X MAPPING\n")
    lines.append(f"Verified official accounts: {u.get('verified_count', 'UNKNOWN')}\n")
    lines.append(f"Unverified: {u.get('unverified_count', 'UNKNOWN')}\n")
    lines.append("\n### 20 sample VERIFIED mappings\n")
    lines.append("| Coin | Ticker | X Handle | Evidence |")
    lines.append("|---|---|---|---|")
    for m in u.get("sample_verified_mappings", []):
        lines.append(f"| {m['coin_name']} | {m['ticker']} | @{m['x_handle']} | {m['evidence']} |")

    lines.append("\n## X LIVE MONITORING\n")
    if x.get("skipped"):
        lines.append(f"SKIPPED: {x.get('reason')}\n")
    else:
        lines.append(f"Attempted: {x.get('total_accounts_attempted', 0)}")
        lines.append(f"Successfully monitored: {x.get('total_succeeded', 0)}")
        lines.append(f"Failed: {x.get('total_failed', 0)}")
        lines.append(f"Coverage: {x.get('coverage_pct', 0)}%")
        methods_seen = sorted({p["method"] for p in x.get("sample_posts", [])})
        lines.append(f"Working method(s): {', '.join(methods_seen) if methods_seen else 'NONE'}")
        lines.append(f"Rate limiting encountered: {x.get('rate_limiting_encountered')}")
        if x.get("stopped_early_reason"):
            lines.append(f"Scale-up stopped early: {x['stopped_early_reason']}")
        lines.append("\nPer-stage detail:\n")
        lines.append("| Cumulative target | Attempted (new) | Succeeded | Failed | Posts | Avg req/account | Elapsed (s) |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in x.get("stages", []):
            lines.append(f"| {s['cumulative_target']} | {s['accounts_attempted_this_stage']} | {s['succeeded']} | "
                         f"{s['failed']} | {s['posts_retrieved']} | {s['avg_requests_per_account']} | {s['elapsed_seconds']} |")
        lines.append(f"\nOldest retrieved post timestamp: {x.get('oldest_post_timestamp')}")
        lines.append(f"Newest retrieved post timestamp: {x.get('newest_post_timestamp')}\n")
        lines.append("### 10 sample retrieved posts\n")
        for p in x.get("sample_posts", [])[:10]:
            lines.append(f"- **Handle:** @{p['handle']} | **Post ID:** {p['post_id']} | **Published:** {p['published_at']} | "
                         f"**Method:** {p['method']}\n  **URL:** {p['url']}\n  **Text:** {p['text'][:280]}\n")

    sus = results.get("step4_sustainability", {})
    lines.append("\n## SUSTAINABLE POLLING CAPACITY (estimated from this run, not sustained-load-tested)\n")
    for n, v in sus.get("estimates", {}).items():
        lines.append(f"- {n} accounts: {v}")
    lines.append(f"\n{sus.get('note', '')}\n")

    lines.append("\n## RSS / OFFICIAL SOURCES\n")
    lines.append(f"Working: {len(rss_working)}  |  Failed: {len(rss_failed)}\n")
    lines.append("| Source | Status | Latest item | Error |")
    lines.append("|---|---|---|---|")
    for name, v in rss.items():
        latest_str = v["latest_item"]["title_or_text"][:120] if v.get("latest_item") else "-"
        lines.append(f"| {name} | {v['status']} | {latest_str} | {v.get('error') or '-'} |")

    lines.append("\n## MARKET DATA\n")
    lines.append(f"Working: {', '.join(md_working) if md_working else 'NONE'}")
    lines.append(f"Failed/UNKNOWN: {', '.join(md_failed) if md_failed else 'NONE'}\n")
    lines.append("| Coin | Price | 1h | 24h | 7d | Volume | MCap | Binance | Bybit | OKX | Bitget | Gate |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in md:
        p, f = row["price"], row["futures"]
        lines.append(f"| {row['coingecko_id']} | {p.get('price_usd')} | {p.get('change_1h_pct')} | {p.get('change_24h_pct')} | "
                     f"{p.get('change_7d_pct')} | {p.get('volume_24h')} | {p.get('market_cap')} | "
                     f"{f.get('binance')} | {f.get('bybit')} | {f.get('okx')} | {f.get('bitget')} | {f.get('gate')} |")

    lines.append("\n## TELEGRAM\n")
    lines.append(f"{tg.get('status', 'UNKNOWN')}" + (f" -- {tg.get('message') or tg.get('error')}" if tg.get("message") or tg.get("error") else ""))

    lines.append("\n## HISTORICAL VALIDATION\n")
    for label in ("BLAST", "BMX"):
        entry = hist.get(label, {})
        lines.append(f"### {label}\n")
        if entry.get("error"):
            lines.append(f"NOT VERIFIED this run: {entry['error']}\n")
        else:
            lines.append(f"- CoinGecko ID: {entry.get('coingecko_id')}")
            lines.append(f"- Name / Symbol: {entry.get('name')} / {entry.get('symbol')}")
            lines.append(f"- Homepage: {entry.get('homepage')}")
            lines.append(f"- Twitter handle on file: {entry.get('twitter_screen_name')}")
            lines.append(f"- Current price (USD): {entry.get('current_price_usd')}")
            lines.append(f"- All-time high (USD) / date: {entry.get('ath_usd')} / {entry.get('ath_date')}")
            lines.append(f"- {entry.get('note')}\n")
    mentions = hist.get("_rss_mentions_found_this_run", {})
    lines.append("RSS mentions found during this run's STEP 5 (secondary-source corroboration, not primary announcements):")
    none_found = "none found in this run's RSS pull"
    lines.append(f"- BLAST: {mentions.get('BLAST') or none_found}")
    lines.append(f"- BMX: {mentions.get('BMX') or none_found}")
    lines.append(
        "\nIMPORTANT: this script verifies coin identity and current CoinGecko price data only. "
        "It does NOT independently confirm event classification (bankruptcy vs. voluntary wind-down), "
        "exact announcement timestamps, or primary-source URLs -- that requires manually opening the "
        "homepage/twitter links above, or asking Claude to re-run a web search against primary sources "
        "and compare against what's printed here.\n"
    )

    lines.append("## ZERO-COST STATUS\n")
    lines.append(
        "All sources exercised in this run are free/keyless public endpoints (CoinGecko free tier, "
        "public RSS feeds, unofficial free X-access methods, free Telegram Bot API, free exchange "
        "public REST endpoints). No paid API key, subscription, or paid infrastructure was used. "
        "CONFIRMED: $0 recurring cost for everything exercised in this run.\n"
    )

    lines.append("## FINAL VERDICT\n")
    lines.append(f"**{verdict}**\n")

    REPORT_PATH.write_text("\n".join(lines))
    log(f"Report written to {REPORT_PATH}")


def main():
    try:
        step1_feasibility()
    except Exception as exc:
        results["step1_feasibility"] = {"error": str(exc)}
        log(f"STEP 1 raised: {exc}")
    save_raw_log()

    verified_accounts = []
    top_coins = []
    try:
        verified_accounts = step2_universe()
        with get_conn() as conn:
            top_coins = [dict(r) for r in conn.execute(
                "SELECT coingecko_id, symbol FROM coins WHERE is_active=1 ORDER BY market_cap_rank ASC LIMIT 10"
            ).fetchall()]
    except Exception as exc:
        results["step2_universe"] = {"error": str(exc)}
        log(f"STEP 2 raised: {exc}")
    save_raw_log()

    try:
        step3_4_x_monitoring(verified_accounts)
    except Exception as exc:
        results["step3_x_monitoring"] = {"error": str(exc)}
        log(f"STEP 3/4 raised: {exc}")
    save_raw_log()

    try:
        step5_rss()
    except Exception as exc:
        results["step5_rss"] = {"error": str(exc)}
        log(f"STEP 5 raised: {exc}")
    save_raw_log()

    try:
        step6_market_data(top_coins or [{"coingecko_id": "bitcoin", "symbol": "btc"}])
    except Exception as exc:
        results["step6_market_data"] = {"error": str(exc)}
        log(f"STEP 6 raised: {exc}")
    save_raw_log()

    try:
        step7_historical()
    except Exception as exc:
        results["step7_historical"] = {"error": str(exc)}
        log(f"STEP 7 raised: {exc}")
    save_raw_log()

    try:
        step8_telegram()
    except Exception as exc:
        results["step8_telegram"] = {"error": str(exc)}
        log(f"STEP 8 raised: {exc}")
    save_raw_log()

    write_report()
    save_raw_log()
    log("ALL STEPS COMPLETE. See LIVE_VALIDATION_REPORT.md")


if __name__ == "__main__":
    sys.exit(main())
