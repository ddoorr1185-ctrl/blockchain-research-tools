"""Measures SHUTDOWN-EVENT SOURCE COVERAGE: for a project, whether we have
at least one free, first-party, machine-monitorable source that is
*plausibly* where it would publish a shutdown/wind-down/bankruptcy/
closure/termination announcement -- as opposed to the broader (and looser)
"directly monitorable" metric in app.source_discovery, which happily
counts GitHub release activity or a generic subreddit even though neither
is where a company announces insolvency.

Design, matching the explicit constraints given for this pass:
- A small, bounded candidate path list (not a brute-force crawl).
- Homepage <a> link discovery by href path OR anchor text keyword.
- First-party domain validation (same registered domain as the homepage,
  or an explicitly allowed subdomain of it).
- A lightweight "is this actually a changing listing, not a static page"
  heuristic (repeated dated entries / repeated slug-shaped links) --
  NOT a general-purpose scraper. We are validating coverage, not building
  the eventual poller yet.
- Every finding gets an explicit shutdown_relevance (HIGH/MEDIUM/LOW),
  independent of whether CoinGecko metadata is involved at all.
- GitHub and subreddit are LOW relevance by default -- Task 8's whole
  point is that a technical/community channel must not inflate the
  shutdown-relevant metric just because it happens to be monitorable.
"""
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from app.config import HTTP_TIMEOUT_SECONDS
from app.db import get_conn, now_iso
from app.source_discovery import check_url_reachable, record_source

log = logging.getLogger("shutdown_source_discovery")

# Small, bounded set (Task 3): do not brute-force a huge path list.
CANDIDATE_PATHS = [
    "/blog", "/news", "/announcements", "/announcement", "/updates",
    "/press", "/press-releases", "/media", "/company/news", "/community",
    "/notices", "/support/announcements",
]

# Anchor-text keywords that suggest a link leads to a news/blog/announcement
# surface even when the URL path itself doesn't match CANDIDATE_PATHS.
ANCHOR_TEXT_KEYWORDS = ["news", "blog", "announcement", "press", "updates", "notices"]

# How plausible each matched path/keyword is as a shutdown-announcement
# surface. HIGH = operators very commonly post exactly this kind of notice
# there; MEDIUM = a general update channel that sometimes carries it;
# LOW = community/technical, not an operator mouthpiece.
HIGH_RELEVANCE_HINTS = ["announcement", "announcements", "press-release", "press-releases", "notices", "support/announcements"]
MEDIUM_RELEVANCE_HINTS = ["news", "blog", "updates", "press", "media", "company/news"]
LOW_RELEVANCE_HINTS = ["community"]

ANCHOR_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
DATE_LIKE_RE = re.compile(
    r"\b(19|20)\d{2}-\d{2}-\d{2}\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+(19|20)\d{2}\b",
    re.IGNORECASE,
)

_session = None  # lazily bound to app.source_discovery's session to avoid a second pool


def _get_session():
    global _session
    if _session is None:
        from app.source_discovery import _session as shared_session
        _session = shared_session
    return _session


def relevance_for_path_or_text(path_or_text: str) -> str:
    """Pure function: classify HIGH/MEDIUM/LOW from a URL path or anchor
    text. Defaults to LOW for anything not explicitly recognized -- we
    never assume HIGH relevance without a specific match.
    """
    s = (path_or_text or "").lower()
    for hint in HIGH_RELEVANCE_HINTS:
        if hint in s:
            return "HIGH"
    for hint in MEDIUM_RELEVANCE_HINTS:
        if hint in s:
            return "MEDIUM"
    return "LOW"


def is_first_party(homepage_url: str, candidate_url: str) -> bool:
    """True if `candidate_url` is on the same registered domain as
    `homepage_url` (allowing subdomains), e.g. homepage example.com and
    candidate blog.example.com both pass; a wholly different domain does
    not. This is a simple suffix check, not full public-suffix-list
    parsing -- adequate for "same company's own site" at the scale of a
    coin's single homepage domain, and documented as a heuristic.
    """
    try:
        home_host = urlparse(homepage_url).netloc.lower().split(":")[0]
        cand_host = urlparse(candidate_url).netloc.lower().split(":")[0]
    except ValueError:
        return False
    if not home_host or not cand_host:
        return False
    home_host = home_host[4:] if home_host.startswith("www.") else home_host
    cand_host = cand_host[4:] if cand_host.startswith("www.") else cand_host
    return cand_host == home_host or cand_host.endswith("." + home_host)


