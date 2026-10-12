"""Audits the RAW shutdown-source discovery results (app.shutdown_source_discovery)
against a much stricter rule: a source counts toward STRICT A/B only if it
is demonstrably a PUBLICATION STREAM (>=2 distinct historical entries),
not merely a reachable page -- an individual article, a legal/signup/
anchor page, or a bare homepage must never qualify by itself.

This module never overwrites raw discovery data. It reads official_sources
(written by app.shutdown_source_discovery) as input and writes its
findings to the separate shutdown_source_audit / project_audit_classification
tables (see app.db), so RAW_DISCOVERY_CLASSIFICATION and
AUDITED_CLASSIFICATION can both be inspected side by side.
"""
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from app.db import get_conn, now_iso
from app.shutdown_source_discovery import (
    check_url_reachable, relevance_for_path_or_text, extract_candidate_links, is_first_party,
    ANCHOR_RE, TAG_STRIP_RE, DATE_LIKE_RE,
)

log = logging.getLogger("shutdown_source_audit")

# Segments that disqualify a URL from ever being a qualifying stream,
# wherever they appear in the path -- these are never legitimately a
# wrapper directory for a real announcements listing (Task 1/7).
ALWAYS_DISQUALIFYING_SEGMENTS = {
    "legal", "terms", "term", "api-terms", "tos", "privacy", "cookie", "cookies",
    "signup", "sign-up", "register", "join", "newsletter", "subscribe",
    "careers", "jobs", "community",
    # Task 2 (GOLD audit) hard blocks: docs/API surfaces can NEVER be a
    # shutdown-announcement stream, wherever they sit in the path --
    # e.g. bittensor.com/docs/query was wrongly counted as a listing
    # because only the LEAF segment ("query") was checked against the
    # old docs/documentation entry below, never the "docs" segment itself.
    "docs", "documentation", "api", "apis", "api-reference", "reference", "query", "graphql", "sdk",
}
# Segments that only disqualify when they are the LEAF (final) path
# segment -- e.g. a nested "/resources/docs" page is a docs page, but
# "/docs/changelog" style directories are not blanket-banned by a prefix.
LEAF_ONLY_DISQUALIFYING_SEGMENTS = {
    "faq", "press-kit", "media-kit", "brand", "branding", "about",
}

# Extra, bounded path variants for the deeper D-project pass (Task 5) --
# beyond app.shutdown_source_discovery.CANDIDATE_PATHS. Still a small,
# fixed set, not a brute-force crawl.
DEEPER_CANDIDATE_PATHS = ["/newsroom", "/insights", "/resources/news", "/about/news", "/company/press", "/media-center"]
FIRST_PARTY_SUBDOMAIN_PREFIXES = ["blog", "news", "foundation", "support"]

# Well-known, publicly documented operator relationships. This is a small,
# explicit, curated list for cases where the project's branding does not
# textually match its operating entity's domain -- NOT a general entity
# database. Anything not listed falls through to the generic heuristic
# below (PLAUSIBLE if the homepage domain textually relates to the
# project's name/symbol, else UNCLEAR -- never guessed as VERIFIED).
ENTITY_RELATIONSHIP_OVERRIDES = {
    "xrp": ("Ripple", "VERIFIED"),
    "ripple": ("Ripple", "VERIFIED"),
    "okb": ("OKX", "VERIFIED"),
    "leo-token": ("Bitfinex", "VERIFIED"),
    "unus-sed-leo": ("Bitfinex", "VERIFIED"),
    "gas": ("Neo", "VERIFIED"),
    "neo-gas": ("Neo", "VERIFIED"),
}


def is_disqualified_url(url: str) -> Optional[str]:
    """Pure function. Returns a reason string if `url` can never qualify
    as a stream regardless of its content, or None if it might. Checked
    BEFORE any content-based heuristic (Task 1/7).
    """
    parsed = urlparse(url)
    if parsed.fragment:
        return f"anchor URL (#{parsed.fragment}) -- not a distinct page"
    path = parsed.path.lower().rstrip("/")
    if path in ("", "/"):
        return "homepage itself -- not a publication stream"
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg in ALWAYS_DISQUALIFYING_SEGMENTS:
            return f"path contains disqualifying segment '{seg}'"
    if segments and segments[-1] in LEAF_ONLY_DISQUALIFYING_SEGMENTS:
        return f"leaf segment '{segments[-1]}' is a disqualified page type"
    return None


