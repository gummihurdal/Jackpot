"""Outside data: fundamentals, share-count history, SEC filings, clinical trials, news."""
import re, time, urllib.parse, xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import yfinance as yf

from .config import CONTACT
from .universe import norm_name
from .util import http_get, http_json, log, retry

# ---------------------------------------------------------------- Yahoo fundamentals

INFO_KEYS = ("sector", "industry", "totalCash", "totalDebt", "freeCashflow", "operatingCashflow", "ebitda",
             "currentRatio", "returnOnEquity", "profitMargins", "grossMargins", "revenueGrowth",
             "totalRevenue", "numberOfAnalystOpinions", "recommendationMean", "targetMeanPrice")


def fetch_info(sym):
    """Only the fields the radar uses, so the cache stays small."""
    try:
        raw = retry(lambda: yf.Ticker(sym).info, tries=3, wait=1.5) or {}
    except Exception:
        raw = {}
    return {k: raw.get(k) for k in INFO_KEYS if raw.get(k) is not None}


def info_is_empty(info):
    return not info.get("sector") and info.get("totalCash") is None


def fetch_share_growth(sym):
    """Share count change over about 3 years, corrected for stock splits. None when unknown."""
    try:
        t = yf.Ticker(sym)
        sh = t.get_shares_full(start=date.today() - timedelta(days=365 * 3 + 30)).dropna()
        if len(sh) < 4:
            return None
        first, last = float(sh.iloc[:3].median()), float(sh.iloc[-3:].median())
        if first <= 0:
            return None
        g = last / first - 1
        if abs(g) > 0.2:
            # the raw series is not split-adjusted: divide out any split since the first sample
            start, factor = sh.index[1].tz_localize(None), 1.0
            for when, ratio in t.splits.items():
                if when.tz_localize(None) >= start and ratio and ratio > 0:
                    factor *= float(ratio)
            g = (1 + g) / factor - 1
        return g
    except Exception:
        return None


# ---------------------------------------------------------------- company name keys

_GENERIC = {"first", "american", "united", "national", "china", "global", "new", "pt", "general", "international",
            "pacific", "western", "eastern", "northern", "southern", "great", "royal", "bank", "banco", "grupo",
            "shanghai", "beijing", "shenzhen", "hong", "japan", "korea", "india", "advanced", "applied", "smart"}


def name_key(name):
    """A distinctive lower-case phrase that identifies the company in a headline or sponsor field."""
    words = norm_name(name).split()
    if not words:
        return None
    if words[0] in _GENERIC or len(words[0]) < 4:
        return " ".join(words[:2]) if len(words) >= 2 else None
    return words[0]


# ---------------------------------------------------------------- ClinicalTrials.gov

_ACTIVE = "RECRUITING,ACTIVE_NOT_RECRUITING,ENROLLING_BY_INVITATION,NOT_YET_RECRUITING"


