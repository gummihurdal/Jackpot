"""Build the worldwide universe from the Yahoo Finance screener."""
import math, re, time

import yfinance as yf
from yfinance import EquityQuery

from .config import (AI_TECH, MAX_CAP_USD, MIN_CAP_USD, MINING, MINOR, PHARMA, PHARMA_SCREEN,
                     REGION_NAME, REGIONS)
from .util import log, retry


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
            rates[c] = float(close[f"{c}USD=X"].dropna().iloc[-1])
        except Exception:
            log("no FX for", c)
    return rates


_SUFFIX = (r"\b(asa|ab|as|ag|sa|se|nv|plc|inc|corp|corporation|company|co|ltd|limited|oyj|holdings|holding|"
           r"group|spa|adr|class a|class b|ord|shs|the|publ)\b")


def norm_name(n):
    n = re.sub(r"[^a-z0-9 ]", " ", (n or "").lower())
    return re.sub(r"\s+", " ", re.sub(_SUFFIX, " ", n)).strip()


def fetch_universe(regions, rates, pharma_only=False, failed=None):
    """failed: optional list that collects the countries whose listing could not be loaded."""
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
                if failed is not None:
                    failed.append(reg)
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
        "next_earnings": x.get("earningsTimestampStart") or x.get("earningsTimestamp"),
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
    """Merge cross-listings of the same company, keeping the most traded line."""
    best = {}
    for m in items:
        k = norm_name(m["name"])
        if k not in best or m["dvol_usd"] > best[k]["dvol_usd"]:
            best[k] = m
    return list(best.values())


def width_near(m):
    return min(1.0, math.log(max(m["ratio"], 1.0001)) / math.log(4)) + (1 - m["pos"] / 0.40)