def extract_candidate_links(html: str, base_url: str) -> list:
    """Pure function (no network): scans homepage HTML for <a> tags whose
    href matches one of CANDIDATE_PATHS, or whose anchor text contains one
    of ANCHOR_TEXT_KEYWORDS. Returns a de-duplicated list of
    {url, matched_via, relevance} dicts. Resolves relative hrefs against
    base_url. Does not fetch anything.
    """
    out = []
    seen = set()
    for href, inner_html in ANCHOR_RE.findall(html):
        text = TAG_STRIP_RE.sub(" ", inner_html).strip().lower()
        resolved = urljoin(base_url, href)
        path = urlparse(resolved).path.lower().rstrip("/")

        matched_via = None
        for candidate_path in CANDIDATE_PATHS:
            if path == candidate_path or path.endswith(candidate_path):
                matched_via = f"href path matches {candidate_path}"
                relevance_source = candidate_path
                break
        else:
            for kw in ANCHOR_TEXT_KEYWORDS:
                if kw in text:
                    matched_via = f"anchor text contains '{kw}'"
                    relevance_source = kw
                    break
            else:
                continue

        if resolved in seen:
            continue
        seen.add(resolved)
        out.append({
            "url": resolved,
            "matched_via": matched_via,
            "relevance": relevance_for_path_or_text(relevance_source),
        })
    return out


def assess_monitorability(html: str) -> dict:
    """Pure function: a lightweight heuristic for whether a page looks
    like a changing listing (multiple distinct dated/slug-shaped entries)
    rather than a static one-off page. NOT a general scraper -- just
    enough evidence to say "polling this for new entries is plausible".

    Returns {"monitorable": bool, "entry_count_evidence": int, "strategy": str|None, "reason": str}.
    """
    date_hits = len(DATE_LIKE_RE.findall(html))
    article_tag_hits = len(re.findall(r"<article[\s>]", html, re.IGNORECASE))
    # Distinct internal links that look like dated/slug permalinks, e.g.
    # /blog/2026/some-post-title or /news/some-post-title.
    slug_links = set(re.findall(r'href=["\']([^"\']*/(?:[0-9]{4}/)?[a-z0-9][a-z0-9-]{4,}/?)["\']', html, re.IGNORECASE))

    if article_tag_hits >= 2:
        return {"monitorable": True, "entry_count_evidence": article_tag_hits,
                "strategy": "repeated <article> tags -- poll for new <article> blocks",
                "reason": f"{article_tag_hits} <article> tags found"}
    if len(slug_links) >= 3:
        return {"monitorable": True, "entry_count_evidence": len(slug_links),
                "strategy": "repeated slug-shaped permalinks -- poll for new distinct URLs",
                "reason": f"{len(slug_links)} distinct slug-shaped links found"}
    if date_hits >= 3:
        return {"monitorable": True, "entry_count_evidence": date_hits,
                "strategy": "repeated dated entries -- poll for new dates/titles",
                "reason": f"{date_hits} date-like strings found"}
    return {"monitorable": False, "entry_count_evidence": max(date_hits, article_tag_hits, len(slug_links)),
            "strategy": None, "reason": "no repeated article/date/permalink pattern found (looks like a single static page)"}


def get_cached_shutdown_pages(conn, coingecko_id: str):
    """Read-only: whatever first_party_page rows are already persisted for
    this coin from a prior run. Makes no network call.
    """
    return conn.execute(
        "SELECT * FROM official_sources WHERE coingecko_id=? AND source_type='first_party_page'",
        (coingecko_id,),
    ).fetchall()


