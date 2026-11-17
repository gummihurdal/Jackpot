"""The short list: at most 20 stocks, each with its numbers crunched.

A stock gets on the list only if all of this holds:
  - it trades on a market a European private investor can normally reach
  - it has at least 3 years of price history, or it is a newcomer (listed 9 months to 3 years) that is
    profitable and whose old level was backed by its current earnings; at most 5 newcomers are listed
  - it held a level at least 3 times today's price for 13 weeks or more in the last 3 years
    (that is the "+200%" test: the distance back to a level it really traded at, not to a one-day peak)
  - that old level was backed by real earnings (not a hype price)
  - profit is not collapsing right now
  - it passed the whole safety gate and carries no capital-raise, legal or filing warning

Everything else that came close is kept as a near miss, with the reason.
"""
import statistics, time
from datetime import date, datetime, timedelta, timezone

import yfinance as yf

from . import config as C
from .universe import fx_rates
from .util import log, retry

TRADABLE = {"us", "ca", "gb", "de", "fr", "ch", "nl", "be", "se", "no", "dk", "fi", "it", "es", "pt", "at",
            "ie", "pl", "il", "jp", "hk", "sg", "au", "mx"}
MIN_POTENTIAL = 2.0        # +200%: the bar for the main list
MIN_POTENTIAL_B = 1.5      # +150%: "next best" names that fill the list up to 20
MIN_WEEKS = 156            # 3 years of history; younger listings are judged as newcomers
MIN_WEEKS_NEWCOMER = 40    # a newcomer still needs most of a year on the market
MAX_NEWCOMERS = 5
SUSTAIN_WEEKS = 13         # the old level must have held this long
MAX_PE_AT_HIGH = 40.0      # above this the old level was hype, not earnings
FAIR_MULTIPLE = 15.0
TOP_N, MAX_PER_THEME = 20, 4

DEEP_KEYS = ("financialCurrency", "earningsQuarterlyGrowth", "revenueGrowth", "heldPercentInsiders",
             "heldPercentInstitutions", "shortPercentOfFloat", "floatShares", "sharesOutstanding", "payoutRatio",
             "priceToBook", "fullTimeEmployees", "country", "freeCashflow", "totalCash", "totalDebt",
             "firstTradeDateMilliseconds")


# ---------------------------------------------------------------- price history

def price_profile(closes, price):
    """What the last years of weekly prices say: the level it held, the spike test, the normal level."""
    n = len(closes)
    if n < 30 or not price:
        return None
    last = closes[-156:]
    ranked = sorted(last, reverse=True)
    sustained = ranked[min(SUSTAIN_WEEKS - 1, len(ranked) - 1)]
    peak = ranked[0]
    normal = statistics.median(closes[-260:-52]) if n >= 156 else None
    week_moves = [closes[i] / closes[i - 1] - 1 for i in range(max(1, n - 52), n)]
    return {
        "weeks": n,
        "sustained_high": sustained,
        "potential": sustained / price - 1,
        "peak": peak,
        "spike": peak > sustained * 1.6,          # the very top was brief
        "weeks_near_level": sum(1 for c in last if c >= sustained * 0.8),
        "normal": normal,
        "vs_normal": (price / normal - 1) if normal else None,
        "worst_week": min(week_moves) if week_moves else None,
    }


# ---------------------------------------------------------------- deeper company data

def _rows(df, names):
    out = []
    try:
        for col in list(df.columns)[:6]:
            r = {"d": str(col)[:10]}
            for key, label in names.items():
                v = df.loc[label, col] if label in df.index else None
                r[key] = float(v) if v is not None and v == v else None
            out.append(r)
    except Exception:
        pass
    return out


def fetch_deep(sym):
    """Annual and quarterly results, dividend payments, ownership and officers for one company."""
    t = yf.Ticker(sym)
    try:
        info = retry(lambda: t.info, tries=3, wait=2.0) or {}
    except Exception:
        info = {}
    names = {"rev": "Total Revenue", "ni": "Net Income"}
    try:
        annual = _rows(t.income_stmt, names)
    except Exception:
        annual = []
    try:
        quarterly = _rows(t.quarterly_income_stmt, names)
    except Exception:
        quarterly = []
    divs = []
    try:
        d = t.dividends
        cut = datetime.now(timezone.utc) - timedelta(days=365 * 6)
        divs = [[str(k)[:10], float(v)] for k, v in d.items() if k.tz_convert("UTC") >= cut]
    except Exception:
        pass
    officers = [{"name": o.get("name"), "title": o.get("title"), "age": o.get("age")}
                for o in (info.get("companyOfficers") or [])[:4]]
    if not info and not annual:
        return None
    return {"info": {k: info.get(k) for k in DEEP_KEYS if info.get(k) is not None},
            "summary": (info.get("longBusinessSummary") or "")[:420],
            "annual": annual, "quarterly": quarterly, "divs": divs, "officers": officers}


