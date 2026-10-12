"""GOLD layer: a second, stricter audit pass on top of app.shutdown_source_audit
(the STRICT layer).

The STRICT layer's >=2-anchor rule turned out to be insufficient: real
projects produced "passing" entries that were actually navigation/category/
filter labels ("Education", "Ecosystem", "Past Events", "Gold articles",
"In The News", "Announcements" as a filter tab), API/docs query endpoints
(bittensor.com/docs/query), or literal unrendered template placeholders
(Zcash's `{{{ data._highlightResult... }}}`). This module adds genuine-
publication validation on top of the same disqualification rules, plus
three additional, zero-cost evidence mechanisms that a plain anchor-regex
heuristic misses entirely: JSON-LD Article/NewsArticle/BlogPosting markup,
RSS/Atom autodiscovery, and embedded JSON (e.g. Next.js __NEXT_DATA__)
article listings.

Never overwrites RAW_CLASSIFICATION or AUDITED_CLASSIFICATION (the STRICT
layer) -- writes to the separate gold_source_audit / manual_review_queue
tables and the ADDITIVE project_audit_classification.gold_classification
column (see app.db).
"""
import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser

from app.shutdown_source_audit import (
    is_disqualified_url, parent_path_candidate, check_url_reachable,
    relevance_for_path_or_text,
)
from app.shutdown_source_discovery import ANCHOR_RE, TAG_STRIP_RE, DATE_LIKE_RE

log = logging.getLogger("shutdown_source_gold_audit")

# Exact-match (after lowercasing + stripping punctuation) titles that are
# navigation/category/filter labels, not publications -- the concrete
# false positives reported from the real-Mac GOLD run, plus the obvious
# generalizations of each.
NAV_CATEGORY_BLOCKLIST = {
    "education", "developers", "ecosystem", "overview", "home", "about", "about us",
    "our team", "contact", "contact us", "pr agency", "contact our pr agency",
    "past events", "upcoming events", "events", "gold articles", "silver articles",
    "in the news", "news", "press", "blog", "resources", "community", "docs",
    "documentation", "api", "pricing", "careers", "support", "help", "terms",
    "privacy", "balances", "login", "sign up", "register", "faq",
    "read more", "learn more", "view all", "see all", "load more", "next", "previous",
    "tokenization", "interoperability", "announcements",  # only a bare filter/category label; a real dated entry titled just "Announcements" is vanishingly rare and indistinguishable from the filter tab without more evidence
}
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}")
PAGINATION_RE = re.compile(r"^(page\s*\d+|\d+)$", re.IGNORECASE)
SLUG_RE = re.compile(r"[a-z0-9-]{6,}", re.IGNORECASE)

JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)
ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting"}
FEED_AUTODISCOVERY_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE
)
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)
GENERIC_INLINE_JSON_RE = re.compile(r"<script[^>]*>\s*(\{.*?\})\s*</script>", re.IGNORECASE | re.DOTALL)


def is_navigation_like(title: str) -> bool:
    """True if `title` reads as a nav/category/filter label rather than a
    genuine publication title (Task 1's reject list).
    """
    if not title:
        return True
    cleaned = title.strip().lower().strip(":").strip()
    if TEMPLATE_PLACEHOLDER_RE.search(title):
        return True
    if PAGINATION_RE.match(cleaned):
        return True
    if cleaned in NAV_CATEGORY_BLOCKLIST:
        return True
    return False


def _entry_is_genuine(entry: dict, mechanism_is_structured: bool) -> bool:
    """Task 1: an entry counts only if it looks like an actual publication.
    Structured mechanisms (JSON-LD/RSS/embedded JSON) are trusted on title
    alone (still run through the nav blocklist) since their very presence
    is already strong evidence; a plain anchor needs a date OR a genuinely
    article-shaped (multi-word, hyphenated) slug as corroboration.
    """
    title = (entry.get("title") or "").strip()
    if not title or is_navigation_like(title):
        return False
    if len(title.split()) < 2 and not SLUG_RE.search(urlparse(entry.get("url") or "").path):
        return False
    if mechanism_is_structured:
        return True
    has_date = bool(entry.get("date"))
    slug = urlparse(entry.get("url") or "").path.rstrip("/").rsplit("/", 1)[-1]
    slug_like = slug.count("-") >= 2
    return has_date or slug_like


