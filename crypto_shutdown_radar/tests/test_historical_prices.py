import time
from unittest.mock import patch

from app import historical_prices as hp
from app.db import get_conn


def test_get_or_fetch_price_never_blocks_on_429():
    with patch.object(hp, "get_json_no_retry", return_value=("RATE_LIMITED", None)), \
         patch("time.sleep", return_value=None):
        t0 = time.monotonic()
        result = hp.get_or_fetch_price("rl-coin", "01-01-2026")
        elapsed = time.monotonic() - t0
    assert result["status"] == "RATE_LIMITED"
    assert result["price_usd"] is None
    assert elapsed < 1


def test_successful_lookup_is_never_requeried():
    calls = []

    def fake_get_json(url, params=None):
        calls.append(params["date"])
        return "OK", {"market_data": {"current_price": {"usd": 42.0}}}

    with patch.object(hp, "get_json_no_retry", side_effect=fake_get_json):
        first = hp.get_or_fetch_price("cached-coin", "01-01-2026")
    assert first["status"] == "OK"
    assert first["from_cache"] is False
    assert calls == ["01-01-2026"]

    with patch.object(hp, "get_json_no_retry", side_effect=fake_get_json):
        second = hp.get_or_fetch_price("cached-coin", "01-01-2026")
    assert second["from_cache"] is True
    assert calls == ["01-01-2026"]  # no second network call


def test_rate_limited_point_is_retried_on_a_later_call():
    with patch.object(hp, "get_json_no_retry", return_value=("RATE_LIMITED", None)):
        first = hp.get_or_fetch_price("retry-coin", "02-02-2026")
    assert first["status"] == "RATE_LIMITED"

    with patch.object(hp, "get_json_no_retry", return_value=("OK", {"market_data": {"current_price": {"usd": 7.0}}})):
        second = hp.get_or_fetch_price("retry-coin", "02-02-2026")
    assert second["status"] == "OK"
    assert second["price_usd"] == 7.0


def test_price_series_defers_remaining_points_once_request_budget_exhausted():
    with patch.object(hp, "get_json_no_retry", return_value=("OK", {"market_data": {"current_price": {"usd": 1.0}}})):
        series = hp.get_or_fetch_price_series("budget-coin", ["01-01-2026", "02-01-2026", "03-01-2026"], request_budget=1)
    assert series["01-01-2026"]["status"] == "OK"
    assert series["02-01-2026"]["status"] == "DEFERRED (budget)"
    assert series["03-01-2026"]["status"] == "DEFERRED (budget)"


def test_price_series_respects_shared_deadline():
    with patch.object(hp, "get_json_no_retry", return_value=("OK", {"market_data": {"current_price": {"usd": 1.0}}})):
        series = hp.get_or_fetch_price_series("deadline-coin", ["01-01-2026", "02-01-2026"], deadline=time.monotonic() - 1)
    assert series["01-01-2026"]["status"] == "DEFERRED (budget)"
    assert series["02-01-2026"]["status"] == "DEFERRED (budget)"


def test_price_series_one_failed_point_does_not_block_the_others():
    def fake_get_json(url, params=None):
        if params["date"] == "02-01-2026":
            return "RATE_LIMITED", None
        return "OK", {"market_data": {"current_price": {"usd": 5.0}}}

    with patch.object(hp, "get_json_no_retry", side_effect=fake_get_json):
        series = hp.get_or_fetch_price_series("mixed-coin", ["01-01-2026", "02-01-2026", "03-01-2026"])
    assert series["01-01-2026"]["status"] == "OK"
    assert series["02-01-2026"]["status"] == "RATE_LIMITED"
    assert series["03-01-2026"]["status"] == "OK"


def test_record_and_get_cached_price_round_trip():
    with get_conn() as conn:
        hp.record_price(conn, "round-trip-coin", "05-05-2026", "OK", 123.45)
        row = hp.get_cached_price(conn, "round-trip-coin", "05-05-2026")
    assert row["price_usd"] == 123.45
    assert row["status"] == "OK"
