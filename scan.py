#!/usr/bin/env python3
"""
SPRING RADAR v2 - worldwide scan for strong low- and mid-cap companies near the bottom of a
wide 52-week range, with hard safety gates against dilution and bankruptcy.

Pipeline
  1. Universe  : every stock between MIN_CAP and MAX_CAP on the primary exchanges of ~36 countries
                 (Yahoo Finance screener). A second pass pulls all pharma and biotech names.
  2. Filter    : wide 52-week range and price near the bottom of it (looser for the pharma track).
  3. Deep scan : fundamentals, 10 years of weekly prices, 3 years of share-count history,
                 and for pharma the number of active Phase 2 / Phase 3 trials (ClinicalTrials.gov).
  4. Safety gate (hard, fail = not listed):
                 - funds itself, or has 2+ years of cash at the current burn rate
                 - debt it can carry (net debt vs operating profit)
                 - can pay its near-term bills (current ratio)
                 - has not grown its share count by more than 20% in 3 years
  5. Scores    : Quality, Safety, Setup (and Potential for pharma) -> Spring score.
  6. Memory    : top picks are logged with date and price in tracker.json and re-priced each run.

Usage
  python scan.py                 # full scan
  python scan.py --regions no,se # quick test on a few countries
"""
import argparse, json, math, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import yfinance as yf
from yfinance import EquityQuery

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RULES_VERSION = 2

MIN_CAP_USD = 3e8      # low cap floor
MAX_CAP_USD = 1e10     # mid cap ceiling
MIN_DVOL_USD = 1e6     # average daily traded value floor
MIN_RATIO, MAX_POS = 2.0, 0.25          # main track: 52w high/low, position in range
PH_MIN_RATIO, PH_MAX_POS = 1.6, 0.40    # pharma track: looser, so more candidates get examined
MAX_DEEP, MAX_DEEP_PHARMA = 1200, 450
TRACK_TOP = 40
MIN_RUNWAY_YEARS = 2.0
MAX_SHARE_GROWTH_3Y = 0.20

REGIONS = {
    "us": (["NMS", "NYQ", "NGM", "NCM", "ASE"], "USD"), "ca": (["TOR"], "CAD"),
    "gb": (["LSE"], "GBP"), "de": (["GER"], "EUR"), "fr": (["PAR"], "EUR"), "ch": (["EBS"], "CHF"),
    "nl": (["AMS"], "EUR"), "be": (["BRU"], "EUR"), "se": (["STO"], "SEK"), "no": (["OSL"], "NOK"),
    "dk": (["CPH"], "DKK"), "fi": (["HEL"], "EUR"), "it": (["MIL"], "EUR"), "es": (["MCE"], "EUR"),
    "pt": (["LIS"], "EUR"), "at": (["VIE"], "EUR"), "ie": (["ISE"], "EUR"), "pl": (["WSE"], "PLN"),
    "gr": (["ATH"], "EUR"), "il": (["TLV"], "ILS"), "jp": (["JPX"], "JPY"), "hk": (["HKG"], "HKD"),
    "tw": (["TAI"], "TWD"), "kr": (["KSC", "KOE"], "KRW"), "in": (["NSI"], "INR"),
    "sg": (["SES"], "SGD"), "au": (["ASX"], "AUD"), "nz": (["NZE"], "NZD"), "id": (["JKT"], "IDR"),
    "th": (["SET"], "THB"), "my": (["KLS"], "MYR"), "sa": (["SAU"], "SAR"), "za": (["JNB"], "ZAR"),
    "br": (["SAO"], "BRL"), "mx": (["MEX"], "MXN"), "cl": (["SGO"], "CLP"),
}
REGION_NAME = {
    "us": "USA", "ca": "Canada", "gb": "UK", "de": "Germany", "fr": "France", "ch": "Switzerland",
    "nl": "Netherlands", "be": "Belgium", "se": "Sweden", "no": "Norway", "dk": "Denmark",
    "fi": "Finland", "it": "Italy", "es": "Spain", "pt": "Portugal", "at": "Austria", "ie": "Ireland",
    "pl": "Poland", "gr": "Greece", "il": "Israel", "jp": "Japan", "hk": "Hong Kong",
    "tw": "Taiwan", "kr": "South Korea", "in": "India", "sg": "Singapore", "au": "Australia",
    "nz": "New Zealand", "id": "Indonesia", "th": "Thailand", "my": "Malaysia", "sa": "Saudi Arabia",
    "za": "South Africa", "br": "Brazil", "mx": "Mexico", "cl": "Chile",
}
MINOR = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

