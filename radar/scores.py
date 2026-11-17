"""Scores from 0 to 100. Higher is better."""
import math
from datetime import date

from .util import grade


def quality_score(m, info):
    fin = info.get("sector") in ("Financial Services", "Real Estate")
    parts = {}
    parts["earns"] = None if m["eps_ttm"] is None else (1.0 if m["eps_ttm"] > 0 else 0.0)
    pf = m["pe_fwd"]
    if m["eps_fwd"] is not None and m["eps_fwd"] <= 0:
        parts["fwd"] = 0.0
    elif pf is not None and pf > 0:
        parts["fwd"] = 1.0 if pf <= 15 else 0.7 if pf <= 25 else 0.4 if pf <= 40 else 0.15
    else:
        parts["fwd"] = None
    parts["roe"] = grade(info.get("returnOnEquity"), [(0.15, 1.0), (0.08, 0.6), (0.0001, 0.3)])
    parts["margin"] = grade(info.get("profitMargins"), [(0.15, 1.0), (0.07, 0.6), (0.0001, 0.3)])
    parts["gross"] = grade(info.get("grossMargins"), [(0.5, 1.0), (0.3, 0.7), (0.15, 0.4)])
    parts["growth"] = grade(info.get("revenueGrowth"), [(0.15, 1.0), (0.05, 0.7), (0.0, 0.4)])
    if not fin:
        fcf = info.get("freeCashflow")
        parts["fcf"] = None if fcf is None else (1.0 if fcf > 0 else 0.0)
    n = info.get("numberOfAnalystOpinions") or 0
    rec = info.get("recommendationMean")
    if n >= 3 and rec:
        parts["analysts"] = 1.0 if rec <= 2.0 else 0.7 if rec <= 2.5 else 0.4 if rec <= 3.0 else 0.1
    tgt = info.get("targetMeanPrice")
    if n >= 3 and tgt and m["price"]:
        up = tgt / m["price"] - 1
        parts["upside"] = 1.0 if up >= 0.40 else 0.7 if up >= 0.20 else 0.4 if up >= 0 else 0.0
        m["upside"] = up
    vals = [v for v in parts.values() if v is not None]
    cover = min(1.0, len(vals) / 9.0 + 0.3)   # thin data is pulled toward the middle
    raw = sum(vals) / len(vals) if vals else 0.5
    return round(100 * (raw * cover + 0.5 * (1 - cover)))


def setup_score(m, dna, stab):
    width = min(1.0, math.log(max(m["ratio"], 1.0001)) / math.log(4.0))
    near = max(0.0, 1 - m["pos"] / 0.40)
    if dna is None:
        d = 0.4
    elif dna["open"] and dna["open_weeks"] > 156:
        d = 0.0
    elif dna["recovered"] >= 2:
        d = 1.0
    elif dna["recovered"] == 1:
        d = 0.7
    else:
        d = 0.4
    s = {"turning": 1.0, "basing": 0.6, "falling": 0.2}.get((stab or {}).get("state"), 0.5)
    return round(100 * (0.30 * width + 0.25 * near + 0.25 * d + 0.20 * s))


def months_until(ym):
    y, mth = int(ym[:4]), int(ym[5:7])
    t = date.today()
    return (y - t.year) * 12 + (mth - t.month)


def potential_score(m, info, trials):
    """Pharma only: how much could go right."""
    parts = {}
    if trials:
        p3, p2 = trials.get("p3", 0), trials.get("p2", 0)
        parts["pipeline"] = 1.0 if p3 >= 3 else 0.85 if p3 == 2 else 0.65 if p3 == 1 else 0.4 if p2 >= 2 else 0.2
        nxt = trials.get("next_p3")
        if nxt:
            mo = months_until(nxt["date"])
            parts["catalyst"] = 1.0 if mo <= 12 else 0.6 if mo <= 24 else 0.3
    rev, gm = info.get("totalRevenue"), info.get("grossMargins")
    parts["commercial"] = (1.0 if gm and gm >= 0.6 else 0.7) if rev and rev > 5e7 else 0.3
    parts["growth"] = grade(info.get("revenueGrowth"), [(0.25, 1.0), (0.10, 0.75), (0.0, 0.45)])
    n = info.get("numberOfAnalystOpinions") or 0
    if n >= 3:
        rec = info.get("recommendationMean")
        if rec:
            parts["analysts"] = 1.0 if rec <= 1.8 else 0.75 if rec <= 2.3 else 0.4 if rec <= 3.0 else 0.1
        if m.get("upside") is not None:
            u = m["upside"]
            parts["upside"] = 1.0 if u >= 0.6 else 0.75 if u >= 0.3 else 0.45 if u >= 0 else 0.0
    vals = [v for v in parts.values() if v is not None]
    return round(100 * sum(vals) / len(vals)) if vals else 50


def safety_score(parts):
    vals = [v for v in parts.values() if v is not None]
    return round(100 * sum(vals) / len(vals)) if vals else 50


def spring_score(q, safety, setup, potential=None, news_adj=0, structural=False):
    if potential is None:
        s = 0.35 * q + 0.35 * safety + 0.30 * setup
    else:
        s = 0.20 * q + 0.30 * safety + 0.20 * setup + 0.30 * potential
    s += news_adj
    if structural:
        s -= 12
    return max(0, min(100, round(s)))
