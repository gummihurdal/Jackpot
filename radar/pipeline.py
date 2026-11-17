"""The scan, end to end."""
import argparse, json, os, re, time
from datetime import datetime, timezone

from . import config as C
from .gates import cash_gate, filing_gate, news_gate, share_gate
from .history import download_weekly, rebound_dna, stabilisation
from .scores import potential_score, quality_score, safety_score, setup_score, spring_score
from .sources import (classify, edgar_ticker_map, fetch_feeds, fetch_filings, fetch_info, fetch_share_growth,
                      fetch_trials, fetch_yahoo_news, info_is_empty, is_relevant, name_key)
from .store import Store
from .universe import (basic_metrics, dedupe, fetch_universe, fx_rates, repair_ranges, theme_of, width_near)
from .util import clean, log, threaded
from .watch import scan_watchlist


class Timer:
    def __init__(self):
        self.t0 = self.last = time.time()
        self.steps = {}

    def mark(self, name):
        now = time.time()
        self.steps[name] = round(now - self.last, 1)
        self.last = now
        log(f"[{now - self.t0:6.0f}s] {name}")


def cached_fetch(store, kind, items, ttl, fetch, key=lambda m: m["symbol"], arg=lambda m: m["symbol"],
                 keep=lambda v: v is not None, workers=4, use_price=False):
    """Return {symbol: value}, calling fetch() only for entries that are missing or stale."""
    cache = store.cache_load(kind)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stale = [m for m in items if not Store.fresh(cache.get(key(m)), ttl, m["price"] if use_price else None)]
    got = threaded(lambda m: fetch(arg(m)), stale, workers=workers)
    for m, v in zip(stale, got):
        if keep(v):
            cache[key(m)] = {"d": today, "p": m["price"], "v": v}
    # forget entries that have not been needed for two months
    cache = {k: c for k, c in cache.items() if Store.fresh(c, 60)}
    store.cache_save(kind, cache)
    log(f"  {kind}: {len(items) - len(stale)} from cache, {len(stale)} fetched")
    return {key(m): (cache[key(m)]["v"] if key(m) in cache else None) for m in items}, stale


def reject(rejected, m, reason):
    rejected.append({"symbol": m["symbol"], "name": m["name"], "theme": m.get("theme", "Other"), "reason": reason})


def collect_news(store, survivors):
    """Per-company headlines from Yahoo plus the market-wide feeds, filed in the news store."""
    rows = []
    for m, items in zip(survivors, threaded(lambda m: fetch_yahoo_news(m["symbol"]), survivors, workers=4)):
        for h in items:
            if is_relevant(h["title"], m["name"], m["symbol"]):
                rows.append({**h, "symbol": m["symbol"], "tags": ",".join(classify(h["title"]))})
    rows += match_feeds(fetch_feeds(), survivors)
    total = store.save_news(rows) if rows else 0
    log(f"  news: {len(rows)} headlines this run, {total} in the store")
    return store.news_for([m["symbol"] for m in survivors])


def match_feeds(feed, stocks):
    """Attach market-wide feed items to radar stocks: Benzinga by ticker, FDA by company name."""
    us = {m["symbol"].upper(): m for m in stocks if m.get("region") == "us"}
    pharma = [(name_key(m["name"]), m) for m in stocks if m.get("theme") == C.PHARMA_THEME]
    rows = []
    for it in feed:
        tags = ",".join(classify(it["title"]))
        base = {k: it[k] for k in ("ts", "title", "url", "publisher", "source")}
        hit = False
        for t in it.get("tickers", []):
            if t in us:
                rows.append({**base, "symbol": us[t]["symbol"], "tags": tags})
                hit = True
        if it["source"] == "fda":
            low = re.sub(r"[^a-z0-9 ]", " ", it["title"].lower())
            for key, m in pharma:
                if key and key in low:
                    rows.append({**base, "symbol": m["symbol"], "tags": tags})
                    hit = True
        if not hit:
            rows.append({**base, "symbol": "*", "tags": tags})  # kept so later scans can still match it
    return rows


