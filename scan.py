#!/usr/bin/env python3
"""
SPRING RADAR - worldwide scan for strong companies near the bottom of a wide 52-week range.

Idea (Hafnia model): very cyclical but very strong company, price range e.g. 6 -> 27,
buy near the bottom and wait.

Pipeline
  1. Universe  : every stock above a market-cap floor on the primary exchanges of ~40 countries
                 (Yahoo Finance screener, one query per country, no hand-made ticker lists).
  2. Filter    : 52-week high/low ratio >= MIN_RATIO and price in the lowest MAX_POS of that range.
  3. Deep scan : fundamentals + 10 years of weekly prices for the survivors.
  4. Scores    : Quality (is the company strong?)  and  Setup (is the spring coiled?).
                 Setup includes "rebound DNA" - how often this stock fell 40%+ before and came back.
  5. Memory    : every stock that reaches the top list is logged with date and price in tracker.json,
                 and re-priced on every run, so we can see whether the method works.

Usage
  python scan.py                       # full scan
  python scan.py       --regions no,se # quick test on a few countries
"""
import argparse, json, math, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from yfinance import EquityQuery

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

MIN_CAP_USD = 2e9      # market-cap floor
MIN_DVOL_USD = 2e6     # average daily traded value floor
MIN_RATIO = 2.0        # 52w high / 52w low
MAX_POS = 0.25         # position in the 52w range (0 = at the low, 1 = at the high)
MAX_DEEP = 800         # how many survivors get the deep scan
TRACK_TOP = 40         # top-N by Spring score that get logged in the tracker

# country -> (primary exchanges, main currency)
REGIONS = {
    "us": (["NMS", "NYQ", "NGM", "NCM", "ASE"], "USD"),
    "ca": (["TOR"], "CAD"),
    "gb": (["LSE"], "GBP"),
    "de": (["GER"], "EUR"),
    "fr": (["PAR"], "EUR"),
    "ch": (["EBS"], "CHF"),
    "nl": (["AMS"], "EUR"),
    "be": (["BRU"], "EUR"),
    "se": (["STO"], "SEK"),
    "no": (["OSL"], "NOK"),
    "dk": (["CPH"], "DKK"),
    "fi": (["HEL"], "EUR"),
    "it": (["MIL"], "EUR"),
    "es": (["MCE"], "EUR"),
    "pt": (["LIS"], "EUR"),
    "at": (["VIE"], "EUR"),
    "ie": (["ISE"], "EUR"),
    "pl": (["WSE"], "PLN"),
    "gr": (["ATH"], "EUR"),
    "il": (["TLV"], "ILS"),
    "jp": (["JPX"], "JPY"),
    "hk": (["HKG"], "HKD"),
    "cn": (["SHH", "SHZ"], "CNY"),
    "tw": (["TAI"], "TWD"),
    "kr": (["KSC", "KOE"], "KRW"),
    "in": (["NSI"], "INR"),
    "sg": (["SES"], "SGD"),
    "au": (["ASX"], "AUD"),
    "nz": (["NZE"], "NZD"),
    "id": (["JKT"], "IDR"),
    "th": (["SET"], "THB"),
    "my": (["KLS"], "MYR"),
    "sa": (["SAU"], "SAR"),
    "za": (["JNB"], "ZAR"),
    "br": (["SAO"], "BRL"),
    "mx": (["MEX"], "MXN"),
    "cl": (["SGO"], "CLP"),
}
REGION_NAME = {
    "us": "USA", "ca": "Canada", "gb": "UK", "de": "Germany", "fr": "France", "ch": "Switzerland",
    "nl": "Netherlands", "be": "Belgium", "se": "Sweden", "no": "Norway", "dk": "Denmark",
    "fi": "Finland", "it": "Italy", "es": "Spain", "pt": "Portugal", "at": "Austria", "ie": "Ireland",
    "pl": "Poland", "gr": "Greece", "il": "Israel", "jp": "Japan", "hk": "Hong Kong", "cn": "China",
    "tw": "Taiwan", "kr": "South Korea", "in": "India", "sg": "Singapore", "au": "Australia",
    "nz": "New Zealand", "id": "Indonesia", "th": "Thailand", "my": "Malaysia", "sa": "Saudi Arabia",
    "za": "South Africa", "br": "Brazil", "mx": "Mexico", "cl": "Chile",
}
# quote currencies given in 1/100 units
MINOR = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

