"""Small shared helpers."""
import json, math, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import USER_AGENT


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


def threaded(fn, items, workers=6):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def http_get(url, timeout=25, ua=USER_AGENT):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=25, ua=USER_AGENT):
    return json.loads(http_get(url, timeout, ua))


def grade(v, steps):
    """steps: (threshold, score) pairs checked in order with >=."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    for th, sc in steps:
        if v >= th:
            return sc
    return 0.0


def clean(o):
    """Make a structure JSON-safe: drop private keys, NaN and infinity, round floats."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return round(o, 5)
    return o