# ---------------------------------------------------------------- profiles

def earnings_profile(m, deep, prof, rates):
    info = deep["info"]
    fx = rates.get(info.get("financialCurrency") or "USD")
    annual = [a for a in deep["annual"] if a.get("ni") is not None]
    q = [x["ni"] for x in deep["quarterly"] if x.get("ni") is not None]
    yoy = info.get("earningsQuarterlyGrowth")
    if yoy is None and len(q) >= 5 and q[4] and q[4] > 0:
        yoy = q[0] / q[4] - 1
    rising = len(q) >= 3 and q[0] > q[1] > q[2]
    loss = (m.get("eps_ttm") is not None and m["eps_ttm"] <= 0)
    if loss:
        state = "loss-making"
    elif yoy is not None and yoy <= -0.5 and not rising:
        state = "collapsing"
    elif rising and (yoy is None or yoy > -0.5):
        state = "recovering"
    elif yoy is not None and yoy <= -0.2:
        state = "falling"
    elif yoy is not None and yoy >= 0.1:
        state = "growing"
    else:
        state = "stable"
    out = {"state": state, "yoy": yoy, "quarters": q[:5],
           "years": [{"y": a["d"][:4], "rev": a.get("rev"), "ni": a["ni"]} for a in annual[:4]],
           "currency": info.get("financialCurrency")}
    if annual and fx and m.get("cap_usd"):
        nis = [a["ni"] for a in annual[:4]]
        peak, avg3 = max(nis), sum(nis[:3]) / len(nis[:3])
        peak_usd = peak * fx
        # a young, fast-growing company can earn more now than in any full year on file
        if m.get("eps_ttm") and m["eps_ttm"] > 0 and m.get("price"):
            ttm_usd = m["cap_usd"] * m["eps_ttm"] / m["price"]
            out["ttm_ni_usd"] = ttm_usd
            peak_usd = max(peak_usd, ttm_usd)
        out["peak_ni"] = peak_usd
        if peak_usd > 0:
            out["pe_at_high"] = m["cap_usd"] * (1 + prof["potential"]) / peak_usd
        if avg3 > 0:
            out["pe_normal"] = m["cap_usd"] / (avg3 * fx)
            out["earnings_upside"] = (avg3 * fx * FAIR_MULTIPLE) / m["cap_usd"] - 1
        out["years_profitable"] = sum(1 for v in nis if v > 0)
    return out


def dividend_profile(m, deep):
    divs = deep["divs"]
    today = date.today()
    def total(a, b):
        lo, hi = (today - timedelta(days=a)).isoformat(), (today - timedelta(days=b)).isoformat()
        return sum(v for d, v in divs if lo < d <= hi)
    ttm, prev = total(365, 0), total(730, 365)
    years = sorted({d[:4] for d, _ in divs if d[:4] >= str(today.year - 4)})
    if not ttm and not prev:
        trend = "none"
    elif not ttm:
        trend = "suspended"
    elif prev and ttm < prev * 0.7:
        trend = "cut"
    elif prev and ttm > prev * 1.1:
        trend = "raised"
    else:
        trend = "steady" if prev else "new"
    return {"ttm": ttm, "yield_ttm": (ttm / m["price"]) if m.get("price") else None, "prev": prev,
            "trend": trend, "years_paid_5": len(years), "last": divs[-1] if divs else None,
            "payments_12m": sum(1 for d, _ in divs if d > (today - timedelta(days=365)).isoformat())}


def management_profile(m, deep, heads):
    info = deep["info"]
    ceo = next((o for o in deep["officers"] if o.get("title") and
                ("ceo" in o["title"].lower() or "chief executive" in o["title"].lower())), None)
    cut = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [h for h in heads if h["ts"] >= cut]
    lawyers = sum(1 for h in recent if "lawyer_notice" in h.get("tags", []))
    ins = info.get("heldPercentInsiders")
    g = m.get("share_growth_3y")
    return {"ceo": ceo, "insiders": ins, "institutions": info.get("heldPercentInstitutions"),
            "controlled": bool(ins and ins > 0.5), "short_float": info.get("shortPercentOfFloat"),
            "buying_back": bool(g is not None and g < -0.02), "share_growth_3y": g,
            "class_action": lawyers >= 2,
            "ceo_change": any("ceo_change" in h.get("tags", []) for h in recent)}


# ---------------------------------------------------------------- the list

