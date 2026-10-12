import time
from unittest.mock import patch

from app.db import get_conn
from app.universe import upsert_coin, record_account
from app.sample_selection import get_or_build_validation_sample
from scripts import shutdown_source_coverage as ssc


def _seed_small_universe(n=10):
    with get_conn() as conn:
        for i in range(1, n + 1):
            upsert_coin(conn, {"id": f"ssc-coin-{i}", "symbol": f"s{i}", "name": f"Proj{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
            conn.execute("UPDATE coins SET website=? WHERE coingecko_id=?", (f"https://proj{i}.example", f"ssc-coin-{i}"))
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins ORDER BY market_cap_rank"
        ).fetchall()]
    return all_coins


def test_check_cached_universe_and_sample_refuses_without_universe():
    ok, sample = ssc.check_cached_universe_and_sample()
    assert ok is False
    assert sample == []


def test_check_cached_universe_and_sample_refuses_without_persisted_sample():
    with get_conn() as conn:
        for i in range(1, 901):
            upsert_coin(conn, {"id": f"nosamp-coin-{i}", "symbol": f"n{i}", "name": f"N{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
    # No validation_sample rows exist yet for SAMPLE_VERSION.
    ok, sample = ssc.check_cached_universe_and_sample()
    assert ok is False
    assert sample == []


def test_check_cached_universe_and_sample_never_calls_fetch_top_markets():
    """Task 1: must never trigger a hidden universe rebuild."""
    with get_conn() as conn:
        for i in range(1, 901):
            upsert_coin(conn, {"id": f"norefetch-coin-{i}", "symbol": f"r{i}", "name": f"R{i}",
                                "market_cap_rank": i, "current_price": 1.0, "market_cap": 1.0})
        all_coins = [dict(r) for r in conn.execute(
            "SELECT coingecko_id, symbol, name, market_cap_rank, website FROM coins ORDER BY market_cap_rank"
        ).fetchall()]
        get_or_build_validation_sample(conn, all_coins, sample_version=ssc.SAMPLE_VERSION)

    with patch("app.universe.fetch_top_markets") as mock_markets, \
         patch("app.universe.get_json_no_retry") as mock_detail:
        ok, sample = ssc.check_cached_universe_and_sample()
    assert ok is True
    assert len(sample) == 100
    mock_markets.assert_not_called()
    mock_detail.assert_not_called()


def test_run_shutdown_discovery_one_broken_project_does_not_abort_batch():
    all_coins = _seed_small_universe(5)
    with get_conn() as conn:
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="ssc-test-v1")

    def fake_check(url, timeout=15):
        if "proj3" in url:
            raise RuntimeError("simulated network failure for this one project")
        return 200, "<html><body><a href='/blog'>Blog</a></body></html>"

    ssc.results.clear()
    with patch.object(ssc, "check_url_reachable", side_effect=fake_check), \
         patch.object(ssc, "get_cached_link_metadata", return_value=None), \
         patch.object(ssc.time, "sleep", return_value=None):
        ssc.run_shutdown_discovery(sample, time.monotonic() + 60)

    per_project = ssc.results["per_project"]
    assert len(per_project) == 5  # all 5 recorded, including the broken one
    broken_id = next(s["coingecko_id"] for s in sample if "proj3" in s["website"])
    assert per_project[broken_id]["classification"] == "D"


def test_run_shutdown_discovery_resumes_using_cached_pages_without_reprobing():
    all_coins = _seed_small_universe(3)
    with get_conn() as conn:
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="ssc-test-v2")

    probe_calls = []

    def counting_check(url, timeout=15):
        probe_calls.append(url)
        return 200, "<html><body><a href='/news'>News</a></body></html>"

    ssc.results.clear()
    with patch.object(ssc, "check_url_reachable", side_effect=counting_check), \
         patch.object(ssc, "get_cached_link_metadata", return_value=None), \
         patch.object(ssc.time, "sleep", return_value=None):
        ssc.run_shutdown_discovery(sample, time.monotonic() + 60)
    first_call_count = len(probe_calls)
    assert first_call_count > 0

    # "Restart": run again with the same sample; cached official_sources
    # rows must be reused, so no new homepage/candidate probes happen.
    probe_calls.clear()
    ssc.results.clear()
    with patch.object(ssc, "check_url_reachable", side_effect=counting_check), \
         patch.object(ssc, "get_cached_link_metadata", return_value=None), \
         patch.object(ssc.time, "sleep", return_value=None):
        ssc.run_shutdown_discovery(sample, time.monotonic() + 60)
    assert probe_calls == []  # fully resumed from cache, zero new network calls
    assert len(ssc.results["per_project"]) == 3


def test_run_shutdown_discovery_no_60_second_sleeps():
    all_coins = _seed_small_universe(5)
    with get_conn() as conn:
        sample = get_or_build_validation_sample(conn, all_coins, sample_version="ssc-test-v3")

    ssc.results.clear()
    with patch.object(ssc, "check_url_reachable", return_value=(503, "")), \
         patch.object(ssc, "get_cached_link_metadata", return_value=None), \
         patch("time.sleep") as mock_sleep:
        t0 = time.monotonic()
        ssc.run_shutdown_discovery(sample, time.monotonic() + 60)
        elapsed = time.monotonic() - t0
    for call_args in mock_sleep.call_args_list:
        assert call_args.args[0] < 5
    assert elapsed < 5
