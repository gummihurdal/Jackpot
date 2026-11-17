"""The safety gate. A company is listed only if every stage passes."""
from datetime import datetime, timedelta, timezone

from .config import (MAX_NET_DEBT_EBITDA, MAX_NET_DEBT_EBITDA_ASSET_HEAVY, MAX_SHARE_GROWTH_3Y,
                     MIN_CURRENT_RATIO, MIN_RUNWAY_YEARS)
from .sources import NEWS_ADJ


def cash_gate(m, info):
    """Stage 1: funding, debt and liquidity. Returns (ok, parts, notes, reason)."""
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
        notes.append("Free cash flow slightly negative against a large cash reserve" if runway >= 10
                     else f"Burning cash, about {runway:.1f} years of runway")

    net_debt = debt - cash
    limit = MAX_NET_DEBT_EBITDA_ASSET_HEAVY if sector in ("Utilities", "Real Estate") else MAX_NET_DEBT_EBITDA
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
        if cr < MIN_CURRENT_RATIO:
            return False, parts, notes, f"Short-term bills exceed liquid assets (current ratio {cr:.1f})"
        parts["liquidity"] = 1.0 if cr >= 2 else 0.7 if cr >= 1.2 else 0.4
    return True, parts, notes, None


def share_gate(g):
    """Stage 2: the dilution record. Returns (ok, score, note, reason)."""
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


def filing_gate(f, burning):
    """Stage 3 (US-listed only): SEC filings. Returns (ok, score, notes, flags, reason)."""
    if f is None:
        return True, None, [], [], None
    if f.get("distress"):
        return False, 0, [], [], f"SEC filing {f['distress']['date']}: {f['distress']['what']}"
    if f.get("late"):
        return False, 0, [], [], f"Late with its financial reports (SEC notice {f['late']})"
    flags, score = [], 1.0
    if f.get("offering"):
        if burning:
            return False, 0, [], [], f"Sold new securities ({f['offering']}) while burning cash"
        flags.append(f"Prospectus for new securities filed {f['offering']}")
        score = 0.4
    if f.get("unregistered"):
        if burning:
            return False, 0, [], [], f"Issued unregistered shares ({f['unregistered']}) while burning cash"
        flags.append(f"Unregistered share issue reported {f['unregistered']}")
        score = min(score, 0.4)
    if f.get("notice"):
        flags.append(f"SEC filing {f['notice']['date']}: {f['notice']['what']}")
        score = min(score, 0.6)
    if f.get("shelf") and burning:
        flags.append(f"Shelf registration on file since {f['shelf']['date']}: can sell shares at short notice")
        score = min(score, 0.4)
    notes = [] if flags else ["No offering or distress filings with the SEC in the past year"]
    return True, score, notes, flags, None


def news_gate(headlines, burning, days=90):
    """Stage 4: recent headlines about the company. Returns (ok, flags, adjustment, reason)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [h for h in headlines if h["ts"] >= cutoff]
    flags, seen = [], set()
    for h in recent:
        for tag in h.get("tags", []):
            if tag == "distress":
                return False, [], 0, f"Distress in the news: “{h['title'][:90]}”"
            if tag == "dilution" and burning:
                return False, [], 0, f"Raising capital while burning cash: “{h['title'][:90]}”"
            seen.add(tag)
    if "dilution" in seen:
        flags.append("Capital raise in the news")
    if "legal" in seen:
        flags.append("Legal or accounting issue in the news")
    if "trial_setback" in seen:
        flags.append("Trial setback in the news")
    if "guidance_cut" in seen:
        flags.append("Cut its guidance recently")
    adj = max(-6, min(6, sum(NEWS_ADJ.get(t, 0) for t in seen)))
    return True, flags, adj, None
