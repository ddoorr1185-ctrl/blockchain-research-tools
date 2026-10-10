#!/usr/bin/env python3
"""Pulls the top-1000 coin universe and attempts official X-account mapping.

Full detail fetch (account mapping) is throttled to ~1 request every 2s per
CoinGecko's free-tier limits, so a full 1000-coin run takes ~35-40 minutes.
Use --details-limit to do a quick partial run.
"""
import argparse

from app.db import init_db
from app.universe import refresh_universe

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000, help="how many top coins to pull")
    parser.add_argument("--no-details", action="store_true", help="skip per-coin account mapping (fast)")
    parser.add_argument("--details-limit", type=int, default=None, help="only map accounts for the first N coins")
    args = parser.parse_args()

    init_db()
    summary = refresh_universe(
        limit=args.limit,
        fetch_details=not args.no_details,
        detail_limit=args.details_limit,
    )
    print(summary)
