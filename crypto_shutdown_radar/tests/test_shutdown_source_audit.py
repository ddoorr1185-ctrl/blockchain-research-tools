import time
from unittest.mock import patch

from app.db import get_conn
from app.universe import upsert_coin
from app.sample_selection import get_or_build_validation_sample
from app.source_discovery import record_source
from app.shutdown_source_audit import (
    is_disqualified_url, extract_listing_entries, classify_page, parent_path_candidate,
    audit_candidate_source, entity_relationship, wilson_confidence_interval,
    deeper_discovery_for_d_project,
)
from scripts import shutdown_source_audit as ssa

LISTING_HTML = """
<html><body>
<a href="/blog/post-one-launch-update">Post One: Launch Update</a>
<small>Jan 5, 2026</small>
<a href="/blog/post-two-roadmap-review">Post Two: Roadmap Review</a>
<small>Feb 10, 2026</small>
</body></html>
"""

ARTICLE_HTML = "<html><body><h1>Just one article, no other entries here at all.</h1></body></html>"


# --- Task 12.1: individual article cannot qualify as a stream ---

def test_individual_article_cannot_qualify():
    page = classify_page("https://example.com/blog/introducing-ccip-2-0", ARTICLE_HTML)
    assert page["page_type"] != "LISTING"


# --- Task 12.2: legal page cannot qualify ---

def test_legal_page_cannot_qualify():
    reason = is_disqualified_url("https://example.com/legal")
    assert reason is not None
    reason2 = is_disqualified_url("https://example.com/legal/general/api-terms/")
    assert reason2 is not None


# --- Task 12.3: signup/newsletter page cannot qualify ---

def test_signup_and_newsletter_pages_cannot_qualify():
    assert is_disqualified_url("https://example.com/newsletter") is not None
    assert is_disqualified_url("https://example.com/signup") is not None


# --- Task 12.4: anchor URL cannot qualify ---

def test_anchor_url_cannot_qualify():
    reason = is_disqualified_url("https://example.com/community#join")
    assert reason is not None
    assert "anchor" in reason.lower()


# --- Task 12.5: homepage alone cannot qualify ---

def test_homepage_alone_cannot_qualify():
    assert is_disqualified_url("https://example.com") is not None
    assert is_disqualified_url("https://example.com/") is not None


# --- Task 12.6: stream needs >=2 identifiable entries ---

def test_stream_needs_at_least_two_entries():
    one_entry_html = "<html><body><a href='/blog/only-post-here-now'>Only Post Here Now</a></body></html>"
    entries = extract_listing_entries(one_entry_html, "https://example.com/blog")
    assert len(entries) < 2
    page = classify_page("https://example.com/blog", one_entry_html)
    assert page["page_type"] != "LISTING"

    entries_two = extract_listing_entries(LISTING_HTML, "https://example.com/blog")
    assert len(entries_two) >= 2
    page_two = classify_page("https://example.com/blog", LISTING_HTML)
    assert page_two["page_type"] == "LISTING"


# --- Task 12.7: parent listing is preferred over article ---

def test_parent_listing_preferred_over_article():
    assert parent_path_candidate("https://chain.link/blog/introducing-ccip-2-0") == "https://chain.link/blog"

    def fake_check(url, timeout=15):
        if url == "https://chain.link/blog/introducing-ccip-2-0":
            return 200, ARTICLE_HTML
        if url == "https://chain.link/blog":
            return 200, LISTING_HTML
        return 404, ""

    with patch("app.shutdown_source_audit.check_url_reachable", side_effect=fake_check):
        result = audit_candidate_source("chainlink", "https://chain.link/blog/introducing-ccip-2-0", "PLAUSIBLE")
    assert result["verdict"] == "PASS"
    assert result["canonical_stream_url"] == "https://chain.link/blog"


# --- Task 12.8: one broken site cannot abort the audit ---

def test_one_broken_site_cannot_abort_audit():
    def fake_check(url, timeout=15):
        raise RuntimeError("simulated network failure")

    with patch("app.shutdown_source_audit.check_url_reachable", side_effect=fake_check):
        result = audit_candidate_source("broken-coin", "https://broken.example/blog", "PLAUSIBLE")
    assert result["verdict"] == "UNRESOLVED"  # recorded, not raised


def test_deeper_discovery_one_broken_candidate_does_not_raise():
    def fake_check(url, timeout=15):
        raise RuntimeError("simulated failure")

    with patch("app.shutdown_source_audit.check_url_reachable", side_effect=fake_check):
        result = deeper_discovery_for_d_project("broken-coin", "https://broken.example")
    assert result["upgraded"] is False
    assert result["reason"]  # explains why, never raises