def extract_listing_entries(html: str, base_url: str, min_entries: int = 2) -> list:
    """Pure function (no network). Looks for anchors under the SAME path
    prefix as `base_url` that point to a deeper, distinct page (a
    plausible "entry" on a listing page), with title-like anchor text.
    Returns a de-duplicated list of {url, title, date} (date is a
    best-effort nearby match, may be None), ordered as found.

    This is intentionally a lightweight heuristic (Task 1/4's "we are
    validating coverage, not building the eventual poller"), not a real
    scraper -- it establishes plausibility of a changing listing.
    """
    base_path = urlparse(base_url).path.rstrip("/")
    entries = []
    seen = set()
    for match in ANCHOR_RE.finditer(html):
        href, inner_html = match.group(1), match.group(2)
        title = TAG_STRIP_RE.sub(" ", inner_html).strip()
        if len(title) < 8:
            continue
        resolved = urljoin(base_url, href)
        resolved_path = urlparse(resolved).path.rstrip("/")
        if not resolved_path.startswith(base_path + "/") or resolved_path == base_path:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        window = html[max(0, match.start() - 200):match.start()]
        date_match = DATE_LIKE_RE.search(window)
        entries.append({"url": resolved, "title": title, "date": date_match.group(0) if date_match else None})
    return entries


def classify_page(url: str, html: Optional[str]) -> dict:
    """Pure function. Combines disqualification + entry-listing detection
    into one verdict for a single URL, WITHOUT trying to resolve a parent
    (that's find_qualifying_stream's job, since it needs to fetch the
    parent too). Returns {"page_type": "DISQUALIFIED"|"LISTING"|"ARTICLE_OR_STATIC",
    "entries": [...], "reason": str}.
    """
    disq_reason = is_disqualified_url(url)
    if disq_reason:
        return {"page_type": "DISQUALIFIED", "entries": [], "reason": disq_reason}
    if not html:
        return {"page_type": "ARTICLE_OR_STATIC", "entries": [], "reason": "no content to inspect"}
    entries = extract_listing_entries(html, url)
    if len(entries) >= 2:
        return {"page_type": "LISTING", "entries": entries, "reason": f"{len(entries)} distinct entries found"}
    return {"page_type": "ARTICLE_OR_STATIC", "entries": entries,
            "reason": "fewer than 2 distinct entries on this page -- looks like a single article or static page"}