def sponsor_query(name):
    c = re.sub(r"\(.*?\)", " ", name)
    c = re.sub(r"[,./]", " ", c)
    c = re.sub(r"\b(Inc|Incorporated|Corp|Corporation|Ltd|Limited|plc|PLC|AG|SA|S A|NV|N V|AB|A S|ASA|Co|Company|"
               r"Holdings|Holding|Group|SE|Oyj|publ|ADR|Class [AB]|KGaA|GmbH|SpA|S p A)\b", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def fetch_trials(name):
    """Active Phase 3 / Phase 2 trials the company leads, and the next expected Phase 3 readout."""
    q, key = sponsor_query(name), name_key(name)
    if len(q) < 4 or not key:
        return None
    out, today = {}, date.today().strftime("%Y-%m")
    for ph in ("3", "2"):
        url = "https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode({
            "query.lead": q, "filter.overallStatus": _ACTIVE, "aggFilters": "phase:" + ph, "pageSize": "100",
            "fields": "NCTId,Condition,PrimaryCompletionDate,LeadSponsorName"})
        try:
            studies = http_json(url, ua="Mozilla/5.0 (spring-radar)").get("studies", [])
        except Exception:
            return None
        mine = []
        for s in studies:
            p = s.get("protocolSection", {})
            lead = (p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name") or "").lower()
            if key in re.sub(r"[^a-z0-9 ]", " ", lead):
                mine.append(p)
        out["p" + ph] = len(mine)
        if ph == "3":
            nxt = None
            for p in mine:
                d = (p.get("statusModule", {}).get("primaryCompletionDateStruct") or {}).get("date")
                if d and d[:7] >= today and (nxt is None or d < nxt["date"]):
                    cond = (p.get("conditionsModule", {}).get("conditions") or [""])[0]
                    nxt = {"date": d[:7], "nct": p["identificationModule"]["nctId"], "condition": cond[:70]}
            out["next_p3"] = nxt
    return out


# ---------------------------------------------------------------- SEC EDGAR

_SEC_UA = "spring-radar " + CONTACT
_OFFERING = {"424B5", "424B4"}
_SHELF = {"S-3", "S-3ASR", "F-3", "F-3ASR", "S-1", "F-1"}
_LATE = {"NT 10-K", "NT 10-Q", "NT 20-F"}
_BAD_ITEMS = {"1.03": "bankruptcy or receivership", "4.02": "past financial statements withdrawn"}
# these are often harmless (a move to another exchange, an early bond redemption), so they only warn
_WARN_ITEMS = {"3.01": "exchange listing notice or transfer", "2.04": "debt repayment triggered early"}


def edgar_ticker_map():
    try:
        d = http_json("https://www.sec.gov/files/company_tickers.json", ua=_SEC_UA, timeout=40)
        return {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}
    except Exception as e:
        log("EDGAR ticker map failed:", e)
        return {}


def fetch_filings(cik):
    """Dilution and distress signals from the last year of SEC filings."""
    try:
        time.sleep(0.12)  # stay well inside the SEC limit of 10 requests a second
        r = http_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua=_SEC_UA)["filings"]["recent"]
    except Exception:
        return None
    d180 = (date.today() - timedelta(days=180)).isoformat()
    d365 = (date.today() - timedelta(days=365)).isoformat()
    out = {"offering": None, "shelf": None, "unregistered": None, "distress": None, "late": None, "notice": None}
    for form, when, items in zip(r.get("form", []), r.get("filingDate", []), r.get("items", [])):
        if when < d365:
            break  # newest first
        if form in _OFFERING and when >= d180 and not out["offering"]:
            out["offering"] = when
        elif form in _SHELF and not out["shelf"]:
            out["shelf"] = {"date": when, "form": form}
        elif form in _LATE and not out["late"]:
            out["late"] = when
        elif form == "8-K" and items:
            its = set(items.split(","))
            if "3.02" in its and when >= d180 and not out["unregistered"]:
                out["unregistered"] = when
            for code, what in _BAD_ITEMS.items():
                if code in its and not out["distress"]:
                    out["distress"] = {"date": when, "what": what}
            for code, what in _WARN_ITEMS.items():
                if code in its and not out["notice"]:
                    out["notice"] = {"date": when, "what": what}
    return out


# ---------------------------------------------------------------- news

