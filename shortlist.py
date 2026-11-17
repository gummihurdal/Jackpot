#!/usr/bin/env python3
"""Rebuild only the Top 20 list from the last scan (about a minute).

Use it after editing data/research.json, for example after adding a research veto:
    python shortlist.py
"""
import json, os

from radar import config as C
from radar.dossier import TRADABLE, build_shortlist
from radar.history import download_weekly
from radar.pipeline import cached_fetch, rates_all
from radar.store import Store
from radar.universe import fx_rates
from radar.util import clean, log


def main():
    path = os.path.join(C.DATA, "latest.json")
    d = json.load(open(path))
    stocks = d["stocks"]
    # only names that could plausibly triple need their full price history again
    maybe = [m for m in stocks if m["region"] in TRADABLE]
    hist = download_weekly([m["symbol"] for m in maybe])
    store = Store()
    news = store.news_for([m["symbol"] for m in maybe])
    rates = rates_all(fx_rates({cur for _, cur in C.REGIONS.values()}))
    top, near = build_shortlist(stocks, hist, news, rates, store, cached_fetch)
    d["top"], d["near"] = top, near
    short = {e["symbol"] for e in top}
    tr = d.get("tracker") or {}
    by = {m["symbol"]: m for m in stocks}
    for s, t in tr.items():
        t["short_list"] = s in short
    for s in short:
        if s not in tr:
            m = by[s]
            tr[s] = {"name": m["name"], "theme": m["theme"], "country": m["country"], "currency": m["currency"],
                     "first_seen": d["meta"]["date"], "first_price": m["price"], "first_spring": m["spring"],
                     "low_at_flag": m["low"], "high_at_flag": m["high"], "rules": C.RULES_VERSION,
                     "last_price": m["price"], "last_date": d["meta"]["date"], "return": 0.0,
                     "on_radar": True, "spring_now": m["spring"], "short_list": True}
        if not tr[s].get("short_since"):
            tr[s]["short_since"] = d["meta"]["date"]
    d["tracker"] = tr
    json.dump(clean(d), open(path, "w"), separators=(",", ":"))
    json.dump(clean(tr), open(os.path.join(C.DATA, "tracker.json"), "w"), indent=1)
    log(f"Top list: {len(top)} names, {len(near)} near misses")
    for e in top:
        p = e["profile"]
        log(f"  {e['tier']} {e['jackpot']:5.1f} {e['symbol']:<10} {e['name'][:30]:<30} {e['theme'][:15]:<15} "
            f"+{p['potential']:.0%} {'NEW ' if e['newcomer'] else ''}{e['earnings']['state']}")


if __name__ == "__main__":
    main()
