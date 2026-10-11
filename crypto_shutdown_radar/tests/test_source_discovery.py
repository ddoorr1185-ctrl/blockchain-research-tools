import time
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


def test_enrich_missing_link_metadata_never_blocks_on_repeated_429():
    """The exact bug being fixed: on a real Mac, CoinGecko 429s with a 60s
    Retry-After were sleeping once per coin per retry inside the old
    inline-per-coin fetch_coin_detail call. enrich_missing_link_metadata
    must use get_json_no_retry and move on immediately.
    """
    call_count = {"n": 0}

    def fake_get_json(url, params=None):
        call_count["n"] += 1
        return "RATE_LIMITED", None

    coin_ids = [f"coin-{i}" for i in range(20)]
    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json), \
         patch.object(sd.time, "sleep", return_value=None):
        t0 = time.monotonic()
        summary = sd.enrich_missing_link_metadata(coin_ids, request_budget=20, time_budget_seconds=90)
        elapsed = time.monotonic() - t0

    assert summary["deferred_rate_limited"] == 20
    assert summary["newly_ok"] == 0
    assert call_count["n"] == 20
    assert elapsed < 2  # 20 "rate limited" coins must not cost 20x60s


def test_enrich_missing_link_metadata_persists_immediately_per_coin():
    """Partial progress survives even if the process were killed mid-run --
    verified here by checking the DB after processing only some coins."""
    def fake_get_json(url, params=None):
        cid = url.rsplit("/", 1)[-1]
        return "OK", {"links": {"repos_url": {"github": [f"https://github.com/org/{cid}"]}, "subreddit_url": "", "announcement_url": [""]}}

    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json):
        sd.enrich_missing_link_metadata(["coin-a", "coin-b"], request_budget=1, time_budget_seconds=90)

    with get_conn() as conn:
        row_a = sd.get_cached_link_metadata(conn, "coin-a")
        row_b = sd.get_cached_link_metadata(conn, "coin-b")
    assert row_a is not None and row_a["fetch_status"] == "OK"
    assert row_b is None  # request_budget=1 stopped before coin-b was attempted


def test_enrich_missing_link_metadata_skips_already_cached_coins():
    with get_conn() as conn:
        sd.record_link_metadata(conn, "already-done", "https://github.com/x/y", None, None, None, "OK")

    calls = []

    def fake_get_json(url, params=None):
        calls.append(url)
        return "OK", {"links": {}}

    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json):
        summary = sd.enrich_missing_link_metadata(["already-done", "new-coin"], request_budget=10, time_budget_seconds=90)

    assert calls == [f"{sd.COINGECKO_BASE}/coins/new-coin"]
    assert summary["already_cached_skipped"] == 1
    assert summary["attempted"] == 1


def test_discover_sources_from_cache_reports_missing_metadata_without_any_coingecko_call():
    with patch.object(sd, "get_json_no_retry") as mock_fetch, \
         patch.object(sd, "check_url_reachable", return_value=(404, "")):
        result = sd.discover_sources_from_cache("never-enriched-coin", cached_homepage="")

    mock_fetch.assert_not_called()  # zero CoinGecko calls from this function, ever
    assert result["github_status"] == "MISSING_METADATA"
    assert result["subreddit_status"] == "MISSING_METADATA"
    assert result["announcement_status"] == "MISSING_METADATA"


def test_discover_sources_from_cache_reports_deferred_for_rate_limited_metadata():
    with get_conn() as conn:
        sd.record_link_metadata(conn, "rl-coin", None, None, None, None, "RATE_LIMITED")

    with patch.object(sd, "get_json_no_retry") as mock_fetch, \
         patch.object(sd, "check_url_reachable", return_value=(404, "")):
        result = sd.discover_sources_from_cache("rl-coin", cached_homepage="")

    mock_fetch.assert_not_called()
    assert "DEFERRED" in result["github_status"]
    assert "RATE_LIMITED" in result["github_status"]


