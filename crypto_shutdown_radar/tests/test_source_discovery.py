from unittest.mock import patch

from app import source_discovery as sd
from app.db import get_conn


HOMEPAGE_HTML_WITH_FEED = """
<html><head>
<link rel="alternate" type="application/rss+xml" title="Blog" href="/blog/feed.xml">
</head><body>hi</body></html>
"""

VALID_FEED_BODY = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>x</title><link>https://example.com/1</link><guid>1</guid></item>
</channel></rss>"""


def test_extract_feed_links_finds_declared_feed():
    links = sd.extract_feed_links(HOMEPAGE_HTML_WITH_FEED, "https://example.com/")
    assert links == ["https://example.com/blog/feed.xml"]


def test_extract_feed_links_empty_when_none_declared():
    assert sd.extract_feed_links("<html><head></head></html>", "https://example.com/") == []


def test_github_org_or_repo_from_url_repo():
    assert sd.github_org_or_repo_from_url("https://github.com/ethereum/go-ethereum") == ("ethereum", "go-ethereum")


def test_github_org_or_repo_from_url_org_only():
    assert sd.github_org_or_repo_from_url("https://github.com/ethereum") == ("ethereum", None)


def test_github_org_or_repo_from_url_invalid():
    assert sd.github_org_or_repo_from_url("not a url") is None


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_check_github_activity_feed_builds_releases_atom_for_repo():
    with patch.object(sd, "check_url_reachable", return_value=(200, VALID_FEED_BODY)):
        feed_url, ok = sd.check_github_activity_feed("https://github.com/ethereum/go-ethereum")
    assert feed_url == "https://github.com/ethereum/go-ethereum/releases.atom"
    assert ok is True


def test_check_github_activity_feed_builds_user_atom_for_org_only():
    with patch.object(sd, "check_url_reachable", return_value=(200, VALID_FEED_BODY)):
        feed_url, ok = sd.check_github_activity_feed("https://github.com/ethereum")
    assert feed_url == "https://github.com/ethereum.atom"
    assert ok is True


def test_check_github_activity_feed_reports_failure_honestly():
    with patch.object(sd, "check_url_reachable", return_value=(404, "")):
        feed_url, ok = sd.check_github_activity_feed("https://github.com/doesnotexist/doesnotexist")
    assert ok is False


def test_check_subreddit_feed_builds_rss_url():
    with patch.object(sd, "check_url_reachable", return_value=(200, VALID_FEED_BODY)):
        feed_url, ok = sd.check_subreddit_feed("https://www.reddit.com/r/ethereum/")
    assert feed_url == "https://www.reddit.com/r/ethereum/.rss"
    assert ok is True


def test_discover_blog_feed_prefers_declared_link_over_guess():
    with patch.object(sd, "check_url_reachable", return_value=(200, VALID_FEED_BODY)):
        feed_url, how = sd.discover_blog_feed("https://example.com", HOMEPAGE_HTML_WITH_FEED)
    assert feed_url == "https://example.com/blog/feed.xml"
    assert "declared" in how


def test_discover_blog_feed_falls_back_to_common_path_probe():
    def fake_check(url):
        if url.endswith("/feed"):
            return 200, VALID_FEED_BODY
        return 404, ""

    with patch.object(sd, "check_url_reachable", side_effect=fake_check):
        feed_url, how = sd.discover_blog_feed("https://example.com", "<html><head></head></html>")
    assert feed_url == "https://example.com/feed"
    assert "heuristic" in how


def test_discover_blog_feed_returns_none_when_nothing_found():
    with patch.object(sd, "check_url_reachable", return_value=(404, "")):
        feed_url, how = sd.discover_blog_feed("https://example.com", "<html><head></head></html>")
    assert feed_url is None
    assert how is None


def test_discover_official_sources_records_detail_fetch_error_honestly():
    with patch.object(sd, "fetch_coin_detail", return_value=None):
        result = sd.discover_official_sources_for_coin("some-coin")
    assert result["detail_fetch_error"] is True
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM official_sources WHERE coingecko_id='some-coin'").fetchone()["c"]
    assert count == 0  # nothing fabricated when the detail call itself failed


def test_discover_official_sources_full_pipeline_records_sources():
    fake_detail = {
        "links": {
            "homepage": ["https://example.com"],
            "repos_url": {"github": ["https://github.com/exampleorg/examplerepo"]},
            "subreddit_url": "https://www.reddit.com/r/exampleorg/",
            "announcement_url": [""],
        }
    }

    def fake_check(url, timeout=15):
        if url == "https://example.com":
            return 200, HOMEPAGE_HTML_WITH_FEED
        if "feed.xml" in url or "releases.atom" in url or ".rss" in url:
            return 200, VALID_FEED_BODY
        return 404, ""

    with patch.object(sd, "fetch_coin_detail", return_value=fake_detail), \
         patch.object(sd, "check_url_reachable", side_effect=fake_check):
        result = sd.discover_official_sources_for_coin("example-coin")

    assert result["homepage_reachable"] is True
    assert result["blog_rss_found"] == "https://example.com/blog/feed.xml"
    assert result["github_feed_found"] == "https://github.com/exampleorg/examplerepo/releases.atom"
    assert result["subreddit_feed_found"] == "https://www.reddit.com/r/exampleorg/.rss"

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM official_sources WHERE coingecko_id='example-coin'").fetchall()
    types = {r["source_type"] for r in rows}
    assert {"homepage", "rss_feed", "github", "subreddit"}.issubset(types)
    assert all(r["evidence"] for r in rows)  # every row has provenance


def test_discover_for_sample_stops_at_time_budget():
    calls = []

    def fake_discover(cid, cached_homepage=""):
        calls.append(cid)
        return {"coingecko_id": cid, "homepage_reachable": False, "blog_rss_found": None,
                "github_feed_found": None, "subreddit_feed_found": None, "detail_fetch_error": False}

    rows = [{"coingecko_id": f"coin-{i}", "website": ""} for i in range(10)]
    # A negative budget guarantees the very first elapsed-time check exceeds
    # it, so this deterministically stops before processing any coin --
    # avoids fragile mocking of time.monotonic for what is otherwise a
    # real-wall-clock-timed loop.
    with patch.object(sd, "discover_official_sources_for_coin", side_effect=fake_discover):
        result = sd.discover_for_sample(rows, time_budget_seconds=-1)

    assert result["stopped_early_due_to_time_budget"] is True
    assert result["completed"] == 0
    assert result["requested"] == 10
    assert calls == []
