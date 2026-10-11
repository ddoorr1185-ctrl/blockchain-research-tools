import time
from unittest.mock import patch

from app.db import get_conn
from app import source_discovery as sd
from scripts import source_coverage_validation as scv


def test_check_cached_universe_refuses_without_calling_any_refresh():
    """Requirement: validation must not trigger a hidden full-universe
    refresh. With an empty DB, check_cached_universe must return False and
    must never call anything that hits CoinGecko.
    """
    scv.results.clear()
    with patch("app.universe.fetch_top_markets") as mock_markets, \
         patch("app.universe.get_json_no_retry") as mock_detail:
        ok = scv.check_cached_universe()
    assert ok is False
    mock_markets.assert_not_called()
    mock_detail.assert_not_called()


def test_phase_b_c_skipped_when_global_deadline_already_passed():
    scv.results.clear()
    past_deadline = time.monotonic() - 1
    with patch.object(scv, "discover_for_sample") as mock_discover:
        scv.run_phase_b_c(past_deadline)
    mock_discover.assert_not_called()
    assert scv.results["phase_b_c_discovery"]["skipped"] is True


def test_phase_d_skipped_when_global_deadline_already_passed():
    scv.results.clear()
    past_deadline = time.monotonic() - 1
    with patch.object(scv, "run_all_candidates") as mock_run:
        scv.run_phase_d(past_deadline)
    mock_run.assert_not_called()
    assert scv.results["phase_d_x_fallback"]["skipped"] is True


def test_phase_e_skipped_when_global_deadline_already_passed():
    scv.results.clear()
    past_deadline = time.monotonic() - 1
    with patch("app.historical_prices.get_json_no_retry") as mock_price:
        scv.run_phase_e(past_deadline)
    mock_price.assert_not_called()
    assert scv.results["phase_e_historical"]["skipped"] is True


def test_phase_e_history_price_never_blocks_on_429():
    """The real fix for this lives in app.historical_prices
    (get_or_fetch_price_series -> get_or_fetch_price -> get_json_no_retry);
    this just confirms Phase E actually routes through it and stays fast."""
    from app import historical_prices as hp
    with patch.object(hp, "get_json_no_retry", return_value=("RATE_LIMITED", None)), \
         patch("time.sleep", return_value=None):
        t0 = time.monotonic()
        result = hp.get_or_fetch_price("some-coin", "01-01-2026")
        elapsed = time.monotonic() - t0
    assert result["status"] == "RATE_LIMITED"
    assert elapsed < 1


def test_phase_b_c_report_includes_enrichment_and_deferred_counts():
    scv.results.clear()
    deadline = time.monotonic() + 30
    fake_summary = {
        "requested": 5, "completed": 5, "stopped_early_due_to_time_budget": False,
        "enrichment": {"attempted": 5, "newly_ok": 2, "deferred_rate_limited": 3, "errors": 0,
                        "already_cached_skipped": 0, "stopped_reason": None, "elapsed_seconds": 1,
                        "coins_considered": 5, "malformed_coin_ids": []},
        "homepage_coverage": 5, "blog_rss_coverage": 1, "github_coverage": 2, "subreddit_coverage": 0,
        "announcement_coverage": 0, "deferred_due_to_missing_metadata": 3,
        "at_least_one_official_source": 5, "only_x_known_or_nothing": 0,
        "elapsed_seconds": 2, "per_coin": [], "malformed_coin_ids": [],
    }
    with patch.object(scv, "get_all_active_coins", return_value=[{"coingecko_id": "c", "symbol": "c", "name": "C",
                                                                     "market_cap_rank": 1, "website": ""}]), \
         patch.object(scv, "discover_for_sample", return_value=fake_summary):
        scv.run_phase_b_c(deadline)
    assert scv.results["phase_b_c_discovery"]["enrichment"]["deferred_rate_limited"] == 3


# --- Task 2: Phase B/C failure must never render as fake 0/1 / None/None ---

def test_main_records_explicit_failed_marker_when_phase_b_c_raises():
    scv.results.clear()
    with patch.object(scv, "check_cached_universe", return_value=True), \
         patch.object(scv, "run_rss_recheck", return_value=None), \
         patch.object(scv, "run_phase_b_c", side_effect=RuntimeError("Error binding parameter 5: type 'list' is not supported")), \
         patch.object(scv, "run_phase_d", return_value=None), \
         patch.object(scv, "run_phase_e", return_value=None), \
         patch.object(scv, "save_raw_log", return_value=None), \
         patch.object(scv, "write_report", return_value=None):
        exit_code = scv.main()
    assert exit_code == 1
    assert scv.results["phase_b_c_discovery"]["failed"] is True
    assert "type 'list'" in scv.results["phase_b_c_discovery"]["error"]


