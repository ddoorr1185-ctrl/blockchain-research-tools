from unittest.mock import patch

import requests

from app.sources.rss_adapter import RSSFeedAdapter
from app.sources.x_adapter import XAccountAdapter, NitterRSSStrategy, SyndicationStrategy


class _FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content
        self.text = content.decode() if isinstance(content, bytes) else content

    def json(self):
        import json
        return json.loads(self.content)


VALID_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item><title>Some headline</title><link>https://example.com/1</link><guid>guid-1</guid>
<description>desc</description><pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_adapter_reports_failure_on_connection_error():
    adapter = RSSFeedAdapter("TestFeed", "https://example.invalid/rss", "rss_news", 0.8)
    with patch("app.sources.rss_adapter._session.get", side_effect=requests.ConnectionError("dns fail")):
        result = adapter.collect()
    assert result.ok is False
    assert result.items == []
    assert "dns fail" in result.error


def test_rss_adapter_reports_failure_on_http_error_not_empty_success():
    adapter = RSSFeedAdapter("TestFeed", "https://example.invalid/rss", "rss_news", 0.8)
    with patch("app.sources.rss_adapter._session.get", return_value=_FakeResponse(503)):
        result = adapter.collect()
    assert result.ok is False
    assert "503" in result.error


def test_rss_adapter_parses_valid_feed():
    adapter = RSSFeedAdapter("TestFeed", "https://example.invalid/rss", "rss_news", 0.8)
    with patch("app.sources.rss_adapter._session.get", return_value=_FakeResponse(200, VALID_RSS)):
        result = adapter.collect()
    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0].external_id == "guid-1"


def test_x_adapter_all_strategies_failing_reports_failure_not_empty():
    adapter = XAccountAdapter("somehandle")
    with patch.object(NitterRSSStrategy, "fetch", return_value=__import__("app.sources.base", fromlist=["CollectionResult"]).CollectionResult(ok=False, items=[], error="nitter down")), \
         patch.object(SyndicationStrategy, "fetch", return_value=__import__("app.sources.base", fromlist=["CollectionResult"]).CollectionResult(ok=False, items=[], error="syndication blocked")):
        result = adapter.collect()
    assert result.ok is False
    assert "nitter down" in result.error
    assert "syndication blocked" in result.error


def test_x_adapter_falls_back_to_second_strategy():
    from app.sources.base import CollectionResult, CollectedItem

    ok_item = CollectedItem(
        source_type="x", source_name="syndication", external_id="t1", author="somehandle",
        coingecko_id=None, published_at="2026-01-01", text="hello", url="https://x.com/somehandle/status/t1",
    )
    with patch.object(NitterRSSStrategy, "fetch", return_value=CollectionResult(ok=False, items=[], error="nitter down")), \
         patch.object(SyndicationStrategy, "fetch", return_value=CollectionResult(ok=True, items=[ok_item])):
        adapter = XAccountAdapter("somehandle")
        result = adapter.collect()
    assert result.ok is True
    assert len(result.items) == 1