def test_discover_sources_from_cache_uses_cached_github_metadata():
    with get_conn() as conn:
        sd.record_link_metadata(conn, "example-coin", "https://github.com/exampleorg/examplerepo",
                                 "https://www.reddit.com/r/exampleorg/", None, None, "OK")

    def fake_check(url, timeout=15):
        if url == "https://example.com":
            return 200, HOMEPAGE_HTML_WITH_FEED
        if "feed.xml" in url or "releases.atom" in url or ".rss" in url:
            return 200, VALID_FEED_BODY
        return 404, ""

    with patch.object(sd, "get_json_no_retry") as mock_fetch, \
         patch.object(sd, "check_url_reachable", side_effect=fake_check):
        result = sd.discover_sources_from_cache("example-coin", cached_homepage="https://example.com")

    mock_fetch.assert_not_called()  # entirely from cache; github.com/reddit.com calls are not CoinGecko
    assert result["homepage_reachable"] is True
    assert result["blog_rss_found"] == "https://example.com/blog/feed.xml"
    assert result["github_feed_found"] == "https://github.com/exampleorg/examplerepo/releases.atom"
    assert result["subreddit_feed_found"] == "https://www.reddit.com/r/exampleorg/.rss"

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM official_sources WHERE coingecko_id='example-coin'").fetchall()
    types = {r["source_type"] for r in rows}
    assert {"homepage", "rss_feed", "github", "subreddit"}.issubset(types)
    assert all(r["evidence"] for r in rows)  # every row has provenance


def test_discover_for_sample_never_calls_universe_refresh():
    """Guards against a hidden full-universe refresh being triggered by the
    sample-discovery path."""
    rows = [{"coingecko_id": f"coin-{i}", "website": ""} for i in range(3)]
    with patch.object(sd, "get_json_no_retry", return_value=("ERROR", None)), \
         patch.object(sd, "check_url_reachable", return_value=(404, "")), \
         patch("app.universe.fetch_top_markets") as mock_refresh:
        sd.discover_for_sample(rows, enrichment_time_budget_seconds=5, discovery_time_budget_seconds=5)
    mock_refresh.assert_not_called()


def test_discover_for_sample_stops_at_shared_deadline():
    rows = [{"coingecko_id": f"coin-{i}", "website": ""} for i in range(50)]
    calls = []

    def fake_discover(cid, cached_homepage=""):
        calls.append(cid)
        return {"coingecko_id": cid, "homepage_reachable": False, "blog_rss_found": None,
                "github_feed_found": None, "github_status": "NO_GITHUB_LINK",
                "subreddit_feed_found": None, "announcement_page_found": None, "directly_monitorable": False}

    with patch.object(sd, "enrich_missing_link_metadata", return_value={"elapsed_seconds": 0, "attempted": 0,
                                                                          "newly_ok": 0, "deferred_rate_limited": 0,
                                                                          "errors": 0, "stopped_reason": None,
                                                                          "coins_considered": 50, "already_cached_skipped": 0}), \
         patch.object(sd, "discover_sources_from_cache", side_effect=fake_discover):
        # A deadline already in the past guarantees an immediate stop.
        result = sd.discover_for_sample(rows, deadline=time.monotonic() - 1)

    assert result["stopped_early_due_to_time_budget"] is True
    assert result["completed"] == 0
    assert calls == []


def test_progress_callback_invoked_with_expected_fields():
    rows = [{"coingecko_id": f"coin-{i}", "website": ""} for i in range(4)]
    progress_calls = []

    def fake_discover(cid, cached_homepage=""):
        return {"coingecko_id": cid, "homepage_reachable": False, "blog_rss_found": None,
                "github_feed_found": None, "github_status": "NO_GITHUB_LINK",
                "subreddit_feed_found": None, "announcement_page_found": None, "directly_monitorable": False}

    with patch.object(sd, "enrich_missing_link_metadata", return_value={"elapsed_seconds": 0, "attempted": 0,
                                                                          "newly_ok": 0, "deferred_rate_limited": 0,
                                                                          "errors": 0, "stopped_reason": None,
                                                                          "coins_considered": 4, "already_cached_skipped": 0}), \
         patch.object(sd, "discover_sources_from_cache", side_effect=fake_discover), \
         patch.object(sd.time, "sleep", return_value=None):
        sd.discover_for_sample(rows, progress_every=2, progress_cb=lambda *a: progress_calls.append(a))

    assert len(progress_calls) == 2  # called at coin 2 and coin 4
    processed, total, found, deferred, elapsed, remaining = progress_calls[0]
    assert processed == 2
    assert total == 4


# --- Regression tests for the real bug found on a real Mac run (commit b630ce2) ---
# "Error binding parameter 5: type 'list' is not supported" -- CoinGecko's
# links.official_forum_url is list-typed (always), but the old code did
# `forum_url=links.get("official_forum_url") or None`, passing the raw list
# straight into a sqlite bind parameter.

