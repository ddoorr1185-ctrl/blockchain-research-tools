"""EXPERIMENTAL research module -- NOT used by the production pipeline.

The previously-shipped free X methods (app/sources/x_adapter.py: a fixed
Nitter mirror list + the syndication widget endpoint) were measured at
0/20 real accounts on a real network (see LIVE_VALIDATION_REPORT.md from
the prior run). Per instructions, this module does NOT just retry those --
it tests genuinely different candidates:

1. xcancel.com -- a separate Nitter-codebase deployment, legally contested
   (X Corp. sent cease-and-desist letters to both Nitter and xcancel in
   August 2026; both went dark, came back after legal advice, then were
   disrupted again in September 2026 per reporting as of this writing).
   Uses the same /<handle>/rss convention as Nitter.
2. A small set of additional community Nitter-codebase mirrors, distinct
   from the three already tried in x_adapter.py.
3. RSSHub (https://github.com/DIYgod/RSSHub) -- a actively-maintained,
   widely-used open-source RSS aggregator with a public demo instance at
   rsshub.app. It has historically included a Twitter/X route, though
   RSSHub's own docs note X-related routes are frequently the first to
   break because X actively blocks scraping. Tested here for real.
4. Self-hosted RSS-Bridge (https://github.com/RSS-Bridge/rss-bridge) --
   only tested if the operator has deployed their own instance and set
   CSR_RSSBRIDGE_BASE_URL; no public instance is hardcoded here because
   none could be verified as currently reliable, and guessing one would
   violate the "do not fake a solution" requirement.

None of these require authentication, a logged-in account, CAPTCHA
bypass, or credential use. All are free. Their reliability is exactly
what this module measures -- it does not assume any of them work.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import feedparser
import requests

from app.config import HTTP_TIMEOUT_SECONDS, USER_AGENT

log = logging.getLogger("x_fallback_experiments")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

XCANCEL_BASE = "https://xcancel.com"
EXTRA_NITTER_MIRRORS = [
    # Distinct from app/sources/x_adapter.py's NITTER_INSTANCES (nitter.net,
    # nitter.privacydev.net, nitter.poast.org), which already failed 0/20.
    "https://nitter.tiekoetter.com",
    "https://nitter.kavin.rocks",
]
RSSHUB_BASE = "https://rsshub.app"


@dataclass
class MethodResult:
    method: str
    requires_auth: bool
    requires_normal_account: bool
    cost: str
    tos_reliability_note: str
    account_results: dict = field(default_factory=dict)  # handle -> {ok, post_count, newest_ts, error}

    @property
    def success_count(self):
        return sum(1 for r in self.account_results.values() if r["ok"])

    @property
    def total_count(self):
        return len(self.account_results)


def _try_nitter_style_rss(base_url: str, handle: str) -> dict:
    url = f"{base_url}/{handle}/rss"
    try:
        resp = _session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return {"ok": False, "post_count": 0, "newest_ts": None, "error": str(exc), "url": url}
    if resp.status_code != 200:
        return {"ok": False, "post_count": 0, "newest_ts": None, "error": f"HTTP {resp.status_code}", "url": url}
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        return {"ok": False, "post_count": 0, "newest_ts": None, "error": "0 entries / unparseable", "url": url}
    newest = parsed.entries[0].get("published")
    return {"ok": True, "post_count": len(parsed.entries), "newest_ts": newest, "error": None, "url": url}


def _deadline_hit(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() > deadline


def test_xcancel(handles: list, deadline: Optional[float] = None) -> MethodResult:
    result = MethodResult(
        method="xcancel.com (Nitter-codebase mirror)",
        requires_auth=False,
        requires_normal_account=False,
        cost="free",
        tos_reliability_note=(
            "Legally contested: X Corp sent cease-and-desist letters to xcancel and Nitter in "
            "August 2026; service has gone dark and come back multiple times since. Treat as "
            "inherently unstable even if a given test succeeds."
        ),
    )
    for handle in handles:
        if _deadline_hit(deadline):
            break
        result.account_results[handle] = _try_nitter_style_rss(XCANCEL_BASE, handle)
        time.sleep(1.5)
    return result


def test_extra_nitter_mirrors(handles: list, deadline: Optional[float] = None) -> dict:
    """Returns {mirror_base: MethodResult}, one per mirror, since each is a
    separately-operated instance with independent uptime.
    """
    out = {}
    for base in EXTRA_NITTER_MIRRORS:
        if _deadline_hit(deadline):
            break
        result = MethodResult(
            method=f"Nitter-codebase mirror: {base}",
            requires_auth=False,
            requires_normal_account=False,
            cost="free",
            tos_reliability_note="Community-run Nitter instance; same legal/uptime risk as any Nitter mirror.",
        )
        for handle in handles:
            if _deadline_hit(deadline):
                break
            result.account_results[handle] = _try_nitter_style_rss(base, handle)
            time.sleep(1.5)
        out[base] = result
    return out


def test_rsshub(handles: list, deadline: Optional[float] = None) -> MethodResult:
    result = MethodResult(
        method="RSSHub public demo instance (rsshub.app) /twitter/user/:id route",
        requires_auth=False,
        requires_normal_account=False,
        cost="free",
        tos_reliability_note=(
            "RSSHub is an actively-maintained open-source project; the public demo instance is "
            "rate-limited and RSSHub's own documentation flags Twitter/X routes as frequently "
            "broken because X actively blocks scraping. Self-hosting is possible but not required "
            "for this test."
        ),
    )
    for handle in handles:
        if _deadline_hit(deadline):
            break
        url = f"{RSSHUB_BASE}/twitter/user/{handle}"
        try:
            resp = _session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            result.account_results[handle] = {"ok": False, "post_count": 0, "newest_ts": None, "error": str(exc), "url": url}
            time.sleep(1.5)
            continue
        if resp.status_code != 200:
            result.account_results[handle] = {
                "ok": False, "post_count": 0, "newest_ts": None, "error": f"HTTP {resp.status_code}", "url": url
            }
            time.sleep(1.5)
            continue
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            result.account_results[handle] = {
                "ok": False, "post_count": 0, "newest_ts": None, "error": "0 entries / unparseable", "url": url
            }
        else:
            result.account_results[handle] = {
                "ok": True, "post_count": len(parsed.entries),
                "newest_ts": parsed.entries[0].get("published"), "error": None, "url": url,
            }
        time.sleep(1.5)
    return result


def test_self_hosted_rss_bridge(handles: list, deadline: Optional[float] = None) -> Optional[MethodResult]:
    base_url = os.environ.get("CSR_RSSBRIDGE_BASE_URL", "").rstrip("/")
    if not base_url:
        return None  # not configured -- do not fabricate a public instance
    result = MethodResult(
        method=f"Self-hosted RSS-Bridge ({base_url})",
        requires_auth=False,
        requires_normal_account=False,
        cost="free (self-hosted; the operator's own server/bandwidth)",
        tos_reliability_note="Depends entirely on the operator's own deployment staying up to date against X's anti-scraping changes.",
    )
    for handle in handles:
        if _deadline_hit(deadline):
            break
        url = f"{base_url}/?action=display&bridge=Twitter&context=By+username&u={handle}&format=Atom"
        try:
            resp = _session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            result.account_results[handle] = {"ok": False, "post_count": 0, "newest_ts": None, "error": str(exc), "url": url}
            time.sleep(1.5)
            continue
        if resp.status_code != 200:
            result.account_results[handle] = {
                "ok": False, "post_count": 0, "newest_ts": None, "error": f"HTTP {resp.status_code}", "url": url
            }
            time.sleep(1.5)
            continue
        parsed = feedparser.parse(resp.content)
        result.account_results[handle] = {
            "ok": bool(parsed.entries),
            "post_count": len(parsed.entries),
            "newest_ts": parsed.entries[0].get("published") if parsed.entries else None,
            "error": None if parsed.entries else "0 entries / unparseable",
            "url": url,
        }
        time.sleep(1.5)
    return result


def run_all_candidates(handles: list, deadline: Optional[float] = None) -> dict:
    """Runs every candidate method against the same small handle list and
    returns a dict keyed by method name, each value a MethodResult (or None
    for the self-hosted RSS-Bridge candidate if it isn't configured).

    `deadline` (a time.monotonic() value) makes this cooperative with a
    larger time-budgeted run: each method checks it before every per-handle
    (or per-mirror) request and stops immediately once it has passed,
    rather than running every remaining method/account to completion
    regardless of how long that takes.
    """
    out = {"xcancel": test_xcancel(handles, deadline=deadline)}
    out.update({f"nitter_mirror:{k}": v for k, v in test_extra_nitter_mirrors(handles, deadline=deadline).items()})
    out["rsshub"] = test_rsshub(handles, deadline=deadline)
    rss_bridge = test_self_hosted_rss_bridge(handles, deadline=deadline)
    if rss_bridge is not None:
        out["self_hosted_rss_bridge"] = rss_bridge
    return out