def discover_shutdown_sources_for_project(coingecko_id: str, homepage_url: str, homepage_html: Optional[str],
                                           max_candidates: int = 5, skip_if_cached: bool = True) -> dict:
    """For ONE project: discovers and records first-party news/blog/
    announcement candidates from its homepage, probes up to
    `max_candidates` of them for reachability + monitorability, and
    records every one found (reachable or not) in official_sources with
    its shutdown_relevance.

    Never raises: any failure for this one project is caught and returned
    as an error, never propagated (Task 7 -- one broken website must not
    abort the batch).

    `skip_if_cached`: if official_sources already holds first_party_page
    rows for this coin from a prior run, reuse them instead of re-probing
    (resumability without needing a separate cache table).
    """
    result = {
        "coingecko_id": coingecko_id,
        "candidates_found": 0,
        "candidates_probed": 0,
        "pages": [],  # [{url, relevance, monitorable, http_status, strategy, reason}]
        "from_cache": False,
        "error": None,
    }
    try:
        if skip_if_cached:
            with get_conn() as conn:
                cached_rows = get_cached_shutdown_pages(conn, coingecko_id)
            if cached_rows:
                result["from_cache"] = True
                result["candidates_found"] = len(cached_rows)
                result["candidates_probed"] = len(cached_rows)
                result["pages"] = [
                    {"url": r["url"], "relevance": r["shutdown_relevance"], "monitorable": bool(r["monitorable"]),
                     "http_status": r["http_status"], "strategy": r["discovery_method"], "reason": None}
                    for r in cached_rows
                ]
                return result

        if not homepage_url or not homepage_html:
            return result

        candidates = extract_candidate_links(homepage_html, homepage_url)
        candidates = [c for c in candidates if is_first_party(homepage_url, c["url"])]
        result["candidates_found"] = len(candidates)

        # Probe the most relevant candidates first (HIGH before MEDIUM before LOW),
        # bounded by max_candidates so one project can't spend unbounded requests.
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        candidates.sort(key=lambda c: rank.get(c["relevance"], 3))

        with get_conn() as conn:
            for cand in candidates[:max_candidates]:
                status, body = check_url_reachable(cand["url"])
                http_status = status if isinstance(status, int) else None
                reachable = status == 200
                assessment = assess_monitorability(body) if reachable and body else \
                    {"monitorable": False, "entry_count_evidence": 0, "strategy": None,
                     "reason": f"unreachable (HTTP/err={status})"}
                record_source(
                    conn, coingecko_id, "first_party_page", cand["url"],
                    "VERIFIED" if reachable else "UNVERIFIED",
                    f"discovered via homepage: {cand['matched_via']}",
                    monitorable=assessment["monitorable"],
                    http_status=http_status,
                    discovery_method=assessment["strategy"] or cand["matched_via"],
                    shutdown_relevance=cand["relevance"],
                )
                result["pages"].append({
                    "url": cand["url"], "relevance": cand["relevance"], "monitorable": assessment["monitorable"],
                    "http_status": http_status, "strategy": assessment["strategy"], "reason": assessment["reason"],
                })
                result["candidates_probed"] += 1
    except Exception as exc:
        log.warning("shutdown source discovery failed for %s: %s", coingecko_id, exc)
        result["error"] = str(exc)

    return result


def classify_project_coverage(shutdown_pages: list, github_feed_found: bool, subreddit_feed_found: bool) -> dict:
    """Task 5's A/B/C/D classification, computed from already-discovered
    data (no network call). `shutdown_pages` is the `pages` list from
    discover_shutdown_sources_for_project (or an equivalent read from
    official_sources). GitHub/subreddit are LOW relevance by default and
    can only ever contribute to C, never A/B, per Task 8.
    """
    monitorable_high = any(p["monitorable"] and p["relevance"] == "HIGH" for p in shutdown_pages)
    monitorable_medium = any(p["monitorable"] and p["relevance"] == "MEDIUM" for p in shutdown_pages)
    monitorable_low_first_party = any(p["monitorable"] and p["relevance"] == "LOW" for p in shutdown_pages)

    if monitorable_high:
        classification = "A"
    elif monitorable_medium:
        classification = "B"
    elif monitorable_low_first_party or github_feed_found or subreddit_feed_found:
        classification = "C"
    else:
        classification = "D"

    return {
        "classification": classification,
        "shutdown_relevant_first_party_coverage": classification in ("A", "B"),
        "technical_monitorable_coverage": classification in ("A", "B", "C"),
    }
