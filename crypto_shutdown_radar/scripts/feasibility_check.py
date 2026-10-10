#!/usr/bin/env python3
"""Run this FIRST, from the machine (not a network-restricted sandbox) that
will actually run the radar. It makes real requests and prints what truly
works, so you know the real coverage before trusting the dashboard numbers.

Usage: python -m scripts.feasibility_check
"""
import json
import sys

from app.sources.x_adapter import feasibility_report
from app.sources.rss_adapter import build_news_adapters, build_announcement_adapters
from app.config import NEWS_RSS_SOURCES, ANNOUNCEMENT_RSS_SOURCES

SAMPLE_X_HANDLES = ["binance", "coinbase", "VitalikButerin", "ethereum", "cz_binance"]


def check_rss():
    results = {}
    for adapter in build_news_adapters(NEWS_RSS_SOURCES) + build_announcement_adapters(ANNOUNCEMENT_RSS_SOURCES):
        result = adapter.collect()
        results[adapter.name] = {
            "ok": result.ok,
            "item_count": len(result.items) if result.ok else 0,
            "error": result.error,
        }
    return results


def main():
    print("=== RSS / news / announcement feed feasibility ===")
    rss_results = check_rss()
    print(json.dumps(rss_results, indent=2))

    print("\n=== X/Twitter free-access feasibility ===")
    x_results = feasibility_report(SAMPLE_X_HANDLES)
    print(json.dumps(x_results, indent=2))

    rss_working = sum(1 for r in rss_results.values() if r["ok"])
    x_working = sum(1 for r in x_results["accounts"].values() if r["ok"])
    print("\n=== SUMMARY ===")
    print(f"RSS/news/announcement feeds working: {rss_working}/{len(rss_results)}")
    print(f"X accounts reachable via free methods: {x_working}/{len(x_results['accounts'])}")
    if x_working == 0:
        print(
            "WARNING: no free X access method worked from this network. "
            "The radar will still run correctly on RSS/news/announcement "
            "sources alone, but X coverage will be effectively 0% until a "
            "working free method is found or the account list changes."
        )


if __name__ == "__main__":
    sys.exit(main())
