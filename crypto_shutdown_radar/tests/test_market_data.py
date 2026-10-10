from unittest.mock import patch

import requests

from app import market_data


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = str(json_data)

    def json(self):
        return self._json


def test_price_snapshot_unknown_on_network_failure():
    with patch.object(market_data._session, "get", side_effect=requests.ConnectionError("no network")):
        snap = market_data.get_price_snapshot("some-coin")
    assert snap["price_usd"] == "UNKNOWN"
    assert snap["change_1h_pct"] == "UNKNOWN"
    assert snap["retrieved_at_unknown"] is True


def test_price_snapshot_unknown_when_coin_missing_from_response():
    with patch.object(market_data._session, "get", return_value=_FakeResponse(200, [])):
        snap = market_data.get_price_snapshot("not-a-real-coin")
    assert snap["price_usd"] == "UNKNOWN"


def test_futures_availability_unknown_when_exchange_unreachable():
    market_data._futures_cache.clear()
    with patch.object(market_data._session, "get", side_effect=requests.ConnectionError("down")):
        availability = market_data.get_futures_availability("BTC")
    assert all(v == "UNKNOWN" for v in availability.values())


def test_futures_availability_never_assumes_available_from_spot_listing():
    """A coin can be spot-listed without having a perpetual market; the
    adapter must only say 'Available' if the symbol is actually present in
    the exchange's own futures instrument list.
    """
    market_data._futures_cache.clear()
    fake_binance = _FakeResponse(
        200,
        {"symbols": [{"baseAsset": "BTC", "contractType": "PERPETUAL", "status": "TRADING"}]},
    )

    def fake_get(url, params=None, timeout=None):
        if "binance" in url:
            return fake_binance
        return _FakeResponse(500)

    with patch.object(market_data._session, "get", side_effect=fake_get):
        availability = market_data.get_futures_availability("SOMEOBSCURECOIN")
    assert availability["binance"] == "Unavailable"