# --- Task 12.9: entity relationship UNCLEAR does not count ---

def test_entity_relationship_unclear_blocks_credit():
    _, status = entity_relationship("totally-unrelated-id", "xyz", "Totally Unrelated Project", "https://unrelated-domain.example")
    assert status == "UNCLEAR"

    _, status_known = entity_relationship("xrp", "xrp", "XRP", "https://ripple.com")
    assert status_known == "VERIFIED"

    def fake_check(url, timeout=15):
        return 200, LISTING_HTML

    with patch("app.shutdown_source_audit.check_url_reachable", side_effect=fake_check):
        result = audit_candidate_source("totally-unrelated-id", "https://unrelated-domain.example/blog", "UNCLEAR")
    # The page itself still passes the stream test, but callers (the harness)
    # must gate on entity_relationship != UNCLEAR before counting it toward A/B.
    assert result["verdict"] == "PASS"


# --- Task 12.10: A/B count uses audited classification, not raw ---

def _seed_project_with_raw_ab(coingecko_id, homepage, html_for_url, raw_relevance="HIGH"):
    with get_conn() as conn:
        upsert_coin(conn, {"id": coingecko_id, "symbol": coingecko_id[:4], "name": coingecko_id,
                            "market_cap_rank": 1, "current_price": 1.0, "market_cap": 1.0})
        conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?", (homepage, coingecko_id))
        record_source(
            conn, coingecko_id, "first_party_page", f"{homepage}/legal/announcements",
            "VERIFIED", "discovered via homepage: href path matches /legal", monitorable=True,
            http_status=200, discovery_method="matched /legal", shutdown_relevance=raw_relevance,
        )
        sample = get_or_build_validation_sample(
            conn, [dict(r) for r in conn.execute(
                "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins"
            ).fetchall()],
            sample_version="ssa-test-v1",
        )
    return sample


def test_audited_classification_overrides_raw_false_positive():
    sample = _seed_project_with_raw_ab("fp-coin", "https://fp.example", None)
    raw = ssa.load_raw_classification(sample)
    assert raw["fp-coin"]["classification"] == "A"  # raw says A (from a /legal page -- a known false-positive shape)

    ssa.results.clear()
    ssa.results.update({"started_at": "t"})
    with patch("app.shutdown_source_audit.check_url_reachable", return_value=(200, "<html><body>legal text only</body></html>")):
        ssa.run_audit_pass(sample, raw, time.monotonic() + 30)

    audited = ssa.results["audited_ab"]["fp-coin"]
    assert audited["raw_classification"] == "A"
    assert audited["audited_classification"] != "A"  # the /legal page is disqualified, so it downgrades


# --- Task 12.11: no 60-second sleeps ---

def test_audit_pass_has_no_long_sleeps():
    sample = _seed_project_with_raw_ab("nosleep-coin", "https://nosleep.example", None)
    raw = ssa.load_raw_classification(sample)
    ssa.results.clear()
    ssa.results.update({"started_at": "t"})
    with patch("app.shutdown_source_audit.check_url_reachable", return_value=(503, "")), \
         patch("time.sleep") as mock_sleep:
        t0 = time.monotonic()
        ssa.run_audit_pass(sample, raw, time.monotonic() + 30)
        elapsed = time.monotonic() - t0
    for call_args in mock_sleep.call_args_list:
        assert call_args.args[0] < 5
    assert elapsed < 5


# --- Task 12.12: existing DB/sample preserved ---

def test_check_cached_universe_and_sample_never_rebuilds():
    with get_conn() as conn:
        for i in range(1, 901):
            upsert_coin(conn, {"id": f"ssa-coin-{i}", "symbol": f"a{i}", "name": f"A{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins ORDER BY market_cap_rank"
        ).fetchall()]
        get_or_build_validation_sample(conn, all_coins, sample_version=ssa.SAMPLE_VERSION)

    with patch("app.universe.fetch_top_markets") as mock_markets:
        ok, sample = ssa.check_cached_universe_and_sample()
    assert ok is True
    assert len(sample) == 100
    mock_markets.assert_not_called()


def test_wilson_confidence_interval_bounds():
    low, high = wilson_confidence_interval(51, 100)
    assert 0.0 <= low < 0.51 < high <= 1.0
    low0, high0 = wilson_confidence_interval(0, 0)
    assert low0 == 0.0 and high0 == 0.0