def _shrinking(earn):
    """True when sales are lower than two years earlier (or unknown): a business in decline, not a cycle."""
    revs = [y.get("rev") for y in earn.get("years", [])]
    if len(revs) < 3 or revs[0] is None or revs[2] is None:
        return True
    return revs[0] < revs[2]


def _exclusion(m, prof, earn, mgmt, note):
    pharma = m["theme"] == C.PHARMA_THEME
    newcomer = prof["weeks"] < MIN_WEEKS
    if note and note.get("exclude"):
        return "Research: " + note["exclude"]
    if prof["weeks"] < MIN_WEEKS_NEWCOMER:
        return "Listed less than 9 months, too little history to judge"
    if newcomer and earn["state"] in ("loss-making", "falling"):
        return "Newcomer without rising or stable profit"
    if mgmt["class_action"]:
        return "Securities class action pending"
    bad = [f for f in m.get("flags", []) if f.startswith(("Capital raise", "Legal", "Prospectus", "Unregistered",
                                                          "Shelf", "Trial setback", "Below its old peak"))]
    if newcomer or not _shrinking(earn):
        # still below a peak from years ago, but sales have kept growing: a de-rating, not a dying business
        bad = [f for f in bad if not f.startswith("Below its old peak")]
    bad = ["Sales shrinking and below its old peak for 3+ years" if f.startswith("Below its old peak") else f
           for f in bad]
    if bad:
        return bad[0]
    if earn["state"] == "collapsing":
        return f"Profit is collapsing (latest quarter {earn['yoy']:.0%} against a year ago)"
    nis = [y["ni"] for y in earn.get("years", [])]
    if len(nis) >= 3 and nis[0] < nis[1] < nis[2] and not pharma:
        return "Annual profit has fallen two years running"
    if earn["state"] == "loss-making" and not pharma:
        return "Loss-making over the past twelve months"
    if not pharma:
        if earn.get("peak_ni") is not None and earn["peak_ni"] <= 0:
            return "Never earned a profit at the old price level"
        pe = earn.get("pe_at_high")
        if pe is None:
            return "No earnings history to check the old price level against"
        if pe > MAX_PE_AT_HIGH:
            return f"The old level was hype: {pe:.0f} times its best year's profit"
    elif (m.get("potential") or 0) < 60:
        return "Pharma pipeline too thin to support a recovery case"
    return None


def _risks(m, prof, earn, div, mgmt):
    r = []
    if (m.get("stab") or {}).get("state") == "falling":
        r.append("Still falling: new low within the last week")
    if earn["state"] == "falling":
        r.append(f"Profit down {-earn['yoy']:.0%} against a year ago")
    if prof.get("worst_week") is not None and prof["worst_week"] <= -0.2:
        r.append(f"Violent: lost {-prof['worst_week']:.0%} in a single week this year")
    if mgmt["controlled"]:
        r.append(f"Controlled company: insiders hold {mgmt['insiders']:.0%}, small free float")
    if mgmt.get("short_float") and mgmt["short_float"] > 0.10:
        r.append(f"{mgmt['short_float']:.0%} of the free float is sold short")
    if div["trend"] in ("cut", "suspended"):
        r.append("Dividend was " + div["trend"] + " in the past year")
    if mgmt["ceo_change"]:
        r.append("Chief executive changed recently")
    if m["region"] == "hk" or (m["region"] == "us" and (deep_country(m) in ("China", "Hong Kong"))):
        r.append("China exposure: policy and regulatory risk")
    if prof["weeks"] < MIN_WEEKS:
        r.append(f"Newcomer: only {prof['weeks'] / 52:.1f} years on the market, so no record of how it behaves in a downturn")
    if prof["spike"]:
        r.append("The very top was a brief spike; potential is measured to the level it held, not the top")
    for f in m.get("flags", []):
        if f.startswith(("SEC filing", "Share count rising", "Cut its guidance")):
            r.append(f)
    return r


def deep_country(m):
    return (m.get("_deep_country") or "")


def jackpot_score(m, prof, earn, div, mgmt):
    s = m["spring"]
    s += {"recovering": 8, "growing": 5, "stable": 0, "falling": -6}.get(earn["state"], 0)
    up = earn.get("earnings_upside")
    if up and up > 0:
        s += min(up, 3.0) / 3.0 * 8
    if div["years_paid_5"] >= 4 and div["trend"] not in ("cut", "suspended"):
        s += 3
    if mgmt["buying_back"]:
        s += 3
    if (m.get("stab") or {}).get("state") == "falling":
        s -= 4
    return round(s, 1)


def load_research():
    import json, os
    path = os.path.join(C.DATA, "research.json")
    try:
        return json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        return {}


