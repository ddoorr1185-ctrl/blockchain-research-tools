import time
from unittest.mock import patch

from app.db import get_conn
from app.universe import upsert_coin
from app.sample_selection import get_or_build_validation_sample
from app.source_discovery import record_source
from app.shutdown_source_audit import is_disqualified_url
from app.shutdown_source_gold_audit import (
    is_navigation_like, extract_html_article_cards, extract_jsonld_articles,
    extract_rss_entries, extract_embedded_json_entries, gold_classify_page,
    gold_audit_candidate_source, new_entry_detectable,
)
from scripts import shutdown_source_gold_audit as sga

GENUINE_CARDS_HTML = """
<html><body>
<a href="/blog/post-one-launch-update">Post One: Launch Update</a>
<small>Jan 5, 2026</small>
<a href="/blog/post-two-roadmap-review">Post Two: Roadmap Review</a>
<small>Feb 10, 2026</small>
</body></html>
"""

NAV_ONLY_HTML = """
<html><body>
<a href="/blog/education">Education</a>
<a href="/blog/developers">Developers</a>
<a href="/blog/ecosystem">Ecosystem</a>
</body></html>
"""

TEMPLATE_PLACEHOLDER_HTML = """
<html><body>
<a href="/news/item1">{{{ data._highlightResult.title.value }}}</a>
<a href="/news/item2">{{{ data._highlightResult.subtitle.value }}}</a>
</body></html>
"""


# --- Task 5: remove the Bittensor upgrade -- /docs/query can never qualify ---

def test_docs_query_endpoint_cannot_qualify():
    assert is_disqualified_url("https://www.bittensor.com/docs/query") is not None


def test_docs_query_cannot_pass_gold_audit():
    def fake_check(url, timeout=15):
        return 200, GENUINE_CARDS_HTML  # even with "good-looking" content, the URL itself is hard-blocked

    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check), \
         patch("app.shutdown_source_audit.check_url_reachable", side_effect=fake_check):
        result = gold_audit_candidate_source("bittensor", "https://www.bittensor.com/docs/query")
    assert result["verdict"] != "GOLD_PASS"


# --- Task 10: docs pages / API references cannot qualify ---

def test_docs_and_api_pages_cannot_qualify():
    assert is_disqualified_url("https://example.com/docs") is not None
    assert is_disqualified_url("https://example.com/api/reference") is not None
    assert is_disqualified_url("https://example.com/api") is not None


# --- Task 10: category-only / navigation-only anchors cannot qualify ---

def test_navigation_and_category_titles_rejected():
    for bad_title in ["Education", "Developers", "Ecosystem", "Past Events", "Gold articles",
                       "Silver articles", "In The News", "Tokenization", "Interoperability",
                       "contact our PR agency", "Read More", "View All"]:
        assert is_navigation_like(bad_title), f"expected {bad_title!r} to be rejected as navigation-like"

    entries = extract_html_article_cards(NAV_ONLY_HTML, "https://example.com/blog")
    assert len(entries) == 0

    page = gold_classify_page("https://example.com/blog", NAV_ONLY_HTML)
    assert page["is_stream"] is False


# --- Task 10: template placeholders cannot qualify ---

def test_template_placeholders_rejected():
    assert is_navigation_like("{{{ data._highlightResult.title.value }}}")
    entries = extract_html_article_cards(TEMPLATE_PLACEHOLDER_HTML, "https://example.com/news")
    assert len(entries) == 0


# --- Task 10: identical / non-unique entry URLs cannot qualify ---

def test_non_unique_entry_urls_cannot_detect_new_entries():
    dup_entries = [{"title": "Post A Launch Update", "url": "https://example.com/blog/a", "date": "Jan 1, 2026"},
                   {"title": "Post B Launch Update", "url": "https://example.com/blog/a", "date": "Jan 2, 2026"}]
    assert new_entry_detectable(dup_entries, "HTML_ARTICLE_CARDS") is False


# --- Task 10: "Read More" duplicates without article identity ---

def test_read_more_duplicates_rejected():
    html = """
    <html><body>
    <a href="/blog/post-a">Read More</a>
    <a href="/blog/post-b">Read More</a>
    </body></html>
    """
    entries = extract_html_article_cards(html, "https://example.com/blog")
    assert len(entries) == 0


# --- Task 10 positive: two real article cards with unique URLs ---

def test_two_real_article_cards_qualify():
    entries = extract_html_article_cards(GENUINE_CARDS_HTML, "https://example.com/blog")
    assert len(entries) == 2
    page = gold_classify_page("https://example.com/blog", GENUINE_CARDS_HTML)
    assert page["is_stream"] is True
    assert page["mechanism"] == "HTML_ARTICLE_CARDS"
    assert page["confidence"] == "HIGH"  # both entries carry dates