def _dedupe_and_filter(entries: list, mechanism_is_structured: bool) -> list:
    seen = set()
    out = []
    for e in entries:
        url = e.get("url")
        if not url or url in seen:
            continue
        if not _entry_is_genuine(e, mechanism_is_structured):
            continue
        seen.add(url)
        out.append(e)
    return out


def extract_jsonld_articles(html: str, base_url: str) -> list:
    """Task 1/4: JSON-LD Article/NewsArticle/BlogPosting markup -- a
    structured, machine-readable publication signal most real news/blog
    pages already emit for SEO, independent of how the page renders.
    """
    out = []
    for block in JSONLD_SCRIPT_RE.findall(html):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        flattened = []
        for c in candidates:
            if isinstance(c, dict) and "@graph" in c and isinstance(c["@graph"], list):
                flattened.extend(c["@graph"])
            else:
                flattened.append(c)
        for item in flattened:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(t in ARTICLE_TYPES for t in types if isinstance(t, str)):
                continue
            title = item.get("headline") or item.get("name")
            url = item.get("url") or item.get("@id")
            date = item.get("datePublished") or item.get("dateModified")
            if title and url:
                out.append({"title": str(title), "url": urljoin(base_url, str(url)), "date": date})
    return _dedupe_and_filter(out, mechanism_is_structured=True)


def discover_feed_url(html: str, base_url: str) -> Optional[str]:
    match = FEED_AUTODISCOVERY_RE.search(html)
    if not match:
        return None
    return urljoin(base_url, match.group(1))


def extract_rss_entries(feed_url: str, base_url: str) -> list:
    """Task 1/4: RSS/Atom autodiscovery -- one extra, free request to the
    feed the page itself declares. Never raises; a malformed/unreachable
    feed just yields no entries.
    """
    try:
        status, body = check_url_reachable(feed_url)
        if status != 200 or not body:
            return []
        parsed = feedparser.parse(body)
        out = []
        for item in parsed.entries:
            title = item.get("title")
            link = item.get("link")
            date = item.get("published") or item.get("updated")
            if title and link:
                out.append({"title": title, "url": urljoin(base_url, link), "date": date})
        return _dedupe_and_filter(out, mechanism_is_structured=True)
    except Exception as exc:
        log.warning("extract_rss_entries failed for %s: %s", feed_url, exc)
        return []


def _walk_json_for_article_lists(node, depth=0, max_depth=6, visited=None) -> list:
    """Bounded recursive search for a list of dict-shaped "article-like"
    entries (each with a title/headline and a slug/url) inside an
    arbitrary embedded-JSON blob (e.g. Next.js __NEXT_DATA__). Depth- and
    node-count-bounded so a large page can't cause a runaway scan.
    """
    if visited is None:
        visited = [0]
    if depth > max_depth or visited[0] > 20000:
        return []
    visited[0] += 1

    if isinstance(node, list):
        dict_items = [n for n in node if isinstance(n, dict)]
        if len(dict_items) >= 2:
            candidate_entries = []
            for item in dict_items:
                title = item.get("title") or item.get("headline") or item.get("name")
                url_or_slug = item.get("url") or item.get("slug") or item.get("href") or item.get("path")
                date = item.get("date") or item.get("publishedAt") or item.get("datePublished") or item.get("created_at")
                if title and url_or_slug:
                    candidate_entries.append({"title": str(title), "url": str(url_or_slug), "date": date})
            if len(candidate_entries) >= 2:
                return candidate_entries
        found = []
        for item in node:
            found.extend(_walk_json_for_article_lists(item, depth + 1, max_depth, visited))
            if found:
                return found
        return found
    if isinstance(node, dict):
        for value in node.values():
            found = _walk_json_for_article_lists(value, depth + 1, max_depth, visited)
            if found:
                return found
    return []


