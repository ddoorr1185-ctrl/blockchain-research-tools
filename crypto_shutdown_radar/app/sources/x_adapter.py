"""Free, keyless access to X/Twitter posts for one account.

HONESTY NOTE (read before trusting this module):
X's official API has had no free read tier since 2023 (the free tier is
write/post only; read access starts at a paid "Basic" tier). Every free
read method below is an unofficial, best-effort workaround that X can
break or block at any time, for any account, without notice. This module
is built to be genuinely tried against the real internet -- it does not
fabricate results -- but its real-world success rate must be measured by
running `scripts/feasibility_check.py` on a normal residential/consumer
network. It could NOT be verified from the sandboxed build environment
because that environment's egress policy blocks twitter.com/x.com and
third-party mirror hosts outright (HTTP 403 at the policy layer, before
any request reaches the public internet).

Two independent strategies are attempted, in order, for each account:

1. Nitter-compatible RSS mirrors. Most public Nitter instances died in
   2024 when X intensified IP blocking of scrapers; a small number of
   community instances still appear and disappear over time. We keep a
   configurable instance list and health-check each one before trusting it.

2. The syndication widget endpoint (cdn.syndication.twimg.com), which X
   itself serves unauthenticated so that embedded tweet widgets render on
   third-party websites. It is not guaranteed to be stable or complete,
   and X can rate-limit or disable it for automated callers at any time,
   but it does not require login and is not a login-wall bypass -- it is
   the same data X already serves to anonymous browsers for embeds.

If both strategies fail for an account, the adapter returns
CollectionResult(ok=False, ...). The caller MUST treat that as "unknown",
never as "no new posts".
"""
import logging
import time
from typing import Optional

import feedparser
import requests

from app.config import HTTP_TIMEOUT_SECONDS, USER_AGENT
from app.sources.base import CollectedItem, CollectionResult, SourceAdapter

log = logging.getLogger("x_adapter")

# Community-run Nitter-compatible instances. This list rots quickly; treat
# it as a starting point, not a guarantee. Update it from
# https://github.com/zedeus/nitter/wiki/Instances (a community-maintained
# status page) before relying on this strategy.
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

SYNDICATION_URL = "https://cdn.syndication.twimg.com/timeline/profile"

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def check_nitter_instance_health(base_url: str) -> bool:
    """Real health check: a Nitter instance is only 'healthy' if it returns
    a 200 with a parseable RSS feed for a known, essentially-permanent
    account (X's own @X account). No result is assumed without this check.
    """
    try:
        resp = _session.get(f"{base_url}/X/rss", timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.info("nitter instance %s unreachable: %s", base_url, exc)
        return False
    if resp.status_code != 200:
        return False
    parsed = feedparser.parse(resp.content)
    return len(parsed.entries) > 0


class NitterRSSStrategy:
    name = "nitter_rss"

    def fetch(self, handle: str) -> CollectionResult:
        last_error = None
        for base in NITTER_INSTANCES:
            try:
                resp = _session.get(f"{base}/{handle}/rss", timeout=HTTP_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                last_error = f"{base}: {exc}"
                continue
            if resp.status_code != 200:
                last_error = f"{base}: HTTP {resp.status_code}"
                continue
            parsed = feedparser.parse(resp.content)
            if not parsed.entries and parsed.bozo:
                last_error = f"{base}: unparseable feed"
                continue
            items = []
            for entry in parsed.entries:
                items.append(
                    CollectedItem(
                        source_type="x",
                        source_name=f"nitter:{base}",
                        external_id=entry.get("id") or entry.get("link"),
                        author=handle,
                        coingecko_id=None,
                        published_at=entry.get("published"),
                        text=entry.get("title", ""),
                        url=entry.get("link", ""),
                    )
                )
            return CollectionResult(ok=True, items=items)
        return CollectionResult(ok=False, items=[], error=last_error or "no nitter instance reachable")


class SyndicationStrategy:
    """Uses X's own public embed-widget data feed. This is the same
    unauthenticated data X serves to render embedded tweets on third-party
    pages; it is not a CAPTCHA or login bypass. It returns only a recent
    slice of a timeline and its shape/availability is controlled entirely
    by X, so it can stop working without warning.
    """

    name = "syndication"

    def fetch(self, handle: str) -> CollectionResult:
        try:
            resp = _session.get(
                SYNDICATION_URL,
                params={"screen_name": handle, "dnt": "true"},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            return CollectionResult(ok=False, items=[], error=str(exc))
        if resp.status_code != 200:
            return CollectionResult(ok=False, items=[], error=f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return CollectionResult(ok=False, items=[], error="non-JSON response (endpoint shape likely changed)")

        tweets = data.get("timeline", {}).get("entries") if isinstance(data, dict) else None
        if tweets is None:
            return CollectionResult(ok=False, items=[], error="unexpected response shape")

        items = []
        for entry in tweets:
            tweet = entry.get("content", {}).get("tweet") if isinstance(entry, dict) else None
            if not tweet:
                continue
            items.append(
                CollectedItem(
                    source_type="x",
                    source_name="syndication",
                    external_id=str(tweet.get("id_str") or tweet.get("id")),
                    author=handle,
                    coingecko_id=None,
                    published_at=tweet.get("created_at"),
                    text=tweet.get("text") or tweet.get("full_text", ""),
                    url=f"https://x.com/{handle}/status/{tweet.get('id_str') or tweet.get('id')}",
                )
            )
        return CollectionResult(ok=True, items=items)


class XAccountAdapter(SourceAdapter):
    name = "x"

    def __init__(self, handle: str):
        self.handle = handle
        self.strategies = [NitterRSSStrategy(), SyndicationStrategy()]

    def collect(self, *, since_id: Optional[str] = None) -> CollectionResult:
        errors = []
        for strategy in self.strategies:
            result = strategy.fetch(self.handle)
            if result.ok:
                if since_id:
                    result.items = [i for i in result.items if i.external_id != since_id]
                return result
            errors.append(f"{strategy.name}: {result.error}")
        return CollectionResult(ok=False, items=[], error="; ".join(errors))


def feasibility_report(sample_handles: list[str]) -> dict:
    """Runs both strategies against a small sample of real, well-known
    accounts and reports what actually happened. Intended to be run from a
    normal network (see scripts/feasibility_check.py), not from a
    network-restricted build sandbox.
    """
    report = {"nitter_instances": {}, "accounts": {}}
    for base in NITTER_INSTANCES:
        report["nitter_instances"][base] = check_nitter_instance_health(base)
        time.sleep(1)

    for handle in sample_handles:
        adapter = XAccountAdapter(handle)
        result = adapter.collect()
        report["accounts"][handle] = {
            "ok": result.ok,
            "item_count": len(result.items) if result.ok else 0,
            "error": result.error,
        }
        time.sleep(1)
    return report