def test_html_cards_with_no_dates_go_to_low_confidence():
    html = """
    <html><body>
    <a href="/blog/post-one-launch-update">Post One Launch Update</a>
    <a href="/blog/post-two-roadmap-review">Post Two Roadmap Review</a>
    </body></html>
    """
    page = gold_classify_page("https://example.com/blog", html)
    assert page["is_stream"] is True
    assert page["confidence"] == "LOW"  # slug-only, no dates -- Task 8: must not auto-pass

    def fake_check(url, timeout=15):
        return 200, html

    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check):
        result = gold_audit_candidate_source("low-conf-coin", "https://example.com/blog")
    assert result["verdict"] == "GOLD_UNRESOLVED"


# --- Task 10 positive: JSON-LD Article/NewsArticle ---

def test_jsonld_article_entries_qualify():
    html = """
    <html><body>
    <script type="application/ld+json">
    {"@type": "NewsArticle", "headline": "Company Announces Q1 Results", "url": "https://example.com/news/q1-results", "datePublished": "2026-01-05"}
    </script>
    <script type="application/ld+json">
    {"@type": "Article", "headline": "New Partnership Announced", "url": "https://example.com/news/partnership", "datePublished": "2026-02-10"}
    </script>
    </body></html>
    """
    entries = extract_jsonld_articles(html, "https://example.com/news")
    assert len(entries) == 2
    page = gold_classify_page("https://example.com/news", html)
    assert page["mechanism"] == "JSON-LD"
    assert page["confidence"] == "HIGH"


# --- Task 10 positive: RSS/Atom items ---

def test_rss_entries_qualify():
    feed_xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
    <item><title>Post One</title><link>https://example.com/blog/post-one</link><pubDate>Mon, 05 Jan 2026 00:00:00 GMT</pubDate></item>
    <item><title>Post Two</title><link>https://example.com/blog/post-two</link><pubDate>Tue, 10 Feb 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""

    def fake_check(url, timeout=15):
        return 200, feed_xml

    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check):
        entries = extract_rss_entries("https://example.com/feed.xml", "https://example.com/blog")
    assert len(entries) == 2


# --- Task 10 positive: embedded JSON article listings ---

def test_embedded_json_entries_qualify():
    html = """
    <html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {"posts": [
        {"title": "Launch Update One", "slug": "launch-update-one", "date": "2026-01-05"},
        {"title": "Roadmap Review Two", "slug": "launch-update-two", "date": "2026-02-10"}
    ]}}}
    </script>
    </body></html>
    """
    entries = extract_embedded_json_entries(html, "https://example.com/blog")
    assert len(entries) == 2
    page = gold_classify_page("https://example.com/blog", html)
    assert page["mechanism"] == "EMBEDDED_JSON"


# --- Task 10: one broken site cannot abort the run ---

def test_one_broken_candidate_does_not_raise():
    def fake_check(url, timeout=15):
        raise RuntimeError("simulated network failure")

    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check):
        result = gold_audit_candidate_source("broken-coin", "https://broken.example/blog")
    assert result["verdict"] == "GOLD_UNRESOLVED"


def test_gold_pass_one_broken_project_does_not_abort_batch():
    with get_conn() as conn:
        for i in range(1, 4):
            upsert_coin(conn, {"id": f"gold-coin-{i}", "symbol": f"g{i}", "name": f"Gold{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
            conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?", (f"https://gold{i}.example", f"gold-coin-{i}"))
            record_source(conn, f"gold-coin-{i}", "first_party_page", f"https://gold{i}.example/announcements",
                          "VERIFIED", "test", monitorable=True, http_status=200,
                          discovery_method="matched /announcements", shutdown_relevance="HIGH")
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins"
        ).fetchall()]
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="gold-test-v1")
        conn.execute(
            "INSERT INTO project_audit_classification (coingecko_id, raw_classification, audited_classification, resolved, computed_at) "
            "VALUES ('gold-coin-1', 'A', 'A', 1, ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ"),)
        )
        conn.execute(
            "INSERT INTO project_audit_classification (coingecko_id, raw_classification, audited_classification, resolved, computed_at) "
            "VALUES ('gold-coin-2', 'A', 'A', 1, ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ"),)
        )
        conn.execute(
            "INSERT INTO shutdown_source_audit (coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance, audited_at) "
            "VALUES ('gold-coin-1', 'https://gold1.example/announcements', 'https://gold1.example/announcements', 'PASS', 'HIGH', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.execute(
            "INSERT INTO shutdown_source_audit (coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance, audited_at) "
            "VALUES ('gold-coin-2', 'https://gold2.example/announcements', 'https://gold2.example/announcements', 'PASS', 'HIGH', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )

    raw = sga.load_raw_classification(sample)
    strict = sga.load_strict_classification()

    def fake_check(url, timeout=15):
        if "gold2" in url:
            raise RuntimeError("simulated network failure for this one project")
        return 200, GENUINE_CARDS_HTML

    sga.results.clear()
    sga.results.update({"started_at": "t"})
    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check):
        sga.run_gold_pass(sample, raw, strict, time.monotonic() + 30)

    gold_results = sga.results["gold_results"]
    assert "gold-coin-1" in gold_results  # unaffected project still processed
    # The broken project is recorded as unresolved or errored, never silently dropped or crashing the batch.
    assert ("gold-coin-2" in gold_results) or ("gold-coin-2" in sga.results["gold_unresolved"])