def parent_path_candidate(url: str) -> Optional[str]:
    """Pure function: given an article-looking URL, returns the parent
    directory URL one level up (Task 7's chain.link/blog/introducing-ccip-2-0
    -> chain.link/blog), or None if there's nowhere to go up to.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    segments = path.split("/")
    if len(segments) <= 2:  # e.g. "" , "blog" -- already at the top, no parent to try
        return None
    parent_path = "/".join(segments[:-1])
    return f"{parsed.scheme}://{parsed.netloc}{parent_path}"


def entity_relationship(coingecko_id: str, symbol: str, name: str, homepage_url: str) -> tuple:
    """Task 6. Returns (entity_name_or_None, status) where status is
    VERIFIED/PLAUSIBLE/UNCLEAR. VERIFIED only from the small curated
    override list (publicly well-documented relationships); everything
    else is at best PLAUSIBLE (domain textually relates to the project)
    or UNCLEAR -- never guessed as VERIFIED.
    """
    for key in ((coingecko_id or "").lower(), (symbol or "").lower()):
        if key in ENTITY_RELATIONSHIP_OVERRIDES:
            return ENTITY_RELATIONSHIP_OVERRIDES[key]

    try:
        host = urlparse(homepage_url).netloc.lower().split(":")[0]
    except ValueError:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    host_base = host.split(".")[0] if host else ""

    name_norm = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    sym_norm = re.sub(r"[^a-z0-9]", "", (symbol or "").lower())

    if host_base and name_norm and (host_base in name_norm or name_norm in host_base):
        return None, "PLAUSIBLE"
    if host_base and sym_norm and len(sym_norm) >= 2 and (host_base == sym_norm or sym_norm in host_base):
        return None, "PLAUSIBLE"
    return None, "UNCLEAR"


def record_audit(conn, coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance,
                  entries, entity_rel_status, reason):
    entry1 = entries[0] if len(entries) > 0 else {}
    entry2 = entries[1] if len(entries) > 1 else {}
    conn.execute(
        """
        INSERT INTO shutdown_source_audit (
            coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance,
            entry1_url, entry1_title, entry1_date, entry2_url, entry2_title, entry2_date,
            stable_identifier_available, can_detect_new_entry, entity_relationship, reason, audited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id, source_url) DO UPDATE SET
            canonical_stream_url=excluded.canonical_stream_url,
            verdict=excluded.verdict,
            audited_relevance=excluded.audited_relevance,
            entry1_url=excluded.entry1_url, entry1_title=excluded.entry1_title, entry1_date=excluded.entry1_date,
            entry2_url=excluded.entry2_url, entry2_title=excluded.entry2_title, entry2_date=excluded.entry2_date,
            stable_identifier_available=excluded.stable_identifier_available,
            can_detect_new_entry=excluded.can_detect_new_entry,
            entity_relationship=excluded.entity_relationship,
            reason=excluded.reason,
            audited_at=excluded.audited_at
        """,
        (
            coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance,
            entry1.get("url"), entry1.get("title"), entry1.get("date"),
            entry2.get("url"), entry2.get("title"), entry2.get("date"),
            1 if len(entries) >= 2 else 0, 1 if len(entries) >= 2 else 0,
            entity_rel_status, reason, now_iso(),
        ),
    )


def audit_candidate_source(coingecko_id: str, source_url: str, entity_rel_status: str) -> dict:
    """Re-fetches ONE candidate URL and decides PASS / DOWNGRADE /
    UNRESOLVED per the strict stream rule. Never raises -- any failure
    becomes an UNRESOLVED result for this one source (Task 12: one broken
    site cannot abort the audit).
    """
    result = {
        "source_url": source_url, "canonical_stream_url": None, "verdict": "UNRESOLVED",
        "audited_relevance": None, "entries": [], "reason": None,
    }
    try:
        disq_reason = is_disqualified_url(source_url)
        if disq_reason:
            result.update(verdict="DOWNGRADE", reason=disq_reason)
            return result

        status, body = check_url_reachable(source_url)
        if status != 200:
            result.update(verdict="UNRESOLVED", reason=f"unreachable on re-check (HTTP/err={status})")
            return result

        page = classify_page(source_url, body)
        if page["page_type"] == "LISTING":
            result.update(
                verdict="PASS", canonical_stream_url=source_url,
                audited_relevance=relevance_for_path_or_text(urlparse(source_url).path),
                entries=page["entries"], reason=page["reason"],
            )
            return result

        # Article-or-static: try the parent listing once (Task 7).
        parent_url = parent_path_candidate(source_url)
        if parent_url:
            parent_disq = is_disqualified_url(parent_url)
            if not parent_disq:
                parent_status, parent_body = check_url_reachable(parent_url)
                if parent_status == 200:
                    parent_page = classify_page(parent_url, parent_body)
                    if parent_page["page_type"] == "LISTING":
                        result.update(
                            verdict="PASS", canonical_stream_url=parent_url,
                            audited_relevance=relevance_for_path_or_text(urlparse(parent_url).path),
                            entries=parent_page["entries"],
                            reason=f"original URL was an individual article; parent listing {parent_url} qualifies instead",
                        )
                        return result

        result.update(verdict="DOWNGRADE", reason=page["reason"])
        return result
    except Exception as exc:
        log.warning("audit_candidate_source failed for %s / %s: %s", coingecko_id, source_url, exc)
        result.update(verdict="UNRESOLVED", reason=str(exc))
        return result


SITEMAP_KEYWORDS = ["blog", "news", "press", "announce", "update", "notice"]
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
ROBOTS_SITEMAP_RE = re.compile(r"^\s*Sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def fetch_sitemap_urls(homepage_url: str, max_urls: int = 20) -> list:
    """Task 5 deeper pass. Free, first-party discovery via /sitemap.xml and
    any sitemap(s) referenced from /robots.txt, filtered to URLs whose path
    contains a shutdown-relevant keyword. Bounded, best-effort, never
    raises -- returns [] on any failure (a missing sitemap is normal, not
    an error).
    """
    found = []
    try:
        parsed = urlparse(homepage_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        sitemap_candidates = [f"{origin}/sitemap.xml"]

        robots_status, robots_body = check_url_reachable(f"{origin}/robots.txt")
        if robots_status == 200 and robots_body:
            for match in ROBOTS_SITEMAP_RE.finditer(robots_body):
                sitemap_candidates.append(match.group(1).strip())

        seen = set()
        for sitemap_url in sitemap_candidates:
            if sitemap_url in seen:
                continue
            seen.add(sitemap_url)
            status, body = check_url_reachable(sitemap_url)
            if status != 200 or not body:
                continue
            for loc in SITEMAP_LOC_RE.findall(body):
                loc_path = urlparse(loc).path.lower()
                if any(kw in loc_path for kw in SITEMAP_KEYWORDS) and loc not in found:
                    found.append(loc)
                if len(found) >= max_urls:
                    return found
    except Exception as exc:
        log.warning("fetch_sitemap_urls failed for %s: %s", homepage_url, exc)
    return found


def deeper_candidate_urls(homepage_url: str) -> list:
    """Task 5 deeper pass. A small, fixed set of extra first-party path
    variants and first-party subdomains, beyond
    app.shutdown_source_discovery.CANDIDATE_PATHS -- still bounded, not a
    brute-force crawl. Returns plausible URLs to probe; does not fetch
    them itself.
    """
    try:
        parsed = urlparse(homepage_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc.lower().split(":")[0]
        host = host[4:] if host.startswith("www.") else host
    except ValueError:
        return []

    urls = [f"{origin}{path}" for path in DEEPER_CANDIDATE_PATHS]
    for prefix in FIRST_PARTY_SUBDOMAIN_PREFIXES:
        urls.append(f"{parsed.scheme}://{prefix}.{host}")
    return urls


def deeper_discovery_for_d_project(coingecko_id: str, homepage_url: str, max_probes: int = 10) -> dict:
    """Task 5: a second, deeper pass for a project that raw discovery
    classified as D. Tries sitemap.xml/robots.txt references and the
    bounded extra path/subdomain list; for each first-party candidate
    found, runs it through the same strict audit_candidate_source logic
    used for the raw A/B results, so a D project can only be upgraded via
    a genuinely qualifying stream -- never via search-engine results
    (Task 5 explicitly disallows those as proof).

    Never raises -- returns {"upgraded": False, "reason": ...} on any
    failure so one bad site cannot abort the batch.
    """
    result = {"coingecko_id": coingecko_id, "upgraded": False, "qualifying_url": None,
              "audited_relevance": None, "entries": [], "candidates_tried": 0, "reason": None}
    try:
        if not homepage_url:
            result["reason"] = "no homepage URL on record"
            return result

        candidates = fetch_sitemap_urls(homepage_url) + deeper_candidate_urls(homepage_url)
        candidates = [c for c in candidates if is_first_party(homepage_url, c)]
        # De-dupe while preserving order.
        seen = set()
        deduped = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        candidates = deduped[:max_probes]

        for url in candidates:
            result["candidates_tried"] += 1
            audit = audit_candidate_source(coingecko_id, url, "UNCLEAR")
            if audit["verdict"] == "PASS":
                result.update(
                    upgraded=True, qualifying_url=audit["canonical_stream_url"],
                    audited_relevance=audit["audited_relevance"], entries=audit["entries"],
                    reason=f"deeper pass found qualifying stream at {audit['canonical_stream_url']}",
                )
                return result

        result["reason"] = f"no qualifying stream found among {len(candidates)} deeper-pass candidates"
        return result
    except Exception as exc:
        log.warning("deeper_discovery_for_d_project failed for %s: %s", coingecko_id, exc)
        result["reason"] = str(exc)
        return result


def wilson_confidence_interval(successes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson 95% (default z=1.96) confidence interval for a binomial
    proportion. Pure function. Returns (lower, upper) as fractions in
    [0,1]. Standard formula -- see e.g. Wilson (1927).
    """
    if n == 0:
        return (0.0, 0.0)
    p_hat = successes / n
    denom = 1 + z**2 / n
    center = p_hat + z**2 / (2 * n)
    margin = z * ((p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) ** 0.5)
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    return (max(0.0, lower), min(1.0, upper))