def test_normalize_link_value_handles_scalar_list_and_empty():
    assert sd.normalize_link_value("https://x.example") == '["https://x.example"]'
    assert sd.normalize_link_value(["https://a", "https://b"]) == '["https://a", "https://b"]'
    assert sd.normalize_link_value(["", None, "https://a"]) == '["https://a"]'
    assert sd.normalize_link_value([]) is None
    assert sd.normalize_link_value("") is None
    assert sd.normalize_link_value(None) is None


def test_parse_link_value_round_trips_and_handles_legacy_scalar_rows():
    assert sd.parse_link_value(sd.normalize_link_value(["https://a", "https://b"])) == ["https://a", "https://b"]
    # A pre-fix row might hold a bare scalar URL (not JSON) -- must not raise.
    assert sd.parse_link_value("https://legacy-scalar.example") == ["https://legacy-scalar.example"]
    assert sd.parse_link_value(None) == []
    assert sd.parse_link_value("") == []


def test_first_link_picks_first_of_several():
    assert sd.first_link(sd.normalize_link_value(["https://a", "https://b"])) == "https://a"
    assert sd.first_link(None) is None


def test_record_link_metadata_accepts_real_coingecko_shaped_list_without_raising():
    """Reproduces the exact real-world shape that crashed Phase B/C:
    official_forum_url as a genuine list, including an empty-string entry
    (CoinGecko often returns these as padded lists of empty strings).
    """
    with get_conn() as conn:
        sd.record_link_metadata(
            conn, "forum-list-coin",
            github_url=["https://github.com/org/repo"],
            subreddit_url="https://www.reddit.com/r/org/",
            announcement_url=["", "https://org.example/announcements"],
            forum_url=["https://forum.example/org", "", ""],  # the exact real-world shape that crashed before
            status="OK",
        )
    with get_conn() as conn:
        row = sd.get_cached_link_metadata(conn, "forum-list-coin")
    assert row is not None
    assert sd.first_link(row["github_url"]) == "https://github.com/org/repo"
    assert sd.first_link(row["announcement_url"]) == "https://org.example/announcements"
    assert sd.parse_link_value(row["forum_url"]) == ["https://forum.example/org"]


def test_enrichment_with_real_coingecko_list_shaped_response_does_not_raise():
    """End-to-end: enrich_missing_link_metadata given a response shaped
    exactly like the real CoinGecko payload that crashed the real run
    (official_forum_url as a list) must complete without raising.
    """
    def fake_get_json(url, params=None):
        return "OK", {
            "links": {
                "repos_url": {"github": ["https://github.com/org/repo"]},
                "subreddit_url": "https://www.reddit.com/r/org/",
                "announcement_url": ["https://org.example/ann"],
                "official_forum_url": ["https://forum.example/1", "", ""],
            }
        }

    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json):
        summary = sd.enrich_missing_link_metadata(["forum-bug-coin"], request_budget=5, time_budget_seconds=10)

    assert summary["errors"] == 0
    assert summary["newly_ok"] == 1
    assert summary["malformed_coin_ids"] == []


def test_one_malformed_coin_does_not_abort_the_100_coin_discovery_run():
    """A real exception (not a mock) raised while processing ONE coin must
    not prevent the other 99 from being processed, and must be recorded,
    not silently swallowed.
    """
    rows = [{"coingecko_id": f"ok-coin-{i}", "website": ""} for i in range(5)]
    rows.insert(2, {"coingecko_id": "bad-coin", "website": ""})  # position 2 will raise

    def flaky_discover(cid, cached_homepage=""):
        if cid == "bad-coin":
            raise ValueError("simulated malformed-data failure for this one coin")
        return {"coingecko_id": cid, "homepage_reachable": False, "blog_rss_found": None,
                "github_feed_found": None, "github_status": "NO_GITHUB_LINK",
                "subreddit_feed_found": None, "announcement_page_found": None, "directly_monitorable": False}

    with patch.object(sd, "enrich_missing_link_metadata", return_value={"elapsed_seconds": 0, "attempted": 0,
                                                                          "newly_ok": 0, "deferred_rate_limited": 0,
                                                                          "errors": 0, "stopped_reason": None,
                                                                          "coins_considered": 6, "already_cached_skipped": 0}), \
         patch.object(sd, "discover_sources_from_cache", side_effect=flaky_discover), \
         patch.object(sd.time, "sleep", return_value=None):
        result = sd.discover_for_sample(rows)

    assert result["completed"] == 6  # all 6 processed, including recovering past the bad one
    assert "bad-coin" in result["malformed_coin_ids"]
    bad_result = next(r for r in result["per_coin"] if r["coingecko_id"] == "bad-coin")
    assert bad_result["error"] is not None