_RULES = [
    ("dilution", r"(public offering|registered direct|private placement|at-the-market|\batm (offering|program)|"
                 r"pric(es|ing)( of)? .{0,40}offering|secondary offering|follow-on offering|convertible (senior )?notes|"
                 r"rights (issue|offering)|share placement|placing of (new )?shares|capital raise|equity raise|"
                 r"offering of (common|ordinary) (stock|shares))"),
    ("distress", r"(chapter 11|bankrupt|going concern|insolven|defaults? on|debt restructuring|creditor protection|"
                 r"delisting notice|nasdaq (deficiency|non-?compliance)|notice of non-?compliance|forced liquidation)"),
    ("legal", r"(securities fraud|short[- ]seller report|sec (charges|probe|investigation|subpoena)|"
              r"accounting irregularit|restat(es|ed|ement of) |fraud (allegation|charges|probe))"),
    ("trial_setback", r"(complete response letter|\bcrl\b|clinical hold|fails? to meet|did not meet|"
                      r"miss(es|ed) (its |the )?primary endpoint|discontinu(es|ed|ing) .{0,30}(trial|study|program|development)|"
                      r"(halts?|terminates?|stops?) .{0,30}(trial|study))"),
    ("guidance_cut", r"((cuts?|lowers?|slashes|withdraws?|trims?) (its )?(full[- ]year |annual |fy ?\d* ?)?"
                     r"(guidance|outlook|forecast)|profit warning)"),
    ("downgrade", r"\bdowngrad(e|es|ed)\b"),
    ("buyback", r"(buyback|share repurchase|repurchase program|repurchase of (its )?shares)"),
    ("trial_win", r"(fda approv|approval from the fda|wins? (fda|eu|ema|european) (approval|nod)|"
                  r"positive (topline|top-line|phase|pivotal|interim)|met (its |the )?primary endpoint|"
                  r"breakthrough therapy|priority review|chmp (positive|recommend)|ema (approv|recommend))"),
    ("guidance_raise", r"((raises?|lifts?|boosts?|hikes?|increases?) (its )?(full[- ]year |annual |fy ?\d* ?)?"
                       r"(guidance|outlook|forecast))"),
    ("insider_buying", r"(insiders? (buy|bought|purchas)|(director|ceo|chairman|cfo) (buys|bought|purchases))"),
    ("upgrade", r"\bupgrad(e|es|ed)\b"),
]
_RULES = [(tag, re.compile(rx)) for tag, rx in _RULES]
_LAWYER_SPAM = re.compile(r"(investor alert|shareholder alert|class action|law (firm|offices)|deadline|"
                          r"investigation on behalf|rosen|pomerantz|levi & korsinsky|bragar|faruqi|glancy)")
NEWS_ADJ = {"buyback": 3, "trial_win": 3, "guidance_raise": 2, "insider_buying": 2, "upgrade": 1,
            "downgrade": -2, "guidance_cut": -3, "trial_setback": -4, "legal": -3}


def classify(title):
    t = (title or "").lower()
    if _LAWYER_SPAM.search(t):
        return ["lawyer_notice"]
    return [tag for tag, rx in _RULES if rx.search(t)]


def is_relevant(title, name, symbol):
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    key = name_key(name)
    if key and key in t:
        return True
    root = symbol.split(".")[0].lower()
    return len(root) >= 3 and root.isalpha() and re.search(rf"\b{re.escape(root)}\b", t) is not None


def fetch_yahoo_news(sym):
    """Latest headlines Yahoo attaches to a ticker: (iso time, title, publisher, url)."""
    try:
        raw = yf.Ticker(sym).news or []
    except Exception:
        return []
    out = []
    for it in raw:
        c = it.get("content") or it
        title, when = c.get("title"), c.get("pubDate")
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url"))
        if title and when and url:
            out.append({"ts": when[:19] + "Z", "title": title.strip(), "url": url,
                        "publisher": (c.get("provider") or {}).get("displayName") or "", "source": "yahoo"})
    return out


def _rss_items(url, ua="Mozilla/5.0 (spring-radar)"):
    root = ET.fromstring(http_get(url, ua=ua, timeout=30))
    for it in root.iter("item"):
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        yield ts, (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(), it.findtext("description") or ""


def fetch_feeds():
    """Market-wide feeds. Benzinga tags its stories with tickers; FDA press releases are matched by name later."""
    rows = []
    try:
        for ts, title, link, desc in _rss_items("https://www.benzinga.com/feed"):
            tick = sorted(set(re.findall(r'data-ticker="([A-Z][A-Z.\-]{0,6})"', desc)))
            rows.append({"ts": ts, "title": title, "url": link, "publisher": "Benzinga", "source": "benzinga",
                         "tickers": tick})
    except Exception as e:
        log("Benzinga feed failed:", e)
    try:
        for ts, title, link, _ in _rss_items(
                "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"):
            rows.append({"ts": ts, "title": title, "url": link, "publisher": "FDA", "source": "fda", "tickers": []})
    except Exception as e:
        log("FDA feed failed:", e)
    return rows
