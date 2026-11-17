"""Watchlist: names you add by hand in data/watchlist.json.

They are put through the same checks as the radar, but are always shown, with a plain verdict,
even when they are too large, not near their low, or fail the safety gate.
"""
import json, os

import yfinance as yf

from . import config as C
from .gates import cash_gate, filing_gate, news_gate, share_gate
from .history import download_weekly, rebound_dna, stabilisation
from .scores import potential_score, quality_score, safety_score, setup_score, spring_score
from .sources import (INFO_KEYS, classify, fetch_filings, fetch_share_growth, fetch_trials, fetch_yahoo_news,
                      is_relevant)
from .universe import basic_metrics, theme_of
from .util import log, retry

SUFFIX_REGION = {"MI": "it", "HE": "fi", "L": "gb", "OL": "no", "ST": "se", "CO": "dk", "DE": "de", "PA": "fr",
                 "SW": "ch", "AS": "nl", "BR": "be", "MC": "es", "LS": "pt", "VI": "at", "IR": "ie", "WA": "pl",
                 "AT": "gr", "TA": "il", "T": "jp", "HK": "hk", "TW": "tw", "KS": "kr", "KQ": "kr", "NS": "in",
                 "SI": "sg", "AX": "au", "NZ": "nz", "JK": "id", "BK": "th", "KL": "my", "SR": "sa", "JO": "za",
                 "SA": "br", "MX": "mx", "SN": "cl", "TO": "ca"}


def load_watchlist():
    path = os.path.join(C.DATA, "watchlist.json")
    return json.load(open(path)) if os.path.exists(path) else []


def scan_watchlist(rates, tmap):
    items = load_watchlist()
    if not items:
        return []
    hist = download_weekly([w["symbol"] for w in items])
    out = []
    for w in items:
        sym = w["symbol"]
        try:
            raw = retry(lambda: yf.Ticker(sym).info, tries=3, wait=1.5) or {}
        except Exception:
            raw = {}
        raw["symbol"] = sym
        raw["_region"] = SUFFIX_REGION.get(sym.rsplit(".", 1)[-1], "us") if "." in sym else "us"
        m = basic_metrics(raw, rates)
        if not m:
            out.append({"symbol": sym, "name": w.get("name") or sym, "note": w.get("note"),
                        "verdict": "No market data found for this symbol", "ok": False, "flags": []})
            continue
        info = {k: raw.get(k) for k in INFO_KEYS if raw.get(k) is not None}
        m["sector"], m["industry"] = info.get("sector"), (info.get("industry") or "").replace("—", " - ")
        m["theme"] = theme_of(m["sector"], m["industry"])
        m["note"] = w.get("note")
        fails, flags = [], []

        ok, parts, notes, reason = cash_gate(m, info)
        if not ok:
            fails.append(reason)
        burning = "runway_years" in m
        g = fetch_share_growth(sym)
        m["share_growth_3y"] = g
        ok, sc, note, reason = share_gate(g)
        if not ok:
            fails.append(reason)
        else:
            parts["shares"] = sc
            if note:
                notes.append(note)
        if m["region"] == "us" and sym.upper() in tmap:
            ok, sc, n2, f2, reason = filing_gate(fetch_filings(tmap[sym.upper()]), burning)
            if not ok:
                fails.append(reason)
            elif sc is not None:
                parts["filings"] = sc
            notes += n2
            flags += f2
        heads = [dict(h, tags=classify(h["title"])) for h in fetch_yahoo_news(sym)
                 if is_relevant(h["title"], m["name"], sym)]
        ok, f3, adj, reason = news_gate(heads, burning)
        if not ok:
            fails.append(reason)
        flags += f3

        closes = hist.get(sym) or []
        dna = rebound_dna(closes) if len(closes) >= 104 else None
        stab = stabilisation(closes) if closes else None
        q, s, safety = quality_score(m, info), setup_score(m, dna, stab), safety_score(parts)
        structural = bool(dna and dna["open"] and dna["open_weeks"] > 156)
        pot = tr = None
        if m["theme"] == C.PHARMA_THEME:
            tr = fetch_trials(m["name"])
            pot = potential_score(m, info, tr)
            m["potential"], m["trials"] = pot, tr

        fits = []
        if not (C.MIN_CAP_USD <= m["cap_usd"] <= C.MAX_CAP_USD):
            fits.append("outside the low and mid cap range" if m["cap_usd"] > C.MAX_CAP_USD else "below the size floor")
        if not (m["ratio"] >= C.MIN_RATIO and m["pos"] <= C.MAX_POS):
            fits.append(f"not in the washed-out zone (at {m['pos']:.0%} of its 52-week range)")
        if fails:
            verdict = "Fails the safety gate: " + "; ".join(fails)
        elif fits:
            verdict = "Passes the safety gate, but is " + " and ".join(fits)
        else:
            verdict = "Passes the safety gate and fits the radar pattern"
        m.update({
            "rev_growth": info.get("revenueGrowth"), "target": info.get("targetMeanPrice"),
            "dna": dna, "stab": stab, "safety_notes": notes, "flags": flags,
            "quality": q, "setup": s, "safety": safety if not fails else min(safety, 30),
            "spring": spring_score(q, safety, s, pot, adj, structural),
            "verdict": verdict, "ok": not fails, "fits": not fails and not fits,
            "news": [{k: h[k] for k in ("ts", "title", "url", "publisher", "tags")} for h in heads[:4]],
            "spark": [round(v, 4) for v in closes[-104::2]],
            "next_earnings": None,
        })
        m["size"] = "Large cap" if m["cap_usd"] > C.MAX_CAP_USD else m["size"]
        out.append(m)
        log(f"  watch {sym}: {verdict}")
    return out