def build_shortlist(stocks, hist, news, rates, store, cached_fetch):
    """stocks: everything that passed the safety gate. Returns (top, near_misses)."""
    research = load_research()
    pool = []
    for m in stocks:
        if m["region"] not in TRADABLE:
            continue
        prof = price_profile(hist.get(m["symbol"]) or [], m["price"])
        if not prof or prof["potential"] < MIN_POTENTIAL_B:
            continue
        m["_prof"] = prof
        pool.append(m)
    log(f"  short list: {len(pool)} tradable stocks sit at least 60% below a level they held "
        f"({sum(1 for m in pool if m['_prof']['potential'] >= MIN_POTENTIAL)} of them at least 67% below)")
    deep, _ = cached_fetch(store, "deep", pool, 7, fetch_deep, workers=3)
    need = {(deep.get(m["symbol"]) or {}).get("info", {}).get("financialCurrency") for m in pool} - {None} - set(rates)
    if need:
        rates = {**rates, **fx_rates(need)}

    top, near = [], []
    for m in pool:
        d = deep.get(m["symbol"])
        prof = m.pop("_prof")
        if not d:
            near.append({"symbol": m["symbol"], "name": m["name"], "theme": m["theme"], "country": m["country"],
                         "potential": prof["potential"], "spring": m["spring"],
                         "reason": "Company data could not be loaded"})
            continue
        m["_deep_country"] = d["info"].get("country")
        heads = news.get(m["symbol"], [])
        earn = earnings_profile(m, d, prof, rates)
        div = dividend_profile(m, d)
        mgmt = management_profile(m, d, heads)
        why_not = _exclusion(m, prof, earn, mgmt, research.get(m["symbol"]))
        entry = {k: m.get(k) for k in ("symbol", "name", "theme", "country", "region", "currency", "price", "low",
                                        "high", "cap_usd", "size", "quality", "safety", "setup", "potential_score",
                                        "spring", "safety_notes", "flags", "stab", "dna", "spark", "news", "trials",
                                        "pe_fwd", "upside", "next_earnings")}
        entry["potential_score"] = m.get("potential")
        entry.update({"profile": prof, "earnings": earn, "dividend": div, "management": mgmt,
                      "newcomer": prof["weeks"] < MIN_WEEKS,
                      "tier": "A" if prof["potential"] >= MIN_POTENTIAL else "B",
                      "summary": d["summary"], "employees": d["info"].get("fullTimeEmployees"),
                      "home": d["info"].get("country"),
                      "risks": _risks(m, prof, earn, div, mgmt),
                      "jackpot": jackpot_score(m, prof, earn, div, mgmt)})
        m.pop("_deep_country", None)
        if why_not:
            near.append({"symbol": m["symbol"], "name": m["name"], "theme": m["theme"], "country": m["country"],
                         "potential": prof["potential"], "spring": m["spring"], "reason": why_not})
        else:
            top.append(entry)
    # names that clear the +200% bar always come first; the rest only fill what is left of the 20
    top.sort(key=lambda e: (e["tier"], -e["jackpot"]))
    chosen, per_theme, newcomers = [], {}, 0
    for e in top:
        if e["newcomer"] and newcomers >= MAX_NEWCOMERS:
            near.append({"symbol": e["symbol"], "name": e["name"], "theme": e["theme"], "country": e["country"],
                         "potential": e["profile"]["potential"], "spring": e["spring"],
                         "reason": f"Passed every test, but {MAX_NEWCOMERS} stronger newcomers are already listed"})
            continue
        if per_theme.get(e["theme"], 0) >= MAX_PER_THEME:
            near.append({"symbol": e["symbol"], "name": e["name"], "theme": e["theme"], "country": e["country"],
                         "potential": e["profile"]["potential"], "spring": e["spring"],
                         "reason": f"Passed every test, but {MAX_PER_THEME} stronger {e['theme']} names are already listed"})
            continue
        per_theme[e["theme"]] = per_theme.get(e["theme"], 0) + 1
        newcomers += 1 if e["newcomer"] else 0
        chosen.append(e)
        if len(chosen) >= TOP_N:
            break
    listed = {e["symbol"] for e in chosen}
    for e in top:
        if e["symbol"] not in listed and not any(n["symbol"] == e["symbol"] for n in near):
            near.append({"symbol": e["symbol"], "name": e["name"], "theme": e["theme"], "country": e["country"],
                         "potential": e["profile"]["potential"], "spring": e["spring"],
                         "reason": "Passed every test, ranked just outside the 20"})
    near.sort(key=lambda n: -n["spring"])
    return chosen, near[:40]