def update_tracker(today, ranked, universe_price, store):
    path = os.path.join(C.DATA, "tracker.json")
    tr = json.load(open(path)) if os.path.exists(path) else {}
    picks = ranked[:C.TRACK_TOP] + [m for m in ranked if m["theme"] == C.PHARMA_THEME][:C.TRACK_TOP_PHARMA]
    for m in picks:
        if m["symbol"] not in tr:
            tr[m["symbol"]] = {
                "name": m["name"], "theme": m["theme"], "country": m["country"], "currency": m["currency"],
                "first_seen": today, "first_price": m["price"], "first_spring": m["spring"],
                "low_at_flag": m["low"], "high_at_flag": m["high"], "rules": C.RULES_VERSION,
            }
    live = {m["symbol"]: m for m in ranked}
    bench = store.benchmark([t["first_seen"] for t in tr.values()])
    for s, t in tr.items():
        p = live[s]["price"] if s in live else universe_price.get(s)
        if p:
            t["last_price"], t["last_date"] = p, today
            t["return"] = p / t["first_price"] - 1
            t["best"] = max(t.get("best", 0.0), t["return"])
            t["worst"] = min(t.get("worst", 0.0), t["return"])
            b = bench.get(t["first_seen"])
            t["bench"] = b
            t["edge"] = t["return"] - b if b is not None else None
        t["on_radar"] = s in live
        t["spring_now"] = live[s]["spring"] if s in live else None
    json.dump(clean(tr), open(path, "w"), indent=1)
    return tr


def rates_all(rates):
    """Watchlist names can sit in any covered country, so make sure every currency has a rate."""
    need = {cur for _, cur in C.REGIONS.values()} - set(rates)
    return {**rates, **fx_rates(need)} if need else rates


