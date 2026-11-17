#!/usr/bin/env python3
"""Light job between scans: pull the market-wide feeds and file anything about radar stocks."""
import json, os

from radar import config as C
from radar.pipeline import match_feeds
from radar.sources import fetch_feeds
from radar.store import Store
from radar.util import log


def main():
    path = os.path.join(C.DATA, "latest.json")
    stocks = json.load(open(path))["stocks"] if os.path.exists(path) else []
    rows = match_feeds(fetch_feeds(), stocks)
    total = Store().save_news(rows) if rows else 0
    hits = sum(1 for r in rows if r["symbol"] != "*")
    log(f"{len(rows)} feed items, {hits} about radar stocks, {total} headlines in the store")


if __name__ == "__main__":
    main()
