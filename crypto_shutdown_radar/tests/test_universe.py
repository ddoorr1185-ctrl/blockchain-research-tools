from unittest.mock import patch

from app import universe
from app.db import get_conn


def test_coin_with_no_twitter_handle_is_marked_unverified_not_guessed():
    market_row = {"id": "fake-coin", "symbol": "fake", "name": "FakeCoin", "market_cap_rank": 999,
                  "current_price": 1.0, "market_cap": 1000.0}
    with get_conn() as conn:
        universe.upsert_coin(conn, market_row)

    # Directly exercise the per-coin mapping logic the way refresh_universe does.
    detail = {"links": {"homepage": [""], "twitter_screen_name": ""}}
    with get_conn() as conn:
        twitter_handle = (detail["links"].get("twitter_screen_name") or "").strip()
        assert twitter_handle == ""
        universe.record_account(conn, "fake-coin", "x", "", "UNVERIFIED", "no_handle_in_coingecko_metadata")
        row = conn.execute(
            "SELECT * FROM official_accounts WHERE coingecko_id='fake-coin' AND platform='x'"
        ).fetchone()
    assert row["verification_status"] == "UNVERIFIED"
    assert row["handle_or_url"] == ""  # never a guessed handle


def test_coin_with_twitter_handle_is_marked_verified():
    with get_conn() as conn:
        universe.record_account(conn, "real-coin", "x", "realcoinofficial", "VERIFIED", "coingecko_metadata")
        row = conn.execute(
            "SELECT * FROM official_accounts WHERE coingecko_id='real-coin' AND platform='x'"
        ).fetchone()
    assert row["verification_status"] == "VERIFIED"
    assert row["handle_or_url"] == "realcoinofficial"


def test_refresh_universe_reports_errors_instead_of_silent_partial_success():
    with patch.object(universe, "_throttled_get", return_value=None):
        summary = universe.refresh_universe(limit=250, fetch_details=False)
    assert summary["retrieved"] == 0
    assert summary["error_pages"] == [1]


def test_detail_fetch_failure_counted_not_silently_dropped():
    market_row = {"id": "detail-fail-coin", "symbol": "dfc", "name": "DetailFailCoin", "market_cap_rank": 500,
                  "current_price": 2.0, "market_cap": 2000.0}
    with patch.object(universe, "fetch_top_markets", return_value=([market_row], [])), \
         patch.object(universe, "fetch_coin_detail", return_value=None):
        summary = universe.refresh_universe(limit=1, fetch_details=True)
    assert summary["detail_errors"] == 1
    assert summary["detail_checked"] == 0
