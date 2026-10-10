"""Common interfaces for all collection sources, so a source implementation
(X, RSS, exchange announcements, ...) can be swapped without touching the
rest of the system.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class CollectedItem:
    source_type: str          # 'x' | 'rss_news' | 'rss_announcement'
    source_name: str          # adapter/account/feed name
    external_id: str          # stable id for de-dup (post id / guid / url hash)
    author: str
    coingecko_id: Optional[str]
    published_at: Optional[str]   # ISO8601 if known
    text: str
    url: str


@dataclass
class CollectionResult:
    """What an adapter actually achieved on one run -- used so the rest of
    the system never has to guess whether "no items" meant "nothing new"
    or "the request failed".
    """
    ok: bool
    items: list
    error: Optional[str] = None


class SourceAdapter:
    """Base class. A concrete adapter implements `collect()` for one target
    (one X account, one RSS feed, ...) and must never silently convert a
    failed fetch into an empty-but-successful result.
    """

    name: str = "base"

    def collect(self, *, since_id: Optional[str] = None) -> CollectionResult:
        raise NotImplementedError
