import os
from unittest.mock import patch

import pytest
import requests

from app.sources import x_fallback_experiments as xf


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch.object(xf.time, "sleep", return_value=None):
        yield


class _FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


VALID_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>hi</title><link>https://x.com/h/status/1</link><guid>1</guid><pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_xcancel_reports_real_failure_not_fabricated_success():
    with patch.object(xf._session, "get", side_effect=requests.ConnectionError("blocked")):
        result = xf.test_xcancel(["binance"])
    assert result.success_count == 0
    assert result.total_count == 1
    assert "blocked" in result.account_results["binance"]["error"]
    assert result.requires_auth is False


def test_xcancel_records_success_when_feed_parses():
    with patch.object(xf._session, "get", return_value=_FakeResponse(200, VALID_RSS)):
        result = xf.test_xcancel(["binance"])
    assert result.success_count == 1
    assert result.account_results["binance"]["post_count"] == 1


def test_extra_nitter_mirrors_tests_each_independently():
    with patch.object(xf._session, "get", return_value=_FakeResponse(503)):
        results = xf.test_extra_nitter_mirrors(["binance"])
    assert set(results.keys()) == set(xf.EXTRA_NITTER_MIRRORS)
    for r in results.values():
        assert r.success_count == 0


def test_rsshub_reports_http_error_honestly():
    with patch.object(xf._session, "get", return_value=_FakeResponse(404)):
        result = xf.test_rsshub(["binance"])
    assert result.account_results["binance"]["ok"] is False
    assert "404" in result.account_results["binance"]["error"]


def test_self_hosted_rss_bridge_not_configured_returns_none():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CSR_RSSBRIDGE_BASE_URL", None)
        result = xf.test_self_hosted_rss_bridge(["binance"])
    assert result is None


def test_self_hosted_rss_bridge_tested_when_configured():
    with patch.dict(os.environ, {"CSR_RSSBRIDGE_BASE_URL": "http://localhost:3000"}), \
         patch.object(xf._session, "get", return_value=_FakeResponse(200, VALID_RSS)):
        result = xf.test_self_hosted_rss_bridge(["binance"])
    assert result is not None
    assert result.success_count == 1


def test_run_all_candidates_includes_rss_bridge_only_when_configured():
    with patch.object(xf._session, "get", return_value=_FakeResponse(503)):
        os.environ.pop("CSR_RSSBRIDGE_BASE_URL", None)
        results = xf.run_all_candidates(["binance"])
    assert "self_hosted_rss_bridge" not in results
    assert "xcancel" in results
    assert "rsshub" in results
