"""Free RSS-based collection: news aggregators and exchange announcement
feeds. These are the backbone of this system's real-world coverage, since
free X access (see x_adapter.py) is unreliable by construction.
"""
import logging
from typing import Optional

import feedparser
import requests

from app.config import HTTP_TIMEOUT_SECONDS, USER_AGENT
from app.sources.base import CollectedItem, CollectionResult, SourceAdapter

log = logging.getLogger("rss_adapter")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


class RSSFeedAdapter(SourceAdapter):
    """Generic RSS/Atom adapter. `source_type` distinguishes general news
    coverage ('rss_news') from primary-source operator announcements
    ('rss_announcement'), since the classifier and severity logic treat
    primary sources as higher credibility.
    """

    def __init__(self, name: str, url: str, source_type: str, credibility: float):
        self.name = name
        self.url = url
        self.source_type = source_type
        self.credibility = credibility

    def collect(self, *, since_id: Optional[str] = None) -> CollectionResult:
        try:
            resp = _session.get(self.url, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            return CollectionResult(ok=False, items=[], error=str(exc))
        if resp.status_code != 200:
            return CollectionResult(ok=False, items=[], error=f"HTTP {resp.status_code}")

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            return CollectionResult(ok=False, items=[], error=f"feed parse error: {parsed.bozo_exception}")

        items = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("guid") or entry.get("link")
            if since_id and guid == since_id:
                break
            summary = entry.get("summary", "") or entry.get("description", "")
            text = f"{entry.get('title', '')}\n{summary}".strip()
            items.append(
                CollectedItem(
                    source_type=self.source_type,
                    source_name=self.name,
                    external_id=guid,
                    author=self.name,
                    coingecko_id=None,
                    published_at=entry.get("published", entry.get("updated")),
                    text=text,
                    url=entry.get("link", ""),
                )
            )
        return CollectionResult(ok=True, items=items)


def build_news_adapters(news_sources):
    return [RSSFeedAdapter(s["name"], s["url"], "rss_news", s["credibility"]) for s in news_sources]


def build_announcement_adapters(announcement_sources):
    return [RSSFeedAdapter(s["name"], s["url"], "rss_announcement", s["credibility"]) for s in announcement_sources]
