"""Signals taken from 10 years of weekly prices."""
import time

import yfinance as yf

from .util import log, retry


def download_weekly(symbols, chunk=40, period="10y"):
    hist = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        try:
            df = retry(lambda: yf.download(part, period=period, interval="1wk", progress=False,
                                           auto_adjust=True, threads=True, group_by="ticker"))
            for s in part:
                try:
                    c = (df[s]["Close"] if len(part) > 1 else df["Close"]).dropna()
                    if hasattr(c, "columns"):
                        c = c.iloc[:, 0]
                    hist[s] = [float(v) for v in c.values]
                except Exception:
                    pass
        except Exception as e:
            log("history chunk failed", e)
        if (i // chunk) % 8 == 7:
            log(f"  history {min(i + chunk, len(symbols))}/{len(symbols)}")
        time.sleep(0.3)
    return hist


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