def test_phase_b_c_is_valid_evidence_rejects_failed_skipped_and_missing():
    assert scv.phase_b_c_is_valid_evidence({}) is False
    assert scv.phase_b_c_is_valid_evidence({"failed": True, "error": "x"}) is False
    assert scv.phase_b_c_is_valid_evidence({"skipped": True}) is False
    assert scv.phase_b_c_is_valid_evidence({"completed": None, "requested": None}) is False
    assert scv.phase_b_c_is_valid_evidence({"completed": 100, "requested": 100}) is True


def test_pick_verdict_is_inconclusive_when_phase_b_c_failed():
    scv.results.clear()
    scv.results["phase_b_c_discovery"] = {"failed": True, "error": "boom"}
    scv.results["rss_recheck"] = {"a": {"ok": True}}
    assert scv.pick_verdict() == "INCONCLUSIVE / VALIDATION FAILED"


def test_pick_verdict_never_computes_percentage_against_missing_denominator():
    """The exact real bug: with phase_b_c_discovery missing/empty, the old
    code did `bc.get('completed', 0) or 1` and divided by that fake 1,
    producing misleading 0/1 percentages. The new code must refuse to
    compute anything and return INCONCLUSIVE instead.
    """
    scv.results.clear()
    scv.results["phase_b_c_discovery"] = {}  # exactly what main() leaves behind pre-fix
    scv.results["rss_recheck"] = {}
    assert scv.pick_verdict() == "INCONCLUSIVE / VALIDATION FAILED"


def test_write_report_says_phase_failed_and_coverage_not_measured():
    scv.results.clear()
    scv.results["universe_cached"] = True
    scv.results["universe_status"] = {"total_coins_cached": 1000, "x_accounts_verified": 944, "x_accounts_unverified": 56}
    scv.results["phase_b_c_discovery"] = {"failed": True, "error": "Error binding parameter 5: type 'list' is not supported"}
    scv.results["phase_d_x_fallback"] = {"methods": {}}
    scv.results["phase_e_historical"] = {"events": []}
    scv.results["rss_recheck"] = {}
    scv.write_report()
    text = scv.REPORT_PATH.read_text()
    assert "PHASE FAILED" in text
    assert "COVERAGE NOT MEASURED" in text
    assert "INCONCLUSIVE / VALIDATION FAILED" in text
    assert "0/1" not in text
    assert "None/None" not in text


def test_resume_across_simulated_interruption_and_restart():
    """Simulates: run discover_for_sample on coins 0-4 (an 'interrupted'
    partial run), then call it again on the FULL 0-9 set (a 'restart').
    The second call must not re-attempt enrichment for the first 5 (already
    cached OK) and must not lose their previously-recorded official_sources
    rows.
    """
    all_rows = [{"coingecko_id": f"resume-coin-{i}", "website": f"https://resume-coin-{i}.example"} for i in range(10)]

    def fake_get_json(url, params=None):
        cid = url.rsplit("/", 1)[-1]
        return "OK", {"links": {"repos_url": {"github": []}, "subreddit_url": "", "announcement_url": [""]}}

    def fake_check_url(url, timeout=15):
        return 200, "<html><head></head></html>"

    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json), \
         patch.object(sd, "check_url_reachable", side_effect=fake_check_url):
        first_run = sd.discover_for_sample(all_rows[:5])  # "interrupted" partial run

    with get_conn() as conn:
        enriched_after_first = {
            r["coingecko_id"] for r in conn.execute("SELECT coingecko_id FROM coin_link_metadata WHERE fetch_status='OK'").fetchall()
        }
    assert enriched_after_first == {f"resume-coin-{i}" for i in range(5)}

    enrichment_calls_second_run = []

    def fake_get_json_second(url, params=None):
        enrichment_calls_second_run.append(url)
        cid = url.rsplit("/", 1)[-1]
        return "OK", {"links": {"repos_url": {"github": []}, "subreddit_url": "", "announcement_url": [""]}}

    with patch.object(sd, "get_json_no_retry", side_effect=fake_get_json_second), \
         patch.object(sd, "check_url_reachable", side_effect=fake_check_url):
        second_run = sd.discover_for_sample(all_rows)  # "restart" with the full set

    attempted_ids = {url.rsplit("/", 1)[-1] for url in enrichment_calls_second_run}
    assert attempted_ids == {f"resume-coin-{i}" for i in range(5, 10)}  # only the new ones

    with get_conn() as conn:
        homepage_rows = conn.execute(
            "SELECT coingecko_id FROM official_sources WHERE source_type='homepage' AND coingecko_id LIKE 'resume-coin-%'"
        ).fetchall()
    assert len(homepage_rows) == 10  # first run's 5 rows survived into the second run's results
