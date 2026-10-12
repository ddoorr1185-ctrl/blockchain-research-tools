"""Discovers official, non-X monitoring sources for a coin: homepage, blog/
RSS feed, GitHub organization/repo activity feed, and subreddit RSS -- all
free, keyless, and with recorded provenance. Never guesses: every source
written to `official_sources` cites the CoinGecko metadata field (or the
homepage HTML itself) that it came from.

This is read-only discovery + verification. It does not touch
official_accounts (X-specific, pre-existing) or change any monitoring
logic in app/sources/ or app/pipeline.py.
"""
import json
import logging
import re
import time
from typing import Optional

import feedparser
import requests

from app.config import HTTP_TIMEOUT_SECONDS, USER_AGENT, COINGECKO_BASE
from app.db import get_conn, now_iso
from app.universe import get_json_no_retry

log = logging.getLogger("source_discovery")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
FEED_LINK_RE_SWAPPED = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+rel=["\']alternate["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def normalize_link_value(value) -> Optional[str]:
    """Canonical storage representation for a CoinGecko link field, which
    may be a bare scalar string, a list of strings (CoinGecko's schema is
    list-typed for homepage/announcement_url/official_forum_url/repos_url.*,
    scalar for twitter_screen_name/subreddit_url), None, or missing.

    THIS IS THE FIX for "Error binding parameter N: type 'list' is not
    supported": sqlite3 cannot bind a Python list directly, and the
    original bug (official_forum_url, which CoinGecko always returns as a
    list) passed the raw list straight into an INSERT parameter. Every
    multi-valued field is now normalized the same way, regardless of which
    field it is, so this class of bug cannot recur for a field we haven't
    specifically special-cased:

      scalar non-empty string  -> stored as a 1-element canonical JSON
                                   array, e.g. '["https://x.example"]'
      list of strings          -> non-empty entries kept, stored as a
                                   canonical JSON array
      None / empty list / ""   -> None (NULL column)

    Always returns a JSON array string or None -- never a bare list.
    """
    if value is None:
        items = []
    elif isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [v for v in value if isinstance(v, str) and v.strip()]
    else:
        items = []
    return json.dumps(items) if items else None


def parse_link_value(stored: Optional[str]) -> list:
    """Inverse of normalize_link_value. Tolerant of legacy rows written
    before this fix existed (a bare scalar URL string, not JSON) so an
    existing data/radar.db never needs a destructive migration: anything
    that isn't valid JSON, or that decodes to something other than a list,
    is treated as a single scalar URL instead of raising.
    """
    if not stored:
        return []
    try:
        parsed = json.loads(stored)
    except (ValueError, TypeError):
        return [stored]
    if isinstance(parsed, list):
        return [v for v in parsed if isinstance(v, str) and v.strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed]
    return []


def first_link(stored: Optional[str]) -> Optional[str]:
    items = parse_link_value(stored)
    return items[0] if items else None


def record_source(conn, coingecko_id, source_type, url, status, evidence, monitorable=False,
                   http_status=None, discovery_method=None, shutdown_relevance=None):
    """`evidence` is provenance (WHY this URL is considered official).
    `discovery_method` is HOW a monitorable feed was actually found (e.g.
    "declared in homepage <link rel=alternate>"), kept separate so neither
    gets overwritten by the other. `monitorable` must only be True for a
    changing publication/update stream we can poll -- see
    DIRECTLY_MONITORABLE_TYPES and is_directly_monitorable() below. A bare
    reachable homepage is reported (verification_status) but is NEVER
    monitorable=True on its own. `shutdown_relevance` (HIGH/MEDIUM/LOW) is
    set by app.shutdown_source_discovery for first_party_page/github/
    subreddit rows; left None for types it doesn't apply to.
    """
    conn.execute(
        """
        INSERT INTO official_sources (coingecko_id, source_type, url, verification_status, evidence, monitorable, http_status, discovery_method, shutdown_relevance, last_verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, source_type, url) DO UPDATE SET
            verification_status=excluded.verification_status,
            evidence=excluded.evidence,
            monitorable=excluded.monitorable,
            http_status=excluded.http_status,
            discovery_method=excluded.discovery_method,
            shutdown_relevance=excluded.shutdown_relevance,
            last_verified_at=excluded.last_verified_at
        """,
        (coingecko_id, source_type, url, status, evidence, 1 if monitorable else 0, http_status, discovery_method,
         shutdown_relevance, now_iso()),
    )


# Source types that, when monitorable=True, represent an actual changing
# publication/update stream -- NOT a static page. A "homepage" row is
# deliberately excluded here: being reachable is informational only (see
# Task 5 -- "homepage existing is NOT enough"). This is enforced at the
# point `monitorable` is set for each source type below, not just here;
# this set exists so other code (tests, reports) can assert the policy
# rather than re-deriving it ad hoc.
DIRECTLY_MONITORABLE_TYPES = {"rss_feed", "github", "subreddit", "announcement_page"}


def extract_feed_links(html: str, base_url: str) -> list:
    """Pure function (no network): finds <link rel="alternate" type="...+xml">
    feed URLs declared in an HTML <head>. Resolves relative URLs against
    base_url.
    """
    from urllib.parse import urljoin

    found = []
    for pattern in (FEED_LINK_RE, FEED_LINK_RE_SWAPPED):
        for href in pattern.findall(html):
            found.append(urljoin(base_url, href))
    # de-dup while preserving order
    seen = set()
    out = []
    for url in found:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def check_url_reachable(url: str, timeout=HTTP_TIMEOUT_SECONDS):
    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.text
    except requests.RequestException as exc:
        return None, str(exc)


def probe_feed(url: str) -> bool:
    status, body = check_url_reachable(url)
    if status != 200:
        return False
    parsed = feedparser.parse(body)
    return len(parsed.entries) > 0 or bool(parsed.feed.get("title"))


def github_org_or_repo_from_url(github_url: str) -> Optional[tuple]:
    """Parses a github.com URL into (owner, repo_or_none)."""
    m = re.match(r"https?://github\.com/([^/]+)/?([^/]+)?/?$", github_url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return (owner, repo) if repo else (owner, None)


def check_github_activity_feed(github_url: str):
    """GitHub serves free, unauthenticated Atom feeds for both a repo's
    releases/commits and a user/org's public activity. Returns
    (feed_url, ok) -- no API token, no auth, no scraping of rendered JS pages.
    """
    parsed = github_org_or_repo_from_url(github_url)
    if parsed is None:
        return None, False
    owner, repo = parsed
    feed_url = f"https://github.com/{owner}/{repo}/releases.atom" if repo else f"https://github.com/{owner}.atom"
    return feed_url, probe_feed(feed_url)


def check_subreddit_feed(subreddit_url: str):
    """Reddit serves a free, unauthenticated .rss feed for any public
    subreddit -- no API key, no OAuth app registration needed for this.
    """
    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", subreddit_url)
    if not m:
        return None, False
    feed_url = f"https://www.reddit.com/r/{m.group(1)}/.rss"
    return feed_url, probe_feed(feed_url)


def discover_blog_feed(homepage_url: str, homepage_html: Optional[str]):
    """Looks for a declared RSS/Atom feed on the homepage, falling back to
    the single most common convention path (WordPress/Ghost-style /feed)
    only if nothing was declared. Returns (feed_url_or_None, found_how).
    """
    if homepage_html:
        candidates = extract_feed_links(homepage_html, homepage_url)
        for candidate in candidates:
            if probe_feed(candidate):
                return candidate, "declared in homepage <link rel=alternate>"
    guess = homepage_url.rstrip("/") + "/feed"
    if probe_feed(guess):
        return guess, "common path probe (/feed) -- heuristic, not declared"
    return None, None


def get_cached_link_metadata(conn, coingecko_id: str):
    """Reads whatever extra CoinGecko link metadata (github/subreddit/
    announcement) is already cached for this coin. Makes NO network call.
    Returns the row, or None if this coin has never been enriched.
    """
    return conn.execute(
        "SELECT * FROM coin_link_metadata WHERE coingecko_id=?", (coingecko_id,)
    ).fetchone()


def record_link_metadata(conn, coingecko_id, github_url, subreddit_url, announcement_url, forum_url, status):
    """Persists CoinGecko link metadata for one coin. Every one of the four
    value arguments may be a bare scalar string, a list of strings, or
    None/empty -- normalize_link_value() is applied uniformly to all four,
    so it does not matter which of them CoinGecko's API returns as a list
    (today that's github_url/announcement_url/forum_url; subreddit_url is
    scalar today but would be handled safely even if that changed).
    """
    conn.execute(
        """
        INSERT INTO coin_link_metadata (coingecko_id, github_url, subreddit_url, announcement_url, forum_url, fetch_status, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id) DO UPDATE SET
            github_url=excluded.github_url,
            subreddit_url=excluded.subreddit_url,
            announcement_url=excluded.announcement_url,
            forum_url=excluded.forum_url,
            fetch_status=excluded.fetch_status,
            fetched_at=excluded.fetched_at
        """,
        (
            coingecko_id,
            normalize_link_value(github_url),
            normalize_link_value(subreddit_url),
            normalize_link_value(announcement_url),
            normalize_link_value(forum_url),
            status,
            now_iso(),
        ),
    )


def enrich_missing_link_metadata(
    coin_ids,
    request_budget: int = 30,
    time_budget_seconds: float = 90.0,
    deadline: Optional[float] = None,
) -> dict:
    """PHASE: metadata enrichment (A). The ONLY function in this module that
    calls CoinGecko. Strictly bounded by request count AND wall-clock time
    (and an optional shared `deadline`, a time.monotonic() value, for
    coordinating with an overall multi-phase budget).

    Coins already cached with fetch_status='OK' are skipped entirely --
    zero network calls for them. Every result (OK, RATE_LIMITED, or ERROR)
    is persisted immediately via record_link_metadata, so a later run
    resumes incrementally instead of re-attempting already-deferred coins
    from scratch forever... except RATE_LIMITED/ERROR coins ARE retried on
    a later call (their status isn't terminal), which is intentional: a
    429 now doesn't mean 429 forever.

    Uses get_json_no_retry: a 429 is recorded and moved past immediately,
    never slept out. This is the fix for the real failure mode (repeated
    60s Retry-After sleeps stalling an entire sample loop).
    """
    t0 = time.monotonic()
    with get_conn() as conn:
        already_ok = {
            r["coingecko_id"]
            for r in conn.execute("SELECT coingecko_id FROM coin_link_metadata WHERE fetch_status='OK'").fetchall()
        }
    to_try = [cid for cid in coin_ids if cid not in already_ok]

    attempted = 0
    ok_count = 0
    rate_limited = 0
    errors = 0
    stopped_reason = None
    malformed_coin_ids = []

    for cid in to_try:
        if attempted >= request_budget:
            stopped_reason = "request_budget"
            break
        elapsed = time.monotonic() - t0
        if elapsed > time_budget_seconds:
            stopped_reason = "time_budget"
            break
        if deadline is not None and time.monotonic() > deadline:
            stopped_reason = "global_deadline"
            break

        status, data = get_json_no_retry(
            f"{COINGECKO_BASE}/coins/{cid}",
            params={"localization": "false", "tickers": "false", "market_data": "false",
                    "community_data": "false", "developer_data": "false"},
        )
        attempted += 1

        try:
            with get_conn() as conn:
                if status == "OK":
                    links = (data or {}).get("links", {}) or {}
                    record_link_metadata(
                        conn, cid,
                        github_url=(links.get("repos_url") or {}).get("github"),
                        subreddit_url=links.get("subreddit_url"),
                        announcement_url=links.get("announcement_url"),
                        forum_url=links.get("official_forum_url"),
                        status="OK",
                    )
                    ok_count += 1
                else:
                    record_link_metadata(conn, cid, None, None, None, None, status)
                    if status == "RATE_LIMITED":
                        rate_limited += 1
                    else:
                        errors += 1
        except Exception as exc:
            # One malformed/unexpected coin must never abort enrichment for
            # the other 99+. Record it as an error for this coin only and
            # keep going -- see test_enrichment_one_malformed_coin_does_not_abort_batch.
            log.warning("enrichment failed for %s: %s", cid, exc)
            errors += 1
            malformed_coin_ids.append(cid)

    return {
        "coins_considered": len(coin_ids),
        "already_cached_skipped": len(coin_ids) - len(to_try),
        "attempted": attempted,
        "newly_ok": ok_count,
        "deferred_rate_limited": rate_limited,
        "errors": errors,
        "malformed_coin_ids": malformed_coin_ids,
        "stopped_reason": stopped_reason,
        "elapsed_seconds": time.monotonic() - t0,
    }


def discover_sources_from_cache(coingecko_id: str, cached_homepage: str = "") -> dict:
    """PHASE: source monitoring validation (B). Makes ZERO CoinGecko calls.
    Uses only what's already persisted: `cached_homepage` (from coins.website,
    passed in by the caller) and whatever coin_link_metadata already holds.
    A coin with no cached link metadata yet (never enriched, or enrichment
    deferred due to rate limiting) gets github/subreddit/announcement
    reported as MISSING_METADATA -- never silently skipped without saying
    so, and never fetched inline here regardless of how long that would take.

    STRICT "directly monitorable" definition (Task 5): a reachable
    homepage alone is reported (`homepage_reachable`) but never sets
    `directly_monitorable` -- only an actual changing publication/update
    stream does (blog RSS/Atom, GitHub activity feed, subreddit feed, or a
    reachable announcement page), matching DIRECTLY_MONITORABLE_TYPES.

    Never raises: any unexpected error for this one coin is caught,
    recorded, and returned as a result with status 'ERROR' rather than
    propagating and aborting the whole sample (see discover_for_sample).
    """
    result = {
        "coingecko_id": coingecko_id,
        "homepage_reachable": False,
        "blog_rss_found": None,
        "github_feed_found": None,
        "github_status": "MISSING_METADATA",
        "subreddit_feed_found": None,
        "subreddit_status": "MISSING_METADATA",
        "announcement_page_found": None,
        "announcement_status": "MISSING_METADATA",
        "directly_monitorable": False,
        "error": None,
    }

    try:
        with get_conn() as conn:
            meta = get_cached_link_metadata(conn, coingecko_id)

            homepage = cached_homepage or ""
            homepage_html = None
            if homepage:
                status, body = check_url_reachable(homepage)
                http_status = status if isinstance(status, int) else None
                if status == 200:
                    result["homepage_reachable"] = True
                    homepage_html = body
                    record_source(conn, coingecko_id, "homepage", homepage, "VERIFIED",
                                   "cached:coins.website (from prior CoinGecko enrichment)", monitorable=False,
                                   http_status=http_status, discovery_method="cached homepage URL")
                else:
                    record_source(conn, coingecko_id, "homepage", homepage, "UNVERIFIED",
                                   f"cached:coins.website (unreachable, HTTP/err={status})", monitorable=False,
                                   http_status=http_status, discovery_method="cached homepage URL")

                feed_url, how = discover_blog_feed(homepage, homepage_html)
                if feed_url:
                    result["blog_rss_found"] = feed_url
                    record_source(conn, coingecko_id, "rss_feed", feed_url, "VERIFIED",
                                   "derived from coins.website homepage", monitorable=True,
                                   http_status=200, discovery_method=how)
                    result["directly_monitorable"] = True

            if meta is None:
                pass  # never enriched at all (not yet attempted in any run)
            elif meta["fetch_status"] != "OK":
                result["github_status"] = f"DEFERRED ({meta['fetch_status']})"
                result["subreddit_status"] = f"DEFERRED ({meta['fetch_status']})"
                result["announcement_status"] = f"DEFERRED ({meta['fetch_status']})"
            else:
                github_url = first_link(meta["github_url"])
                if github_url:
                    feed_url, ok = check_github_activity_feed(github_url)
                    if feed_url:
                        record_source(
                            conn, coingecko_id, "github", feed_url, "VERIFIED" if ok else "UNVERIFIED",
                            f"cached_coingecko_metadata:links.repos_url.github ({github_url})", monitorable=ok,
                            http_status=200 if ok else None, discovery_method="GitHub releases/user .atom feed",
                        )
                        result["github_status"] = "OK" if ok else "CHECKED_NOT_WORKING"
                        if ok:
                            result["github_feed_found"] = feed_url
                            result["directly_monitorable"] = True
                else:
                    result["github_status"] = "NO_GITHUB_LINK"

                subreddit_url = first_link(meta["subreddit_url"])
                if subreddit_url:
                    feed_url, ok = check_subreddit_feed(subreddit_url)
                    if feed_url:
                        record_source(
                            conn, coingecko_id, "subreddit", feed_url, "VERIFIED" if ok else "UNVERIFIED",
                            f"cached_coingecko_metadata:links.subreddit_url ({subreddit_url})", monitorable=ok,
                            http_status=200 if ok else None, discovery_method="Reddit .rss feed",
                        )
                        result["subreddit_status"] = "OK" if ok else "CHECKED_NOT_WORKING"
                        if ok:
                            result["subreddit_feed_found"] = feed_url
                            result["directly_monitorable"] = True
                else:
                    result["subreddit_status"] = "NO_SUBREDDIT_LINK"

                announcement_url = first_link(meta["announcement_url"])
                if announcement_url:
                    status, _ = check_url_reachable(announcement_url)
                    record_source(
                        conn, coingecko_id, "announcement_page", announcement_url,
                        "VERIFIED" if status == 200 else "UNVERIFIED",
                        "cached_coingecko_metadata:links.announcement_url", monitorable=(status == 200),
                        http_status=status if isinstance(status, int) else None,
                        discovery_method="cached CoinGecko announcement_url",
                    )
                    result["announcement_status"] = "OK" if status == 200 else "CHECKED_NOT_WORKING"
                    if status == 200:
                        result["announcement_page_found"] = announcement_url
                        result["directly_monitorable"] = True
                else:
                    result["announcement_status"] = "NO_ANNOUNCEMENT_LINK"
    except Exception as exc:
        log.warning("discover_sources_from_cache failed for %s: %s", coingecko_id, exc)
        result["error"] = str(exc)

    return result


def discover_for_sample(
    coin_rows,
    enrichment_request_budget: int = 30,
    enrichment_time_budget_seconds: float = 90.0,
    discovery_time_budget_seconds: float = 180.0,
    deadline: Optional[float] = None,
    progress_every: int = 10,
    progress_cb=None,
) -> dict:
    """Orchestrates the two separated phases for a coin sample:
      A) enrich_missing_link_metadata -- bounded CoinGecko enrichment
      B) discover_sources_from_cache -- cache-only, zero CoinGecko calls

    `deadline` (a time.monotonic() value) is an optional hard ceiling
    shared across a larger multi-phase run; both sub-phases stop the
    instant it passes, regardless of their own budgets.
    `progress_cb(processed, total, found, deferred, elapsed, remaining)`
    is called every `progress_every` coins during phase B, if provided.
    """
    t0 = time.monotonic()
    coin_ids = [row["coingecko_id"] if isinstance(row, dict) else row for row in coin_rows]

    enrichment_summary = enrich_missing_link_metadata(
        coin_ids,
        request_budget=enrichment_request_budget,
        time_budget_seconds=enrichment_time_budget_seconds,
        deadline=deadline,
    )

    per_coin = []
    budget_exceeded_after = None
    deferred_count = 0
    malformed_coin_ids = []
    for i, row in enumerate(coin_rows):
        now = time.monotonic()
        if (now - t0) > (enrichment_summary["elapsed_seconds"] + discovery_time_budget_seconds):
            budget_exceeded_after = len(per_coin)
            break
        if deadline is not None and now > deadline:
            budget_exceeded_after = len(per_coin)
            break

        cid = row["coingecko_id"] if isinstance(row, dict) else row
        homepage = row.get("website", "") if isinstance(row, dict) else ""
        try:
            coin_result = discover_sources_from_cache(cid, cached_homepage=homepage)
        except Exception as exc:
            # Belt-and-suspenders: discover_sources_from_cache already
            # catches its own errors internally, but this loop must never
            # abort the other 99 coins even if some future code path (or a
            # test's mock) raises past that. See
            # test_one_malformed_coin_does_not_abort_the_100_coin_run.
            log.warning("discover_for_sample: coin %s raised, skipping: %s", cid, exc)
            coin_result = {
                "coingecko_id": cid, "homepage_reachable": False, "blog_rss_found": None,
                "github_feed_found": None, "github_status": "ERROR", "subreddit_feed_found": None,
                "subreddit_status": "ERROR", "announcement_page_found": None, "announcement_status": "ERROR",
                "directly_monitorable": False, "error": str(exc),
            }
            malformed_coin_ids.append(cid)
        per_coin.append(coin_result)
        if coin_result.get("error") and cid not in malformed_coin_ids:
            malformed_coin_ids.append(cid)
        if "DEFERRED" in coin_result["github_status"]:
            deferred_count += 1

        if progress_cb and (i + 1) % progress_every == 0:
            found_so_far = sum(1 for r in per_coin if r["directly_monitorable"])
            remaining_budget = None
            if deadline is not None:
                remaining_budget = max(0.0, deadline - time.monotonic())
            progress_cb(i + 1, len(coin_rows), found_so_far, deferred_count, time.monotonic() - t0, remaining_budget)

        time.sleep(0.2)  # politeness delay for the non-CoinGecko requests

    total = len(coin_rows)
    completed = len(per_coin)
    directly_monitorable_count = sum(1 for r in per_coin if r["directly_monitorable"])
    homepage_only_or_nothing = completed - directly_monitorable_count
    return {
        "requested": total,
        "completed": completed,
        "stopped_early_due_to_time_budget": budget_exceeded_after is not None,
        "enrichment": enrichment_summary,
        "malformed_coin_ids": malformed_coin_ids,
        "homepage_coverage": sum(1 for r in per_coin if r["homepage_reachable"]),
        "blog_rss_coverage": sum(1 for r in per_coin if r["blog_rss_found"]),
        "github_coverage": sum(1 for r in per_coin if r["github_feed_found"]),
        "subreddit_coverage": sum(1 for r in per_coin if r["subreddit_feed_found"]),
        "announcement_coverage": sum(1 for r in per_coin if r["announcement_page_found"]),
        "deferred_due_to_missing_metadata": deferred_count,
        # STRICT definition (Task 5): a bare reachable homepage never counts
        # here. Only blog RSS / GitHub activity / subreddit feed /
        # announcement page -- an actual pollable update stream -- does.
        "at_least_one_official_source": directly_monitorable_count,
        "only_x_known_or_nothing": homepage_only_or_nothing,
        "elapsed_seconds": time.monotonic() - t0,
        "per_coin": per_coin,
    }