MINING = {"Gold", "Silver", "Copper", "Other Industrial Metals & Mining", "Other Precious Metals & Mining",
          "Uranium", "Coking Coal", "Thermal Coal", "Aluminum", "Steel"}
PHARMA = {"Drug Manufacturers - General", "Drug Manufacturers - Specialty & Generic", "Biotechnology"}
PHARMA_SCREEN = ["Biotechnology", "Drug Manufacturers—General", "Drug Manufacturers—Specialty & Generic"]
AI_TECH = {"Semiconductors", "Semiconductor Equipment & Materials", "Software - Infrastructure",
           "Software - Application", "Computer Hardware", "Communication Equipment",
           "Electronic Components", "Information Technology Services"}


def theme_of(sector, industry):
    s, i = sector or "", (industry or "").replace("—", " - ")
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
            rates[c] = float(close[f"{c}USD=X"].dropna().iloc[-1])
        except Exception:
            log("no FX for", c)
    return rates


def norm_name(n):
    n = (n or "").lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(asa|ab|as|ag|sa|se|nv|plc|inc|corp|corporation|company|co|ltd|limited|oyj|"
               r"holdings|holding|group|spa|adr|class a|class b|ord|shs|the|publ)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_universe(regions, rates, pharma_only=False):
    rows = {}
    for reg in regions:
        exch, cur = REGIONS[reg]
        r_ = rates.get(cur, 1.0)
        parts = [
            EquityQuery("eq", ["region", reg]),
            EquityQuery("is-in", ["exchange"] + exch),
            EquityQuery("gt", ["intradaymarketcap", MIN_CAP_USD / r_]),
            EquityQuery("lt", ["intradaymarketcap", MAX_CAP_USD / r_]),
        ]
        if pharma_only:
            parts.append(EquityQuery("or", [EquityQuery("eq", ["industry", i]) for i in PHARMA_SCREEN]))
        q = EquityQuery("and", parts)
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
            time.sleep(0.2)
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
    cap_usd = (x.get("marketCap") or 0) * rate
    dvol = (x.get("averageDailyVolume3Month") or 0) * p / div * rate
    return {
        "symbol": x["symbol"],
        "name": x.get("longName") or x.get("shortName") or x["symbol"],
        "region": x["_region"], "country": REGION_NAME.get(x["_region"], x["_region"]),
        "exchange": x.get("fullExchangeName") or x.get("exchange"),
        "currency": cur,
        "price": p, "low": lo, "high": hi, "_bad_range": bad_range,
        "ratio": hi / lo, "pos": max(0.0, min(1.0, (p - lo) / (hi - lo))),
        "off_high": 1 - p / hi, "above_low": p / lo - 1,
        "cap_usd": cap_usd, "dvol_usd": dvol,
        "size": "Small cap" if cap_usd < 2e9 else "Mid cap",
        "eps_ttm": x.get("epsTrailingTwelveMonths"), "eps_fwd": x.get("epsForward"),
        "pe_fwd": x.get("forwardPE"), "pe_ttm": x.get("trailingPE"),
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


# ---------- price-history signals ----------

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
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    for th, sc in steps:
        if v >= th:
            return sc
    return 0.0


# ---------- safety gate ----------

def cash_gate(m, info):
    """Stage 1 of the safety gate, from the balance sheet and cash flow.
    Returns (ok, parts, notes, reason)."""
    sector = info.get("sector")
    notes, parts = [], {}
    if sector == "Financial Services":
        roe = info.get("returnOnEquity")
        if not (m["eps_ttm"] and m["eps_ttm"] > 0):
            return False, parts, notes, "Financial company without positive earnings"
        if roe is None or roe < 0.05:
            return False, parts, notes, "Financial company with weak return on equity"
        parts["funding"] = 1.0 if roe >= 0.12 else 0.7
        notes.append("Profitable financial company")
        return True, parts, notes, None

    cash, debt = info.get("totalCash"), info.get("totalDebt") or 0
    fcf, ocf, ebitda = info.get("freeCashflow"), info.get("operatingCashflow"), info.get("ebitda")
    burn = fcf if fcf is not None else ocf
    if burn is None or cash is None:
        return False, parts, notes, "No cash-flow data to verify safety"

    if burn >= 0:
        parts["funding"] = 1.0
        notes.append("Funds itself from its own cash flow")
    else:
        runway = cash / (-burn)
        m["runway_years"] = runway
        if runway < MIN_RUNWAY_YEARS:
            return False, parts, notes, f"Cash lasts about {max(1, round(runway * 12))} months at the current burn"
        parts["funding"] = 0.9 if runway >= 4 else 0.7 if runway >= 3 else 0.5
        notes.append("Free cash flow slightly negative, large cash reserve" if runway >= 10
                     else f"Burning cash, about {runway:.1f} years of runway")

    net_debt = debt - cash
    limit = 6.0 if sector in ("Utilities", "Real Estate") else 3.5
    if net_debt <= 0:
        parts["debt"] = 1.0
        notes.append("More cash than debt")
    elif ebitda and ebitda > 0:
        r = net_debt / ebitda
        m["net_debt_ebitda"] = r
        if r > limit:
            return False, parts, notes, f"Net debt is {r:.1f} times operating profit"
        parts["debt"] = 0.8 if r <= 1 else 0.6 if r <= 2 else 0.35
        notes.append(f"Net debt {r:.1f}x operating profit")
    else:
        return False, parts, notes, "In debt and without operating profit"

    cr = info.get("currentRatio")
    if cr is not None:
        if cr < 0.8:
            return False, parts, notes, f"Short-term bills exceed liquid assets (current ratio {cr:.1f})"
        parts["liquidity"] = 1.0 if cr >= 2 else 0.7 if cr >= 1.2 else 0.4
    return True, parts, notes, None


def share_growth(sym):
    """Share count change over about 3 years, corrected for stock splits. None when unknown."""
    try:
        t = yf.Ticker(sym)
        sh = t.get_shares_full(start=date.today() - timedelta(days=365 * 3 + 30))
        sh = sh.dropna()
        if len(sh) < 4:
            return None
        first = float(sh.iloc[:3].median())
        last = float(sh.iloc[-3:].median())
        if first <= 0:
            return None
        g = last / first - 1
        if abs(g) > 0.2:
            # the raw series is not split-adjusted: divide out any split since the first sample
            try:
                sp = t.splits
                start = sh.index[1]
                factor = 1.0
                for when, ratio in sp.items():
                    if when.tz_localize(None) >= start.tz_localize(None) and ratio and ratio > 0:
                        factor *= float(ratio)
                g = (1 + g) / factor - 1
            except Exception:
                return None
        return g
    except Exception:
        return None


def share_gate(g):
    """Stage 2 of the safety gate. Returns (ok, score, note, reason)."""
    if g is None:
        return True, 0.5, "Share-count history not available", None
    if g > 5:
        return False, 0.0, None, "Share count up more than 5x in 3 years (dilution or a data error)"
    if g > MAX_SHARE_GROWTH_3Y:
        return False, 0.0, None, f"Share count up {g:.0%} in 3 years"
    if g < -0.45:
        return True, 0.4, "Share count fell sharply (possible reverse split)", None
    if g > 0.10:
        return True, 0.3, f"Share count up {g:.0%} in 3 years", None
    if g > 0.03:
        return True, 0.6, f"Share count up {g:.0%} in 3 years", None
    if g >= -0.01:
        return True, 0.85, "Share count stable for 3 years", None
    return True, 1.0, f"Buying back shares ({-g:.0%} fewer in 3 years)", None


# ---------- pharma pipeline ----------

def trial_counts(name):
    """Active Phase 3 and Phase 2 trials where the company is lead sponsor (ClinicalTrials.gov)."""
    clean = re.sub(r"\(.*?\)", " ", name)
    clean = re.sub(r"[,./]", " ", clean)
    clean = re.sub(r"\b(Inc|Incorporated|Corp|Corporation|Ltd|Limited|plc|PLC|AG|SA|S A|NV|N V|AB|A S|ASA|"
                   r"Co|Company|Holdings|Holding|Group|SE|Oyj|publ|ADR|Class [AB]|KGaA|GmbH|SpA|S p A)\b", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) < 4:
        return None
    out = {}
    for ph in ("3", "2"):
        q = urllib.parse.urlencode({
            "query.lead": clean,
            "filter.overallStatus": "RECRUITING,ACTIVE_NOT_RECRUITING,ENROLLING_BY_INVITATION,NOT_YET_RECRUITING",
            "aggFilters": "phase:" + ph, "countTotal": "true", "pageSize": "1", "fields": "NCTId"})
        try:
            req = urllib.request.Request("https://clinicaltrials.gov/api/v2/studies?" + q,
                                         headers={"User-Agent": "Mozilla/5.0 (spring-radar)"})
            out["p" + ph] = int(json.load(urllib.request.urlopen(req, timeout=20)).get("totalCount") or 0)
        except Exception:
            return None
    return out


def potential_score(m, info, trials):
    parts = {}
    if trials:
        p3, p2 = trials["p3"], trials["p2"]
        parts["pipeline"] = 1.0 if p3 >= 3 else 0.85 if p3 == 2 else 0.65 if p3 == 1 else 0.4 if p2 >= 2 else 0.2
    rev, gm = info.get("totalRevenue"), info.get("grossMargins")
    parts["commercial"] = (1.0 if gm and gm >= 0.6 else 0.7) if rev and rev > 5e7 else 0.3
    parts["growth"] = grade(info.get("revenueGrowth"), [(0.25, 1.0), (0.10, 0.75), (0.0, 0.45)])
    n = info.get("numberOfAnalystOpinions") or 0
    if n >= 3:
        rec = info.get("recommendationMean")
        if rec:
            parts["analysts"] = 1.0 if rec <= 1.8 else 0.75 if rec <= 2.3 else 0.4 if rec <= 3.0 else 0.1
        if m.get("upside") is not None:
            parts["upside"] = 1.0 if m["upside"] >= 0.6 else 0.75 if m["upside"] >= 0.3 else 0.45 if m["upside"] >= 0 else 0.0
    vals = [v for v in parts.values() if v is not None]
    return round(100 * sum(vals) / len(vals)) if vals else 50


# ---------- quality and setup ----------

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


def threaded(fn, items, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


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
        if (i // 40) % 5 == 4:
            log(f"  history {min(i + 40, len(syms))}/{len(syms)}")
        time.sleep(0.4)

    def get_info(sym):
        try:
            return retry(lambda: yf.Ticker(sym).info, tries=3, wait=1.5) or {}
        except Exception:
            return {}
    infos = threaded(get_info, syms, workers=4)
    # Yahoo sometimes rejects a burst of requests; fetch the empty ones again, one at a time
    empty = [k for k, i in enumerate(infos) if not i.get("sector") and not i.get("totalCash")]
    for k in empty:
        time.sleep(0.3)
        infos[k] = get_info(syms[k])
    still = sum(1 for k in empty if not infos[k].get("sector") and not infos[k].get("totalCash"))
    log(f"  fundamentals {len(infos)}/{len(syms)} ({len(empty)} retried, {still} still without data)")

    passed, rejected = [], []
    for m, info in zip(cands, infos):
        m["sector"], m["industry"] = info.get("sector"), (info.get("industry") or "").replace("—", " - ")
        m["theme"] = theme_of(m["sector"], m["industry"])
        ok, parts, notes, reason = cash_gate(m, info)
        if not ok:
            rejected.append({"symbol": m["symbol"], "name": m["name"], "theme": m["theme"], "reason": reason})
            continue
        m["_info"], m["_parts"], m["_notes"] = info, parts, notes
        passed.append(m)
    log(f"  cash and debt gate: {len(passed)} pass, {len(rejected)} rejected")

    growth = threaded(share_growth, [m["symbol"] for m in passed], workers=4)
    survivors = []
    for m, g in zip(passed, growth):
        ok, sc, note, reason = share_gate(g)
        m["share_growth_3y"] = g
        if not ok:
            rejected.append({"symbol": m["symbol"], "name": m["name"], "theme": m["theme"], "reason": reason})
            continue
        m["_parts"]["shares"] = sc
        if note:
            m["_notes"].append(note)
        survivors.append(m)
    log(f"  dilution gate: {len(survivors)} pass")

    pharma = [m for m in survivors if m["theme"] == "Pharma & Biotech"]
    trials = dict(zip([m["symbol"] for m in pharma],
                      threaded(lambda m: trial_counts(m["name"]), pharma, workers=4)))
    log(f"  pipeline lookups for {len(pharma)} pharma names")

    out = []
    for m in survivors:
        info = m.pop("_info")
        parts, notes = m.pop("_parts"), m.pop("_notes")
        closes = hist.get(m["symbol"]) or []
        dna = rebound_dna(closes) if len(closes) >= 104 else None
        stab = stabilisation(closes) if closes else None
        q = quality_score(m, info)
        s = setup_score(m, dna, stab)
        vals = list(parts.values())
        safety = round(100 * sum(vals) / len(vals)) if vals else 50
        flags = []
        if dna and dna["open"] and dna["open_weeks"] > 156:
            flags.append("Below its old peak for 3+ years")
        if stab and stab["state"] == "falling":
            flags.append("Made a new low within the last week")
        if len(closes) < 104:
            flags.append("Less than 2 years of price history")
        if 0.10 < (m.get("share_growth_3y") or 0) <= MAX_SHARE_GROWTH_3Y:
            flags.append("Share count rising")
        is_pharma = m["theme"] == "Pharma & Biotech"
        tr = trials.get(m["symbol"]) if is_pharma else None
        if is_pharma:
            pot = potential_score(m, info, tr)
            spring = 0.20 * q + 0.20 * s + 0.30 * safety + 0.30 * pot
            m["potential"], m["trials"] = pot, tr
        else:
            spring = 0.35 * q + 0.30 * s + 0.35 * safety
        if flags and flags[0].startswith("Below"):
            spring -= 12
        m.update({
            "roe": info.get("returnOnEquity"), "margin": info.get("profitMargins"),
            "gross_margin": info.get("grossMargins"), "rev_growth": info.get("revenueGrowth"),
            "rec": info.get("recommendationMean"), "analysts": info.get("numberOfAnalystOpinions"),
            "target": info.get("targetMeanPrice"),
            "dna": dna, "stab": stab, "safety_notes": notes,
            "quality": q, "setup": s, "safety": safety, "spring": max(0, round(spring)), "flags": flags,
            "spark": [round(v, 4) for v in closes[-104::2]],
        })
        out.append(m)
    return out, rejected


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
    picks = ranked[:TRACK_TOP]
    # the pharma track is logged as well, so its own top names are followed even when they rank lower
    picks += [m for m in ranked if m["theme"] == "Pharma & Biotech"][:15]
    for m in picks:
        if m["symbol"] not in tr:
            tr[m["symbol"]] = {
                "name": m["name"], "theme": m["theme"], "country": m["country"], "currency": m["currency"],
                "first_seen": today, "first_price": m["price"], "first_spring": m["spring"],
                "low_at_flag": m["low"], "high_at_flag": m["high"], "rules": RULES_VERSION,
            }
    live = {m["symbol"]: m for m in ranked}
    for s, t in tr.items():
        p = live[s]["price"] if s in live else universe_price.get(s)
        if p:
            t["last_price"], t["last_date"] = p, today
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
    log("Pharma and biotech universe")
    ph_syms = set(fetch_universe(regions, rates, pharma_only=True))

    def in_bounds(m):
        return MIN_CAP_USD <= m["cap_usd"] <= MAX_CAP_USD and m["dvol_usd"] >= MIN_DVOL_USD
    metrics = [m for m in (basic_metrics(x, rates) for x in uni.values()) if m and in_bounds(m)]
    metrics = repair_ranges(dedupe(metrics))
    log(f"Universe after filters and de-duplication: {len(metrics)}")

    def width_near(m):
        return min(1.0, math.log(m["ratio"]) / math.log(4)) + (1 - m["pos"] / 0.40)

    main_c = [m for m in metrics if m["ratio"] >= MIN_RATIO and m["pos"] <= MAX_POS]
    # cheap pre-rank: profitable companies first, then width and nearness to the low
    main_c.sort(key=lambda m: -((1 if (m["eps_ttm"] or 0) > 0 else 0) + (1 if (m["eps_fwd"] or 0) > 0 else 0)
                                + width_near(m)))
    main_c = main_c[:a.max_deep]
    chosen = {m["symbol"] for m in main_c}
    ph_c = [m for m in metrics if m["symbol"] in ph_syms and m["symbol"] not in chosen
            and m["ratio"] >= PH_MIN_RATIO and m["pos"] <= PH_MAX_POS]
    ph_c.sort(key=lambda m: -width_near(m))
    ph_c = ph_c[:MAX_DEEP_PHARMA]
    n_match = len([m for m in metrics if m["ratio"] >= MIN_RATIO and m["pos"] <= MAX_POS]) + len(ph_c)
    log(f"Candidates: {len(main_c)} main track + {len(ph_c)} pharma track")

    deep, rejected = deep_scan(main_c + ph_c)
    deep.sort(key=lambda m: -m["spring"])

    by_region, reasons = {}, {}
    for m in metrics:
        by_region[m["country"]] = by_region.get(m["country"], 0) + 1
    for r in rejected:
        k = re.sub(r"[0-9][0-9.,%]*", "N", r["reason"])
        reasons[k] = reasons.get(k, 0) + 1
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "date": today,
        "rules_version": RULES_VERSION,
        "universe": len(metrics), "matches": n_match, "scanned": len(main_c) + len(ph_c),
        "passed": len(deep), "rejected": len(rejected), "reject_reasons": reasons,
        "countries": len(by_region), "by_country": by_region,
        "rules": {"min_cap_usd": MIN_CAP_USD, "max_cap_usd": MAX_CAP_USD, "min_ratio": MIN_RATIO,
                  "max_pos": MAX_POS, "min_runway_years": MIN_RUNWAY_YEARS,
                  "max_share_growth_3y": MAX_SHARE_GROWTH_3Y},
    }
    tracker = update_tracker(today, deep, {m["symbol"]: m["price"] for m in metrics})
    json.dump(clean({"meta": meta, "stocks": deep, "tracker": tracker, "rejected": rejected}),
              open(os.path.join(DATA, "latest.json"), "w"), separators=(",", ":"))
    slim = [{k: m.get(k) for k in ("symbol", "name", "theme", "country", "price", "currency", "pos", "ratio",
                                    "quality", "safety", "setup", "potential", "spring")} for m in deep[:100]]
    json.dump(clean({"meta": meta, "top": slim}),
              open(os.path.join(DATA, "history", today + ".json"), "w"), separators=(",", ":"))
    log("Done. Top 12:")
    for m in deep[:12]:
        log(f"  {m['spring']:>3}  {m['symbol']:<10} {m['name'][:32]:<32} {m['theme']:<17} "
            f"x{m['ratio']:.1f} pos {m['pos']:.0%} Q{m['quality']} Sa{m['safety']} Se{m['setup']}")


if __name__ == "__main__":
    main()