# --- Task 10: no 60-second sleeps ---

def test_gold_pass_has_no_long_sleeps():
    with get_conn() as conn:
        upsert_coin(conn, {"id": "nosleep-gold", "symbol": "ns", "name": "NoSleepGold",
                            "market_cap_rank": 1, "current_price": 1.0, "market_cap": 1.0})
        conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?", ("https://nosleep-gold.example", "nosleep-gold"))
        record_source(conn, "nosleep-gold", "first_party_page", "https://nosleep-gold.example/announcements",
                      "VERIFIED", "test", monitorable=True, http_status=200,
                      discovery_method="matched /announcements", shutdown_relevance="HIGH")
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins"
        ).fetchall()]
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="gold-test-v2")
        conn.execute(
            "INSERT INTO project_audit_classification (coingecko_id, raw_classification, audited_classification, resolved, computed_at) "
            "VALUES ('nosleep-gold', 'A', 'A', 1, ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ"),)
        )
        conn.execute(
            "INSERT INTO shutdown_source_audit (coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance, audited_at) "
            "VALUES ('nosleep-gold', 'https://nosleep-gold.example/announcements', 'https://nosleep-gold.example/announcements', 'PASS', 'HIGH', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )

    raw = sga.load_raw_classification(sample)
    strict = sga.load_strict_classification()

    sga.results.clear()
    sga.results.update({"started_at": "t"})
    with patch("app.shutdown_source_gold_audit.check_url_reachable", return_value=(503, "")), \
         patch("time.sleep") as mock_sleep:
        t0 = time.monotonic()
        sga.run_gold_pass(sample, raw, strict, time.monotonic() + 30)
        elapsed = time.monotonic() - t0
    for call_args in mock_sleep.call_args_list:
        assert call_args.args[0] < 5
    assert elapsed < 5


# --- Task 10: existing DB/sample untouched ---

def test_check_cached_universe_and_sample_never_rebuilds():
    with get_conn() as conn:
        for i in range(1, 901):
            upsert_coin(conn, {"id": f"sga-coin-{i}", "symbol": f"z{i}", "name": f"Z{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins ORDER BY market_cap_rank"
        ).fetchall()]
        get_or_build_validation_sample(conn, all_coins, sample_version=sga.SAMPLE_VERSION)

    with patch("app.universe.fetch_top_markets") as mock_markets:
        ok, sample = sga.check_cached_universe_and_sample()
    assert ok is True
    assert len(sample) == 100
    mock_markets.assert_not_called()


def test_unclear_entity_relationship_blocks_gold_ab():
    with get_conn() as conn:
        upsert_coin(conn, {"id": "unclear-entity-coin", "symbol": "xyz", "name": "Totally Unrelated",
                            "market_cap_rank": 1, "current_price": 1.0, "market_cap": 1.0})
        conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?",
                     ("https://wholly-different-domain.example", "unclear-entity-coin"))
        record_source(conn, "unclear-entity-coin", "first_party_page",
                      "https://wholly-different-domain.example/announcements", "VERIFIED", "test",
                      monitorable=True, http_status=200, discovery_method="matched /announcements",
                      shutdown_relevance="HIGH")
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins"
        ).fetchall()]
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="gold-test-v3")
        conn.execute(
            "INSERT INTO project_audit_classification (coingecko_id, raw_classification, audited_classification, resolved, computed_at) "
            "VALUES ('unclear-entity-coin', 'A', 'A', 1, ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ"),)
        )
        conn.execute(
            "INSERT INTO shutdown_source_audit (coingecko_id, source_url, canonical_stream_url, verdict, audited_relevance, audited_at) "
            "VALUES ('unclear-entity-coin', 'https://wholly-different-domain.example/announcements', "
            "'https://wholly-different-domain.example/announcements', 'PASS', 'HIGH', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )

    raw = sga.load_raw_classification(sample)
    strict = sga.load_strict_classification()

    def fake_check(url, timeout=15):
        return 200, GENUINE_CARDS_HTML

    sga.results.clear()
    sga.results.update({"started_at": "t"})
    with patch("app.shutdown_source_gold_audit.check_url_reachable", side_effect=fake_check):
        sga.run_gold_pass(sample, raw, strict, time.monotonic() + 30)

    gold = sga.results["gold_results"]["unclear-entity-coin"]
    assert gold["entity_relationship"] == "UNCLEAR"
    assert gold["gold_classification"] not in ("A", "B")