def extract_embedded_json_entries(html: str, base_url: str) -> list:
    """Task 1/4: embedded JSON article listings (Next.js __NEXT_DATA__ or
    a similar inline JSON blob) -- common on JS-rendered sites where the
    server still ships the data as JSON even though there are no
    server-rendered <a> tags for our anchor heuristic to find.
    """
    blocks = NEXT_DATA_RE.findall(html) or GENERIC_INLINE_JSON_RE.findall(html)
    for block in blocks[:5]:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        raw_entries = _walk_json_for_article_lists(data)
        if raw_entries:
            normalized = [{"title": e["title"], "url": urljoin(base_url, e["url"]), "date": e.get("date")} for e in raw_entries]
            filtered = _dedupe_and_filter(normalized, mechanism_is_structured=True)
            if len(filtered) >= 2:
                return filtered
    return []


def extract_html_article_cards(html: str, base_url: str) -> list:
    """Task 1: the STRICT layer's anchor-based heuristic, but with genuine-
    publication validation applied -- rejects nav/category anchors,
    template placeholders, and pagination, and requires a date or an
    article-shaped slug as corroboration (see _entry_is_genuine).
    """
    base_path = urlparse(base_url).path.rstrip("/")
    entries = []
    for match in ANCHOR_RE.finditer(html):
        href, inner_html = match.group(1), match.group(2)
        title = TAG_STRIP_RE.sub(" ", inner_html).strip()
        if not title:
            continue
        resolved = urljoin(base_url, href)
        resolved_path = urlparse(resolved).path.rstrip("/")
        if not resolved_path.startswith(base_path + "/") or resolved_path == base_path:
            continue
        window = html[max(0, match.start() - 200):match.start()]
        date_match = DATE_LIKE_RE.search(window)
        entries.append({"url": resolved, "title": title, "date": date_match.group(0) if date_match else None})
    return _dedupe_and_filter(entries, mechanism_is_structured=False)


def gold_classify_page(url: str, html: Optional[str]) -> dict:
    """Combines every evidence mechanism, in priority order (strongest,
    least ambiguous first), into one verdict for a single already-fetched
    page. Returns {"is_stream": bool, "entries": [...], "mechanism": str|None,
    "confidence": "HIGH"|"LOW", "reason": str}.

    HIGH confidence: a structured mechanism (JSON-LD/RSS/embedded JSON),
    or >=2 HTML article-card entries where at least one carries a real
    date. LOW confidence: exactly the HTML-article-card mechanism with
    entries backed only by the slug heuristic (no dates at all) -- Task 8
    says this must go to manual review, not be auto-decided.
    """
    disq_reason = is_disqualified_url(url)
    if disq_reason:
        return {"is_stream": False, "entries": [], "mechanism": None, "confidence": "HIGH", "reason": disq_reason}
    if not html:
        return {"is_stream": False, "entries": [], "mechanism": None, "confidence": "HIGH", "reason": "no content to inspect"}

    jsonld = extract_jsonld_articles(html, url)
    if len(jsonld) >= 2:
        return {"is_stream": True, "entries": jsonld, "mechanism": "JSON-LD", "confidence": "HIGH",
                "reason": f"{len(jsonld)} JSON-LD Article/NewsArticle/BlogPosting entries found"}

    feed_url = discover_feed_url(html, url)
    if feed_url:
        rss_entries = extract_rss_entries(feed_url, url)
        if len(rss_entries) >= 2:
            return {"is_stream": True, "entries": rss_entries, "mechanism": "RSS", "confidence": "HIGH",
                    "reason": f"{len(rss_entries)} RSS/Atom entries found via {feed_url}"}

    embedded = extract_embedded_json_entries(html, url)
    if len(embedded) >= 2:
        return {"is_stream": True, "entries": embedded, "mechanism": "EMBEDDED_JSON", "confidence": "HIGH",
                "reason": f"{len(embedded)} embedded-JSON article entries found"}

    cards = extract_html_article_cards(html, url)
    if len(cards) >= 2:
        any_dated = any(e.get("date") for e in cards)
        confidence = "HIGH" if any_dated else "LOW"
        reason = f"{len(cards)} genuine HTML article-card entries found" if any_dated else \
            f"{len(cards)} article-shaped entries found, but none carry a date -- slug heuristic only"
        return {"is_stream": True, "entries": cards, "mechanism": "HTML_ARTICLE_CARDS", "confidence": confidence, "reason": reason}

    return {"is_stream": False, "entries": [], "mechanism": None, "confidence": "HIGH",
            "reason": "no genuine publication entries found (navigation/category/placeholder content, or fewer than 2 real entries)"}