MINING = {"Gold", "Silver", "Copper", "Other Industrial Metals & Mining", "Other Precious Metals & Mining",
          "Uranium", "Coking Coal", "Thermal Coal", "Aluminum", "Steel"}
PHARMA = {"Drug Manufacturers - General", "Drug Manufacturers - Specialty & Generic", "Biotechnology"}
AI_TECH = {"Semiconductors", "Semiconductor Equipment & Materials", "Software - Infrastructure",
           "Software - Application", "Computer Hardware", "Communication Equipment",
           "Electronic Components", "Information Technology Services"}


def theme_of(sector, industry):
    s, i = sector or "", industry or ""
    if i in MINING: return "Mining"
    if i in PHARMA: return "Pharma & Biotech"
    if i == "Aerospace & Defense": return "Defence"
    if i == "Marine Shipping": return "Shipping"
    if i in AI_TECH: return "AI & Tech"
    if s == "Healthcare": return "Healthcare"
    if s == "Financial Services": return "Finance"
    if s == "Consumer Defensive": return "Staples"
    if s == "Energy": return "Oil & Gas"
    if s == "Utilities": return "Power & Utilities"
    if s == "Technology": return "AI & Tech"
    if s == "Industrials": return "Industrials"
    if s == "Consumer Cyclical": return "Consumer"
    if s == "Communication Services": return "Telecom & Media"
    if s == "Real Estate": return "Real Estate"
    if s == "Basic Materials": return "Chemicals & Materials"
    return "Other"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def retry(fn, tries=4, wait=2.0):
    last = None
    for k in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            last = e
            time.sleep(wait * (k + 1))
    raise last


def fx_rates(currencies):
    """USD value of one unit of each currency."""
    rates = {"USD": 1.0}
    need = sorted(c for c in set(currencies) if c != "USD")
    if not need:
        return rates
    tick = [f"{c}USD=X" for c in need]
    df = retry(lambda: yf.download(tick, period="7d", interval="1d", progress=False,
                                   auto_adjust=True, threads=True))
    close = df["Close"] if "Close" in df else df
    for c in need:
        try:
            v = close[f"{c}USD=X"].dropna()
            rates[c] = float(v.iloc[-1])
        except Exception:
            log("no FX for", c)
    return rates