def run(regions=None, max_deep=C.MAX_DEEP):
    regions = regions or list(C.REGIONS)
    os.makedirs(os.path.join(C.DATA, "history"), exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store, T = Store(), Timer()

    # 1. universe ------------------------------------------------------------
    rates = fx_rates({C.REGIONS[r][1] for r in regions})
    uni = fetch_universe(regions, rates)
    ph_syms = set(fetch_universe(regions, rates, pharma_only=True))
    metrics = [m for m in (basic_metrics(x, rates) for x in uni.values())
               if m and C.MIN_CAP_USD <= m["cap_usd"] <= C.MAX_CAP_USD and m["dvol_usd"] >= C.MIN_DVOL_USD]
    metrics = repair_ranges(dedupe(metrics))
    store.save_quotes(today, metrics)
    T.mark(f"universe: {len(metrics)} stocks")

    # 2. price pattern -------------------------------------------------------
    hits = [m for m in metrics if m["ratio"] >= C.MIN_RATIO and m["pos"] <= C.MAX_POS]
    # cheap pre-rank: profitable companies first, then width and nearness to the low
    hits.sort(key=lambda m: -((1 if (m["eps_ttm"] or 0) > 0 else 0) + (1 if (m["eps_fwd"] or 0) > 0 else 0)
                              + width_near(m)))
    main_c = hits[:max_deep]
    chosen = {m["symbol"] for m in main_c}
    ph_c = sorted((m for m in metrics if m["symbol"] in ph_syms and m["symbol"] not in chosen
                   and m["ratio"] >= C.PH_MIN_RATIO and m["pos"] <= C.PH_MAX_POS),
                  key=lambda m: -width_near(m))[:C.MAX_DEEP_PHARMA]
    cands = main_c + ph_c
    n_match = len(hits) + len(ph_c)
    log(f"candidates: {len(main_c)} main track + {len(ph_c)} pharma track")

    # 3. deep data -----------------------------------------------------------
    hist = download_weekly([m["symbol"] for m in cands])
    T.mark("price history")
    infos, fetched = cached_fetch(store, "info", cands, C.TTL_INFO, fetch_info,
                                  keep=lambda v: not info_is_empty(v), use_price=True)
    missing = [m for m in fetched if infos.get(m["symbol"]) is None]
    if missing:  # Yahoo sometimes rejects a burst of requests: try those again, one at a time
        cache = store.cache_load("info")
        for m in missing:
            time.sleep(0.3)
            v = fetch_info(m["symbol"])
            if not info_is_empty(v):
                cache[m["symbol"]] = {"d": today, "p": m["price"], "v": v}
                infos[m["symbol"]] = v
        store.cache_save("info", cache)
        log(f"  info: {len(missing)} retried")
    T.mark("fundamentals")

    # 4. safety gate ---------------------------------------------------------
    rejected, passed = [], []
    for m in cands:
        info = infos.get(m["symbol"]) or {}
        m["sector"], m["industry"] = info.get("sector"), (info.get("industry") or "").replace("—", " - ")
        m["theme"] = theme_of(m["sector"], m["industry"])
        ok, parts, notes, reason = cash_gate(m, info)
        if not ok:
            reject(rejected, m, reason)
            continue
        m["_info"], m["_parts"], m["_notes"], m["_flags"] = info, parts, notes, []
        m["_burning"] = "runway_years" in m
        passed.append(m)
    log(f"  stage 1 cash and debt: {len(passed)} pass")

    growth, _ = cached_fetch(store, "shares", passed, C.TTL_SHARES, fetch_share_growth,
                             keep=lambda v: v is not None)
    stage2 = []
    for m in passed:
        g = growth.get(m["symbol"])
        ok, sc, note, reason = share_gate(g)
        m["share_growth_3y"] = g
        if not ok:
            reject(rejected, m, reason)
            continue
        m["_parts"]["shares"] = sc
        if note:
            m["_notes"].append(note)
        stage2.append(m)
    log(f"  stage 2 dilution record: {len(stage2)} pass")
    T.mark("share history")

    tmap = edgar_ticker_map()
    us = [m for m in stage2 if m["region"] == "us" and m["symbol"].upper() in tmap]
    filings = dict(zip([m["symbol"] for m in us],
                       threaded(lambda m: fetch_filings(tmap[m["symbol"].upper()]), us, workers=3)))
    stage3 = []
    for m in stage2:
        ok, sc, notes, flags, reason = filing_gate(filings.get(m["symbol"]), m["_burning"])
        if not ok:
            reject(rejected, m, reason)
            continue
        if sc is not None:
            m["_parts"]["filings"] = sc
        m["_notes"] += notes
        m["_flags"] += flags
        stage3.append(m)
    log(f"  stage 3 SEC filings ({len(us)} US-listed checked): {len(stage3)} pass")
    T.mark("SEC filings")

    news = collect_news(store, stage3)
    survivors = []
    for m in stage3:
        heads = news.get(m["symbol"], [])
        ok, flags, adj, reason = news_gate(heads, m["_burning"])
        if not ok:
            reject(rejected, m, reason)
            continue
        m["_flags"] += flags
        m["news_adj"] = adj
        m["news"] = [{k: h[k] for k in ("ts", "title", "url", "publisher", "tags")} for h in heads[:3]]
        survivors.append(m)
    log(f"  stage 4 news: {len(survivors)} pass")
    T.mark("news")

    # 5. pharma pipeline -----------------------------------------------------
    pharma = [m for m in survivors if m["theme"] == C.PHARMA_THEME]
    trials, _ = cached_fetch(store, "trials", pharma, C.TTL_TRIALS, fetch_trials, arg=lambda m: m["name"])
    T.mark(f"clinical trials for {len(pharma)} pharma names")

    # 6. scores --------------------------------------------------------------
    now_ts = time.time()
    out = []
    for m in survivors:
        info, parts = m.pop("_info"), m.pop("_parts")
        notes, flags = m.pop("_notes"), m.pop("_flags")
        m.pop("_burning")
        closes = hist.get(m["symbol"]) or []
        dna = rebound_dna(closes) if len(closes) >= 104 else None
        stab = stabilisation(closes) if closes else None
        q, s, safety = quality_score(m, info), setup_score(m, dna, stab), safety_score(parts)
        structural = bool(dna and dna["open"] and dna["open_weeks"] > 156)
        if structural:
            flags.append("Below its old peak for 3+ years")
        if stab and stab["state"] == "falling":
            flags.append("Made a new low within the last week")
        if len(closes) < 104:
            flags.append("Less than 2 years of price history")
        if 0.10 < (m.get("share_growth_3y") or 0) <= C.MAX_SHARE_GROWTH_3Y:
            flags.append("Share count rising")
        pot = tr = None
        if m["theme"] == C.PHARMA_THEME:
            tr = trials.get(m["symbol"])
            pot = potential_score(m, info, tr)
            m["potential"], m["trials"] = pot, tr
        ne = m.get("next_earnings")
        m["next_earnings"] = (datetime.fromtimestamp(ne, timezone.utc).strftime("%Y-%m-%d")
                              if ne and ne > now_ts else None)
        m.update({
            "roe": info.get("returnOnEquity"), "margin": info.get("profitMargins"),
            "gross_margin": info.get("grossMargins"), "rev_growth": info.get("revenueGrowth"),
            "rec": info.get("recommendationMean"), "analysts": info.get("numberOfAnalystOpinions"),
            "target": info.get("targetMeanPrice"),
            "dna": dna, "stab": stab, "safety_notes": notes, "flags": flags,
            "quality": q, "setup": s, "safety": safety,
            "spring": spring_score(q, safety, s, pot, m.get("news_adj", 0), structural),
            "spark": [round(v, 4) for v in closes[-104::2]],
        })
        out.append(m)
    out.sort(key=lambda m: -m["spring"])

    # 7. memory and analytics ------------------------------------------------
    prev_date = store.previous_scan_date(today)
    store.save_scores(today, out)
    moves = store.score_moves(today)
    for m in out:
        mv = moves.get(m["symbol"], {})
        m["days_on"] = mv.get("days_on", 1)
        m["prev_spring"] = mv.get("prev_spring")
        m["is_new"] = bool(prev_date) and mv.get("first_seen") == today
    tracker = update_tracker(today, out, {m["symbol"]: m["price"] for m in metrics}, store)
    T.mark("scores and analytics")
    watch = scan_watchlist(rates_all(rates), tmap)
    T.mark(f"watchlist: {len(watch)} names")

    by_region, reasons = {}, {}
    for m in metrics:
        by_region[m["country"]] = by_region.get(m["country"], 0) + 1
    for r in rejected:
        k = re.sub(r"“.*", "", re.sub(r"\(?\d{4}-\d{2}-\d{2}\)?", "", r["reason"]))
        k = re.sub(r"\d[\d.,%]*", "N", k).strip(" :")
        reasons[k] = reasons.get(k, 0) + 1
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "date": today,
        "rules_version": C.RULES_VERSION, "previous_scan": prev_date,
        "universe": len(metrics), "matches": n_match, "scanned": len(cands),
        "passed": len(out), "rejected": len(rejected), "reject_reasons": reasons,
        "countries": len(by_region), "by_country": by_region,
        "breadth": store.breadth(), "timings": T.steps, "runtime_seconds": round(time.time() - T.t0),
    }
    json.dump(clean({"meta": meta, "stocks": out, "watch": watch, "tracker": tracker, "rejected": rejected}),
              open(os.path.join(C.DATA, "latest.json"), "w"), separators=(",", ":"))
    slim = [{k: m.get(k) for k in ("symbol", "name", "theme", "country", "price", "currency", "pos", "ratio",
                                    "quality", "safety", "setup", "potential", "spring")} for m in out[:100]]
    json.dump(clean({"meta": {k: v for k, v in meta.items() if k not in ("breadth", "by_country")}, "top": slim}),
              open(os.path.join(C.DATA, "history", today + ".json"), "w"), separators=(",", ":"))
    log(f"Done in {meta['runtime_seconds']}s: {len(out)} listed, {len(rejected)} rejected. Top 12:")
    for m in out[:12]:
        log(f"  {m['spring']:>3}  {m['symbol']:<10} {m['name'][:32]:<32} {m['theme']:<17} "
            f"x{m['ratio']:.1f} pos {m['pos']:.0%} Q{m['quality']} Sa{m['safety']} Se{m['setup']}")
    return meta


def main():
    ap = argparse.ArgumentParser(description="Spring Radar scan")
    ap.add_argument("--regions", default="", help="comma-separated country codes for a quick test, e.g. no,se")
    ap.add_argument("--max-deep", type=int, default=C.MAX_DEEP)
    a = ap.parse_args()
    run([r for r in a.regions.split(",") if r] or None, a.max_deep)
