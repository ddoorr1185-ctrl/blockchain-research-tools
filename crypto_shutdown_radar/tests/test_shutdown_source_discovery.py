import time
from unittest.mock import patch

from app import shutdown_source_discovery as ssd
from app.db import get_conn


HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS = """
<html><body>
<a href="/blog">Our Blog</a>
<a href="/news">Latest News</a>
<a href="/announcements">Announcements</a>
<a href="https://twitter.com/example">Follow us</a>
<a href="/careers">We're hiring</a>
<a href="https://other-domain.example/community">Community forum</a>
</body></html>
"""

ARTICLE_LISTING_HTML = """
<html><body>
<article>Post one about something, dated 2026-01-01</article>
<article>Post two about something else, dated 2026-02-01</article>
<article>Post three, dated 2026-03-01</article>
</body></html>
"""

STATIC_PAGE_HTML = "<html><body><h1>About us</h1><p>We are a company.</p></body></html>"

SLUG_LISTING_HTML = """
<html><body>
<a href="/blog/announcing-our-new-feature">Announcing our new feature</a>
<a href="/blog/quarterly-update-2026">Quarterly update</a>
<a href="/blog/partnership-with-acme">Partnership with Acme</a>
</body></html>
"""


def test_relevance_for_path_or_text_classifies_correctly():
    assert ssd.relevance_for_path_or_text("/announcements") == "HIGH"
    assert ssd.relevance_for_path_or_text("press-releases") == "HIGH"
    assert ssd.relevance_for_path_or_text("/news") == "MEDIUM"
    assert ssd.relevance_for_path_or_text("blog") == "MEDIUM"
    assert ssd.relevance_for_path_or_text("/community") == "LOW"
    assert ssd.relevance_for_path_or_text("/careers") == "LOW"  # unrecognized -> default LOW, never assumed HIGH


def test_is_first_party_allows_subdomains_rejects_other_domains():
    assert ssd.is_first_party("https://example.com", "https://example.com/blog") is True
    assert ssd.is_first_party("https://example.com", "https://blog.example.com/post") is True
    assert ssd.is_first_party("https://www.example.com", "https://example.com/news") is True
    assert ssd.is_first_party("https://example.com", "https://totally-different.example/news") is False
    assert ssd.is_first_party("https://example.com", "https://twitter.com/example") is False


def test_extract_candidate_links_finds_path_and_text_matches_excludes_unrelated():
    links = ssd.extract_candidate_links(HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, "https://example.com")
    urls = {l["url"] for l in links}
    assert "https://example.com/blog" in urls
    assert "https://example.com/news" in urls
    assert "https://example.com/announcements" in urls
    assert "https://twitter.com/example" not in urls  # not matched by path or keyword
    assert "https://example.com/careers" not in urls  # "hiring"/"careers" isn't a recognized keyword
    by_url = {l["url"]: l for l in links}
    assert by_url["https://example.com/announcements"]["relevance"] == "HIGH"
    assert by_url["https://example.com/news"]["relevance"] == "MEDIUM"


def test_extract_candidate_links_bounded_not_a_brute_force_crawl():
    """The candidate path list must stay small and fixed -- this test
    pins its size so a future change notices if it balloons."""
    assert len(ssd.CANDIDATE_PATHS) <= 15


def test_assess_monitorability_detects_article_listing():
    result = ssd.assess_monitorability(ARTICLE_LISTING_HTML)
    assert result["monitorable"] is True
    assert "article" in result["strategy"]


def test_assess_monitorability_detects_slug_listing():
    result = ssd.assess_monitorability(SLUG_LISTING_HTML)
    assert result["monitorable"] is True


def test_assess_monitorability_rejects_static_page():
    result = ssd.assess_monitorability(STATIC_PAGE_HTML)
    assert result["monitorable"] is False
    assert "static" in result["reason"]


def test_classify_project_coverage_high_relevance_gives_a():
    pages = [{"url": "https://x.example/announcements", "relevance": "HIGH", "monitorable": True}]
    result = ssd.classify_project_coverage(pages, github_feed_found=False, subreddit_feed_found=False)
    assert result["classification"] == "A"
    assert result["shutdown_relevant_first_party_coverage"] is True


def test_classify_project_coverage_medium_relevance_gives_b():
    pages = [{"url": "https://x.example/news", "relevance": "MEDIUM", "monitorable": True}]
    result = ssd.classify_project_coverage(pages, github_feed_found=False, subreddit_feed_found=False)
    assert result["classification"] == "B"
    assert result["shutdown_relevant_first_party_coverage"] is True