def norm_name(n):
    n = (n or "").lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(asa|ab|as|ag|sa|se|nv|plc|inc|corp|corporation|company|co|ltd|limited|oyj|"
               r"holdings|holding|group|spa|adr|class a|class b|ord|shs|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_universe(regions, rates):
    rows = {}
    for reg in regions:
        exch, cur = REGIONS[reg]
        floor_local = MIN_CAP_USD / rates.get(cur, 1.0)
        q = EquityQuery("and", [
            EquityQuery("eq", ["region", reg]),
            EquityQuery("is-in", ["exchange"] + exch),
            EquityQuery("gt", ["intradaymarketcap", floor_local]),
        ])
        offset, total, got = 0, None, 0
        while total is None or offset < total:
            try:
                r = retry(lambda: yf.screen(q, offset=offset, size=250,
                                            sortField="intradaymarketcap", sortAsc=False))
            except Exception as e:
                log(f"  {reg}: screen failed at {offset}: {e}")
                break
            total = r.get("total", 0)
            quotes = r.get("quotes", [])
            if not quotes:
                break
            for x in quotes:
                if x.get("quoteType") != "EQUITY":
                    continue
                x["_region"] = reg
                rows[x["symbol"]] = x
                got += 1
            offset += 250
            time.sleep(0.25)
        log(f"  {reg}: {got} of {total}")
    return rows


def basic_metrics(x, rates):
    p, lo, hi = x.get("regularMarketPrice"), x.get("fiftyTwoWeekLow"), x.get("fiftyTwoWeekHigh")
    if not p or p <= 0:
        return None
    bad_range = not lo or not hi or lo <= 0 or hi <= lo or not (lo * 0.98 <= p <= hi * 1.02)
    if bad_range:
        lo, hi = p, p * 1.0001  # placeholder, repaired from price history in repair_ranges()
    cur = x.get("currency") or "USD"
    major, div = MINOR.get(cur, (cur, 1.0))
    rate = rates.get(major)
    if rate is None:
        return None
    cap = x.get("marketCap") or 0
    cap_usd = cap * rate
    dvol = (x.get("averageDailyVolume3Month") or 0) * p / div * rate
    return {
        "symbol": x["symbol"],
        "name": x.get("longName") or x.get("shortName") or x["symbol"],
        "region": x["_region"],
        "country": REGION_NAME.get(x["_region"], x["_region"]),
        "exchange": x.get("fullExchangeName") or x.get("exchange"),
        "currency": cur,
        "price": p, "low": lo, "high": hi, "_bad_range": bad_range,
        "ratio": hi / lo,
        "pos": max(0.0, min(1.0, (p - lo) / (hi - lo))),
        "off_high": 1 - p / hi,
        "above_low": p / lo - 1,
        "cap_usd": cap_usd, "dvol_usd": dvol,
        "eps_ttm": x.get("epsTrailingTwelveMonths"),
        "eps_fwd": x.get("epsForward"),
        "pe_fwd": x.get("forwardPE"),
        "pe_ttm": x.get("trailingPE"),
        "ma200_gap": x.get("twoHundredDayAverageChangePercent"),
        "div_yield": x.get("dividendYield"),
    }


def set_range(m, lo, hi):
    p = m["price"]
    lo, hi = min(lo, p), max(hi, p)
    m.update({"low": lo, "high": hi, "ratio": hi / lo,
              "pos": max(0.0, min(1.0, (p - lo) / (hi - lo))) if hi > lo else 1.0,
              "off_high": 1 - p / hi, "above_low": p / lo - 1, "_bad_range": False})


def repair_ranges(items):
    """Yahoo sometimes returns a 52-week low of 0 (seen for Brazil and Mexico). Rebuild from daily prices."""
    bad = [m for m in items if m.get("_bad_range")]
    if not bad:
        return items
    log(f"Repairing 52-week range for {len(bad)} stocks from price history")
    by = {m["symbol"]: m for m in bad}
    syms = list(by)
    for i in range(0, len(syms), 60):
        chunk = syms[i:i + 60]
        try:
            df = retry(lambda: yf.download(chunk, period="1y", interval="1d", progress=False,
                                           auto_adjust=False, threads=True, group_by="ticker"))
        except Exception as e:
            log("  repair chunk failed", e)
            continue
        for s in chunk:
            try:
                d = df[s] if len(chunk) > 1 else df
                lo, hi = float(d["Low"].dropna().min()), float(d["High"].dropna().max())
                if lo > 0 and hi > lo:
                    set_range(by[s], lo, hi)
            except Exception:
                pass
        time.sleep(0.4)
    return [m for m in items if not m.get("_bad_range")]


def dedupe(items):
    best = {}
    for m in items:
        k = norm_name(m["name"])
        if k not in best or m["dvol_usd"] > best[k]["dvol_usd"]:
            best[k] = m
    return list(best.values())


def rebound_dna(closes):
    """Past falls of 40%+ from a peak, and whether the price came back to 90% of that peak."""
    peak, trough, in_dd, peak_i, trough_i = None, None, False, 0, 0
    recovered, weeks = 0, []
    for i, c in enumerate(closes):
        if peak is None or (not in_dd and c > peak):
            peak, peak_i = c, i
        if not in_dd:
            if c <= peak * 0.60:
                in_dd, trough, trough_i = True, c, i
        else:
            if c < trough:
                trough, trough_i = c, i
            if c >= peak * 0.90:
                recovered += 1
                weeks.append(i - trough_i)
                in_dd, peak, peak_i = False, c, i
    open_weeks = (len(closes) - 1 - peak_i) if in_dd else 0
    med = sorted(weeks)[len(weeks) // 2] if weeks else None
    return {"recovered": recovered, "median_weeks": med, "open": in_dd, "open_weeks": open_weeks}


def stabilisation(closes):
    last52 = closes[-52:]
    if len(last52) < 12:
        return {"state": "unknown", "weeks_since_low": None, "r4": None}
    lo_i = min(range(len(last52)), key=lambda k: last52[k])
    since = len(last52) - 1 - lo_i
    r4 = last52[-1] / last52[-5] - 1 if len(last52) >= 5 else 0.0
    above = last52[-1] / last52[lo_i] - 1
    if since <= 1:
        state = "falling"
    elif since >= 3 and above >= 0.08 and r4 > 0:
        state = "turning"
    else:
        state = "basing"
    return {"state": state, "weeks_since_low": since, "r4": r4}


def grade(v, steps):
    """steps: list of (threshold, score) checked in order with >=."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    for th, sc in steps:
        if v >= th:
            return sc
    return 0.0


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
    roe = info.get("returnOnEquity")
    parts["roe"] = grade(roe, [(0.15, 1.0), (0.08, 0.6), (0.0001, 0.3)])
    pm = info.get("profitMargins")
    parts["margin"] = grade(pm, [(0.15, 1.0), (0.07, 0.6), (0.0001, 0.3)])
    rg = info.get("revenueGrowth")
    parts["growth"] = grade(rg, [(0.15, 1.0), (0.05, 0.7), (0.0, 0.4)])
    if not fin:
        de = info.get("debtToEquity")
        parts["debt"] = None if de is None else (1.0 if de <= 50 else 0.7 if de <= 100 else 0.4 if de <= 200 else 0.1)
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
    parts["size"] = 1.0 if m["cap_usd"] >= 1e10 else 0.7 if m["cap_usd"] >= 5e9 else 0.4
    vals = [v for v in parts.values() if v is not None]
    # few data points -> pull the score toward the middle so thin data cannot look excellent
    cover = len(vals) / 10.0
    raw = sum(vals) / len(vals) if vals else 0.5
    return round(100 * (raw * min(1, cover + 0.3) + 0.5 * (1 - min(1, cover + 0.3)))), parts


def setup_score(m, dna, stab):
    width = min(1.0, math.log(m["ratio"]) / math.log(4.0))
    near = 1 - m["pos"] / MAX_POS
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


def deep_scan(cands):
    syms = [m["symbol"] for m in cands]
    hist = {}
    for i in range(0, len(syms), 40):
        chunk = syms[i:i + 40]
        try:
            df = retry(lambda: yf.download(chunk, period="10y", interval="1wk", progress=False,
                                           auto_adjust=True, threads=True, group_by="ticker"))
            for s in chunk:
                try:
                    c = (df[s]["Close"] if len(chunk) > 1 else df["Close"]).dropna()
                    if hasattr(c, "columns"):
                        c = c.iloc[:, 0]
                    hist[s] = [float(v) for v in c.values]
                except Exception:
                    pass
        except Exception as e:
            log("history chunk failed", e)
        log(f"  history {min(i + 40, len(syms))}/{len(syms)}")
        time.sleep(0.5)

    def get_info(sym):
        try:
            return retry(lambda: yf.Ticker(sym).info, tries=3, wait=1.5) or {}
        except Exception:
            return {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        infos = list(ex.map(get_info, syms))
    log(f"  fundamentals {len(infos)}/{len(syms)}")

    out = []
    for n, m in enumerate(cands, 1):
        info = infos[n - 1]
        closes = hist.get(m["symbol"]) or []
        dna = rebound_dna(closes) if len(closes) >= 104 else None
        stab = stabilisation(closes) if closes else None
        q, parts = quality_score(m, info)
        s = setup_score(m, dna, stab)
        flags = []
        if dna and dna["open"] and dna["open_weeks"] > 156:
            flags.append("Below its old peak for 3+ years")
        if (m["eps_ttm"] is not None and m["eps_ttm"] <= 0) and (m["eps_fwd"] is not None and m["eps_fwd"] <= 0):
            flags.append("Losing money now and on forecasts")
        if stab and stab["state"] == "falling":
            flags.append("Made a new low within the last week")
        de = info.get("debtToEquity")
        if de is not None and de > 250 and info.get("sector") not in ("Financial Services", "Real Estate"):
            flags.append("Heavy debt")
        if len(closes) < 104:
            flags.append("Less than 2 years of price history")
        penalty = 15 if flags[:2] and any(f.startswith(("Below", "Losing")) for f in flags) else 0
        spring = max(0, round(0.5 * q + 0.5 * s - penalty))
        m.update({
            "sector": info.get("sector"), "industry": info.get("industry"),
            "theme": theme_of(info.get("sector"), info.get("industry")),
            "roe": info.get("returnOnEquity"), "margin": info.get("profitMargins"),
            "rev_growth": info.get("revenueGrowth"), "debt_eq": de,
            "rec": info.get("recommendationMean"), "analysts": info.get("numberOfAnalystOpinions"),
            "target": info.get("targetMeanPrice"),
            "dna": dna, "stab": stab,
            "quality": q, "setup": s, "spring": spring, "flags": flags,
            "spark": [round(v, 4) for v in closes[-104::2]],
        })
        out.append(m)
    return out


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return round(o, 5)
    return o


def update_tracker(today, ranked, universe_price):
    path = os.path.join(DATA, "tracker.json")
    tr = json.load(open(path)) if os.path.exists(path) else {}
    # mainland China A-shares are left out of the log: they are hard to buy from Europe
    loggable = [m for m in ranked if not m["symbol"].endswith((".SS", ".SZ"))]
    for m in loggable[:TRACK_TOP]:
        if m["symbol"] not in tr:
            tr[m["symbol"]] = {
                "name": m["name"], "theme": m["theme"], "country": m["country"], "currency": m["currency"],
                "first_seen": today, "first_price": m["price"], "first_spring": m["spring"],
                "low_at_flag": m["low"], "high_at_flag": m["high"],
            }
    live = {m["symbol"]: m for m in ranked}
    for s, t in tr.items():
        p = live[s]["price"] if s in live else universe_price.get(s)
        if p:
            t["last_price"] = p
            t["last_date"] = today
            t["return"] = p / t["first_price"] - 1
            t["best"] = max(t.get("best", 0.0), t["return"])
            t["worst"] = min(t.get("worst", 0.0), t["return"])
        t["on_radar"] = s in live
        t["spring_now"] = live[s]["spring"] if s in live else None
    json.dump(clean(tr), open(path, "w"), indent=1)
    return tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="")
    ap.add_argument("--max-deep", type=int, default=MAX_DEEP)
    a = ap.parse_args()
    regions = [r for r in a.regions.split(",") if r] or list(REGIONS)
    os.makedirs(os.path.join(DATA, "history"), exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log("FX rates")
    rates = fx_rates({REGIONS[r][1] for r in regions})
    log("Universe")
    uni = fetch_universe(regions, rates)
    metrics = [m for m in (basic_metrics(x, rates) for x in uni.values()) if m]
    metrics = [m for m in metrics if m["cap_usd"] >= MIN_CAP_USD and m["dvol_usd"] >= MIN_DVOL_USD]
    metrics = dedupe(metrics)
    metrics = repair_ranges(metrics)
    log(f"Universe after filters and de-duplication: {len(metrics)}")

    cands = [m for m in metrics if m["ratio"] >= MIN_RATIO and m["pos"] <= MAX_POS]

    def pre(m):  # cheap pre-rank: profitable companies first, then width and nearness to the low
        earns = (1 if (m["eps_ttm"] or 0) > 0 else 0) + (1 if (m["eps_fwd"] or 0) > 0 else 0)
        return -(earns + min(1.0, math.log(m["ratio"]) / math.log(4)) + (1 - m["pos"] / MAX_POS))
    cands.sort(key=pre)
    log(f"Wide range + near the low: {len(cands)} (deep scan up to {a.max_deep})")
    deep = deep_scan(cands[:a.max_deep])
    deep.sort(key=lambda m: -m["spring"])

    by_region = {}
    for m in metrics:
        by_region[m["country"]] = by_region.get(m["country"], 0) + 1
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "date": today,
        "universe": len(metrics), "matches": len(cands), "scanned": len(deep),
        "countries": len(by_region), "by_country": by_region,
        "rules": {"min_cap_usd": MIN_CAP_USD, "min_ratio": MIN_RATIO, "max_pos": MAX_POS},
    }
    tracker = update_tracker(today, deep, {m["symbol"]: m["price"] for m in metrics})
    latest = clean({"meta": meta, "stocks": deep, "tracker": tracker})
    json.dump(latest, open(os.path.join(DATA, "latest.json"), "w"), separators=(",", ":"))
    slim = [{k: m.get(k) for k in ("symbol", "name", "theme", "country", "price", "currency", "pos",
                                    "ratio", "quality", "setup", "spring")} for m in deep[:80]]
    json.dump(clean({"meta": meta, "top": slim}),
              open(os.path.join(DATA, "history", today + ".json"), "w"), separators=(",", ":"))
    log(f"Done. Top 10:")
    for m in deep[:10]:
        log(f"  {m['spring']:>3}  {m['symbol']:<10} {m['name'][:34]:<34} {m['theme']:<18} "
            f"x{m['ratio']:.1f} pos {m['pos']:.0%} Q{m['quality']} S{m['setup']}")


if __name__ == "__main__":
    main()
