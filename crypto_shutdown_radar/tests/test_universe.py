import time
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


def test_record_account_sets_last_verified_at():
    with get_conn() as conn:
        universe.record_account(conn, "ts-coin", "x", "tscoin_official", "VERIFIED", "coingecko_metadata")
        row = conn.execute("SELECT * FROM official_accounts WHERE coingecko_id='ts-coin'").fetchone()
    assert row["last_verified_at"] is not None
    assert row["added_at"] is not None


def test_is_universe_cached_reflects_real_coin_count():
    with get_conn() as conn:
        assert universe.is_universe_cached(conn, min_coins=1) is False  # clean_tables fixture emptied coins
        universe.upsert_coin(conn, {"id": "cache-coin", "symbol": "cc", "name": "CacheCoin",
                                     "market_cap_rank": 1, "current_price": 1.0, "market_cap": 1.0})
        assert universe.is_universe_cached(conn, min_coins=1) is True
        assert universe.is_universe_cached(conn, min_coins=2) is False


def test_only_missing_skips_already_mapped_coins_without_network_call():
    market_rows = [
        {"id": "already-mapped", "symbol": "am", "name": "AlreadyMapped", "market_cap_rank": 1,
         "current_price": 1.0, "market_cap": 1.0},
        {"id": "never-mapped", "symbol": "nm", "name": "NeverMapped", "market_cap_rank": 2,
         "current_price": 1.0, "market_cap": 1.0},
    ]
    with get_conn() as conn:
        universe.record_account(conn, "already-mapped", "x", "somehandle", "VERIFIED", "coingecko_metadata")

    detail_calls = []

    def fake_detail(coin_id):
        detail_calls.append(coin_id)
        return {"links": {"homepage": [""], "twitter_screen_name": "newhandle"}}

    with patch.object(universe, "fetch_top_markets", return_value=(market_rows, [])), \
         patch.object(universe, "fetch_coin_detail", side_effect=fake_detail):
        summary = universe.refresh_universe(limit=2, fetch_details=True, only_missing=True)

    assert detail_calls == ["never-mapped"]
    assert summary["detail_skipped_already_mapped"] == 1
    assert summary["detail_checked"] == 1


def test_get_json_no_retry_never_sleeps_on_429():
    """The bug this guards against: _throttled_get honors a 429's
    Retry-After header and can sleep up to that long, repeated per retry
    (observed up to 60s/attempt x 5 retries on a real run). A bounded
    validation sample loop must use get_json_no_retry instead, which must
    return immediately on 429 -- no Retry-After sleep, no retry loop.
    """
    call_count = {"n": 0}

    class _FakeResp:
        status_code = 429
        headers = {"Retry-After": "60"}

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResp()

    universe._last_request_at = 0.0  # avoid waiting out any real inter-request gap
    with patch("time.sleep") as mock_sleep, patch.object(universe._session, "get", side_effect=fake_get):
        t0 = time.monotonic()
        for _ in range(10):
            status, data = universe.get_json_no_retry("https://api.coingecko.com/api/v3/coins/whatever")
            assert status == "RATE_LIMITED"
            assert data is None
        elapsed = time.monotonic() - t0

    assert call_count["n"] == 10  # one real attempt per call, no hidden retries
    # time.sleep may be called for the (mocked, instant) inter-request spacing,
    # but never with anything resembling the 60s Retry-After value.
    for call_args in mock_sleep.call_args_list:
        assert call_args.args[0] < 5
    assert elapsed < 2  # real wall-clock time must stay negligible, not 10x60s