def test_classify_project_coverage_github_alone_gives_c_not_a_or_b():
    """Task 8: GitHub activity must never inflate the result into A or B."""
    result = ssd.classify_project_coverage([], github_feed_found=True, subreddit_feed_found=False)
    assert result["classification"] == "C"
    assert result["shutdown_relevant_first_party_coverage"] is False
    assert result["technical_monitorable_coverage"] is True


def test_classify_project_coverage_subreddit_alone_gives_c():
    result = ssd.classify_project_coverage([], github_feed_found=False, subreddit_feed_found=True)
    assert result["classification"] == "C"
    assert result["shutdown_relevant_first_party_coverage"] is False


def test_classify_project_coverage_nothing_gives_d():
    result = ssd.classify_project_coverage([], github_feed_found=False, subreddit_feed_found=False)
    assert result["classification"] == "D"
    assert result["technical_monitorable_coverage"] is False


def test_classify_project_coverage_unmonitorable_high_page_does_not_count():
    """A HIGH-relevance page that isn't actually monitorable (e.g. a
    static one-off page) must not grant A."""
    pages = [{"url": "https://x.example/announcements", "relevance": "HIGH", "monitorable": False}]
    result = ssd.classify_project_coverage(pages, github_feed_found=False, subreddit_feed_found=False)
    assert result["classification"] == "D"


# --- network-touching function: mocked ---

class _FakeResp:
    pass


def test_discover_shutdown_sources_handles_one_broken_homepage_without_raising():
    with patch("app.shutdown_source_discovery.check_url_reachable", side_effect=RuntimeError("boom")):
        result = ssd.discover_shutdown_sources_for_project(
            "broken-coin", "https://broken.example", HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, skip_if_cached=False
        )
    assert result["error"] is not None
    assert result["coingecko_id"] == "broken-coin"


def test_discover_shutdown_sources_probes_and_records_candidates():
    def fake_check(url, timeout=15):
        if "announcements" in url:
            return 200, ARTICLE_LISTING_HTML
        return 200, STATIC_PAGE_HTML

    with patch("app.shutdown_source_discovery.check_url_reachable", side_effect=fake_check):
        result = ssd.discover_shutdown_sources_for_project(
            "probe-coin", "https://example.com", HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, skip_if_cached=False
        )
    assert result["error"] is None
    assert result["candidates_found"] == 3  # blog, news, announcements
    pages_by_url = {p["url"]: p for p in result["pages"]}
    assert pages_by_url["https://example.com/announcements"]["monitorable"] is True
    assert pages_by_url["https://example.com/announcements"]["relevance"] == "HIGH"

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM official_sources WHERE coingecko_id='probe-coin' AND source_type='first_party_page'"
        ).fetchall()
    assert len(rows) == 3
    assert all(r["shutdown_relevance"] in ("HIGH", "MEDIUM", "LOW") for r in rows)


def test_discover_shutdown_sources_reuses_cache_without_reprobing():
    def fake_check(url, timeout=15):
        return 200, ARTICLE_LISTING_HTML

    with patch("app.shutdown_source_discovery.check_url_reachable", side_effect=fake_check):
        first = ssd.discover_shutdown_sources_for_project(
            "cache-coin", "https://example.com", HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, skip_if_cached=False
        )
    assert first["from_cache"] is False

    probe_calls = []

    def counting_check(url, timeout=15):
        probe_calls.append(url)
        return 200, ARTICLE_LISTING_HTML

    with patch("app.shutdown_source_discovery.check_url_reachable", side_effect=counting_check):
        second = ssd.discover_shutdown_sources_for_project(
            "cache-coin", "https://example.com", HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, skip_if_cached=True
        )
    assert second["from_cache"] is True
    assert probe_calls == []  # no real re-probing happened


def test_discover_shutdown_sources_no_sleeps_involved():
    """There is no retry/backoff path here at all -- confirm a batch of
    probes completes with negligible wall-clock time even when every
    probe "fails" (mocked), i.e. nothing resembling a 60s sleep exists."""
    with patch("app.shutdown_source_discovery.check_url_reachable", return_value=(503, "")), \
         patch("time.sleep") as mock_sleep:
        t0 = time.monotonic()
        ssd.discover_shutdown_sources_for_project(
            "fast-coin", "https://example.com", HOMEPAGE_WITH_BLOG_AND_NEWS_LINKS, skip_if_cached=False
        )
        elapsed = time.monotonic() - t0
    mock_sleep.assert_not_called()
    assert elapsed < 1