def new_entry_detectable(entries: list, mechanism: Optional[str]) -> bool:
    """Task 7: True only if entries carry a stable, distinct identifier
    (a real URL per entry) and the mechanism is one a poller could re-run
    (RSS/JSON-LD/embedded JSON/HTML cards/sitemap all qualify; no
    mechanism, or non-distinct URLs, do not).
    """
    if not mechanism or len(entries) < 2:
        return False
    urls = [e.get("url") for e in entries]
    return len(set(urls)) == len(urls) and all(urls)


def gold_audit_candidate_source(coingecko_id: str, source_url: str) -> dict:
    """Re-fetches ONE candidate URL and decides GOLD_PASS / GOLD_DOWNGRADE /
    GOLD_UNRESOLVED. A LOW-confidence stream finding is deliberately never
    auto-PASSed (Task 8) -- it comes back GOLD_UNRESOLVED so the caller can
    queue it for manual review instead of guessing. Never raises.
    """
    result = {
        "source_url": source_url, "canonical_stream_url": None, "verdict": "GOLD_UNRESOLVED",
        "audited_relevance": None, "entries": [], "mechanism": None, "new_entry_detectable": False,
        "reason": None,
    }
    try:
        disq_reason = is_disqualified_url(source_url)
        if disq_reason:
            result.update(verdict="GOLD_DOWNGRADE", reason=disq_reason)
            return result

        status, body = check_url_reachable(source_url)
        if status != 200:
            result.update(verdict="GOLD_UNRESOLVED", reason=f"unreachable on re-check (HTTP/err={status})")
            return result

        page = gold_classify_page(source_url, body)
        if page["is_stream"] and page["confidence"] == "HIGH":
            result.update(
                verdict="GOLD_PASS", canonical_stream_url=source_url,
                audited_relevance=relevance_for_path_or_text(urlparse(source_url).path),
                entries=page["entries"], mechanism=page["mechanism"],
                new_entry_detectable=new_entry_detectable(page["entries"], page["mechanism"]),
                reason=page["reason"],
            )
            return result
        if page["is_stream"] and page["confidence"] == "LOW":
            result.update(verdict="GOLD_UNRESOLVED", entries=page["entries"], mechanism=page["mechanism"],
                          reason=f"low-confidence stream evidence, needs manual review: {page['reason']}")
            return result

        parent_url = parent_path_candidate(source_url)
        if parent_url and not is_disqualified_url(parent_url):
            parent_status, parent_body = check_url_reachable(parent_url)
            if parent_status == 200:
                parent_page = gold_classify_page(parent_url, parent_body)
                if parent_page["is_stream"] and parent_page["confidence"] == "HIGH":
                    result.update(
                        verdict="GOLD_PASS", canonical_stream_url=parent_url,
                        audited_relevance=relevance_for_path_or_text(urlparse(parent_url).path),
                        entries=parent_page["entries"], mechanism=parent_page["mechanism"],
                        new_entry_detectable=new_entry_detectable(parent_page["entries"], parent_page["mechanism"]),
                        reason=f"original URL was not a genuine stream; parent listing {parent_url} qualifies instead",
                    )
                    return result
                if parent_page["is_stream"] and parent_page["confidence"] == "LOW":
                    result.update(verdict="GOLD_UNRESOLVED", entries=parent_page["entries"], mechanism=parent_page["mechanism"],
                                  reason=f"parent listing has low-confidence stream evidence, needs manual review: {parent_page['reason']}")
                    return result

        result.update(verdict="GOLD_DOWNGRADE", reason=page["reason"])
        return result
    except Exception as exc:
        log.warning("gold_audit_candidate_source failed for %s / %s: %s", coingecko_id, source_url, exc)
        result.update(verdict="GOLD_UNRESOLVED", reason=str(exc))
        return result
