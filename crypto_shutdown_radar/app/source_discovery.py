"""Discovers official, non-X monitoring sources for a coin: homepage, blog/
RSS feed, GitHub organization/repo activity feed, and subreddit RSS -- all
free, keyless, and with recorded provenance. Never guesses: every source
written to `official_sources` cites the CoinGecko metadata field (or the
homepage HTML itself) that it came from.

This is read-only discovery + verification. It does not touch
official_accounts (X-specific, pre-existing) or change any monitoring
logic in app/sources/ or app/pipeline.py.
"""
import logging
import re
import time
from typing import Optional

import feedparser
import requests

from app.config import HTTP_TIMEOUT_SECONDS, USER_AGENT
from app.db import get_conn, now_iso
from app.universe import fetch_coin_detail

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


def record_source(conn, coingecko_id, source_type, url, status, evidence, monitorable=False):
    conn.execute(
        """
        INSERT INTO official_sources (coingecko_id, source_type, url, verification_status, evidence, monitorable, last_verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, source_type, url) DO UPDATE SET
            verification_status=excluded.verification_status,
            evidence=excluded.evidence,
            monitorable=excluded.monitorable,
            last_verified_at=excluded.last_verified_at
        """,
        (coingecko_id, source_type, url, status, evidence, 1 if monitorable else 0, now_iso()),
    )


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


def discover_official_sources_for_coin(coingecko_id: str, cached_homepage: str = "") -> dict:
    """Runs the full discovery pipeline for one coin and persists findings.
    Makes network requests: 1 CoinGecko detail call (rate-limited) + up to
    ~4 more to independent, non-CoinGecko hosts (homepage, feed candidate,
    github, subreddit) which are NOT subject to CoinGecko's rate limit.

    Returns a summary dict (also written to official_sources) for the
    calling report to aggregate.
    """
    result = {
        "coingecko_id": coingecko_id,
        "homepage_reachable": False,
        "blog_rss_found": None,
        "github_feed_found": None,
        "subreddit_feed_found": None,
        "detail_fetch_error": False,
    }

    detail = fetch_coin_detail(coingecko_id)
    if detail is None:
        result["detail_fetch_error"] = True
        return result
    links = detail.get("links", {}) or {}

    homepage = cached_homepage or (links.get("homepage") or [""])[0]
    homepage_html = None
    with get_conn() as conn:
        if homepage:
            status, body = check_url_reachable(homepage)
            if status == 200:
                result["homepage_reachable"] = True
                homepage_html = body
                record_source(conn, coingecko_id, "homepage", homepage, "VERIFIED",
                               "coingecko_metadata:links.homepage", monitorable=False)
            elif homepage:
                record_source(conn, coingecko_id, "homepage", homepage, "UNVERIFIED",
                               f"coingecko_metadata:links.homepage (unreachable, HTTP/err={status})", monitorable=False)

        if homepage:
            feed_url, how = discover_blog_feed(homepage, homepage_html)
            if feed_url:
                result["blog_rss_found"] = feed_url
                record_source(conn, coingecko_id, "rss_feed", feed_url, "VERIFIED", how, monitorable=True)

        github_urls = (links.get("repos_url") or {}).get("github") or []
        if github_urls:
            feed_url, ok = check_github_activity_feed(github_urls[0])
            if feed_url:
                record_source(
                    conn, coingecko_id, "github", feed_url,
                    "VERIFIED" if ok else "UNVERIFIED",
                    f"coingecko_metadata:links.repos_url.github ({github_urls[0]})",
                    monitorable=ok,
                )
                if ok:
                    result["github_feed_found"] = feed_url

        subreddit_url = links.get("subreddit_url") or ""
        if subreddit_url:
            feed_url, ok = check_subreddit_feed(subreddit_url)
            if feed_url:
                record_source(
                    conn, coingecko_id, "subreddit", feed_url,
                    "VERIFIED" if ok else "UNVERIFIED",
                    f"coingecko_metadata:links.subreddit_url ({subreddit_url})",
                    monitorable=ok,
                )
                if ok:
                    result["subreddit_feed_found"] = feed_url

        announcement_url = (links.get("announcement_url") or [""])[0]
        if announcement_url:
            status, _ = check_url_reachable(announcement_url)
            record_source(
                conn, coingecko_id, "announcement_page", announcement_url,
                "VERIFIED" if status == 200 else "UNVERIFIED",
                "coingecko_metadata:links.announcement_url",
                monitorable=False,
            )
            result["announcement_page_found"] = announcement_url if status == 200 else None

    return result


def discover_for_sample(coin_rows, time_budget_seconds: float = 480.0) -> dict:
    """Runs discovery across a sample of coins (dicts with at least
    coingecko_id/symbol/website) under a wall-clock time budget. Stops
    early and reports exactly how many were actually completed if the
    budget runs out -- never silently truncates without saying so.
    """
    t0 = time.monotonic()
    per_coin = []
    budget_exceeded_after = None
    for row in coin_rows:
        if time.monotonic() - t0 > time_budget_seconds:
            budget_exceeded_after = len(per_coin)
            break
        cid = row["coingecko_id"] if isinstance(row, dict) else row
        homepage = row.get("website", "") if isinstance(row, dict) else ""
        per_coin.append(discover_official_sources_for_coin(cid, cached_homepage=homepage))
        time.sleep(0.3)  # politeness delay for the non-CoinGecko requests

    total = len(coin_rows)
    completed = len(per_coin)
    at_least_one_source = sum(
        1 for r in per_coin
        if r["homepage_reachable"] or r["blog_rss_found"] or r["github_feed_found"] or r["subreddit_feed_found"]
    )
    only_x_known = sum(
        1 for r in per_coin
        if not (r["homepage_reachable"] or r["blog_rss_found"] or r["github_feed_found"] or r["subreddit_feed_found"])
    )
    return {
        "requested": total,
        "completed": completed,
        "stopped_early_due_to_time_budget": budget_exceeded_after is not None,
        "homepage_coverage": sum(1 for r in per_coin if r["homepage_reachable"]),
        "blog_rss_coverage": sum(1 for r in per_coin if r["blog_rss_found"]),
        "github_coverage": sum(1 for r in per_coin if r["github_feed_found"]),
        "subreddit_coverage": sum(1 for r in per_coin if r["subreddit_feed_found"]),
        "at_least_one_official_source": at_least_one_source,
        "only_x_known_or_nothing": only_x_known,
        "elapsed_seconds": time.monotonic() - t0,
        "per_coin": per_coin,
    }
