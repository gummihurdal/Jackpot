"""Local analytical database: Parquet files queried with DuckDB.

Layout under data/db/
  quotes/YYYY-MM-DD.parquet   every stock in the universe on that day (point-in-time snapshot)
  scores/YYYY-MM-DD.parquet   every stock that passed the safety gate, with its scores
  news.parquet                headlines collected so far (deduplicated)
  cache/<kind>.parquet        slow-changing data (fundamentals, share history, trials) with fetch dates

No server and no credentials: the files live in the repository and DuckDB reads them in place.
"""
import glob, json, os
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd

from .config import DB, INFO_PRICE_MOVE, NEWS_DAYS


class Store:
    def __init__(self, root=DB):
        self.root = root
        for sub in ("quotes", "scores", "cache"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        self.con = duckdb.connect()

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def _write(self, df, path):
        self.con.register("df_out", df)
        self.con.execute(f"COPY df_out TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        self.con.unregister("df_out")

    def _has(self, sub):
        return bool(glob.glob(self._p(sub, "*.parquet")))

    # ------------------------------------------------------------ cache
    def cache_load(self, kind):
        path = self._p("cache", kind + ".parquet")
        if not os.path.exists(path):
            return {}
        rows = self.con.execute(f"SELECT symbol, fetched, ref_price, payload FROM '{path}'").fetchall()
        return {s: {"d": str(d), "p": p, "v": json.loads(v)} for s, d, p, v in rows}

    def cache_save(self, kind, cache):
        if not cache:
            return
        df = pd.DataFrame([{"symbol": s, "fetched": c["d"], "ref_price": c.get("p"),
                            "payload": json.dumps(c["v"])} for s, c in cache.items()])
        self._write(df, self._p("cache", kind + ".parquet"))

    @staticmethod
    def fresh(entry, ttl_days, price=None):
        if not entry:
            return False
        age = (date.today() - datetime.strptime(entry["d"], "%Y-%m-%d").date()).days
        if age > ttl_days:
            return False
        if price and entry.get("p"):
            return abs(price / entry["p"] - 1) <= INFO_PRICE_MOVE
        return True

    # ------------------------------------------------------------ daily snapshots
    def save_quotes(self, today, metrics):
        df = pd.DataFrame([{"date": today, "symbol": m["symbol"], "country": m["country"], "price": m["price"],
                            "pos": m["pos"], "ratio": m["ratio"], "cap_usd": m["cap_usd"]} for m in metrics])
        self._write(df, self._p("quotes", today + ".parquet"))

    def save_scores(self, today, stocks):
        df = pd.DataFrame([{"date": today, "symbol": m["symbol"], "theme": m["theme"], "price": m["price"],
                            "spring": m["spring"], "quality": m["quality"], "safety": m["safety"],
                            "setup": m["setup"], "potential": m.get("potential")} for m in stocks])
        if len(df):
            self._write(df, self._p("scores", today + ".parquet"))

    # ------------------------------------------------------------ news
    def save_news(self, rows):
        """Merge new headlines into the store, one row per (symbol, url), and drop old ones."""
        path = self._p("news.parquet")
        cols = ["symbol", "ts", "title", "url", "publisher", "source", "tags"]
        new = pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows], columns=cols)
        self.con.register("news_new", new)
        old = f"SELECT * FROM '{path}'" if os.path.exists(path) else "SELECT * FROM news_new WHERE 1=0"
        cutoff = (date.today() - timedelta(days=NEWS_DAYS)).isoformat()
        merged = self.con.execute(f"""
            SELECT symbol, ts, title, url, publisher, source, tags FROM (
                SELECT *, row_number() OVER (PARTITION BY symbol, url ORDER BY ts DESC) AS rn
                FROM ({old} UNION ALL SELECT * FROM news_new))
            WHERE rn = 1 AND ts >= '{cutoff}' ORDER BY ts DESC""").df()
        self.con.unregister("news_new")
        self._write(merged, path)
        return len(merged)

    def news_for(self, symbols):
        """Stored headlines per symbol, newest first."""
        path = self._p("news.parquet")
        if not os.path.exists(path) or not symbols:
            return {}
        self.con.register("want", pd.DataFrame({"symbol": list(symbols)}))
        rows = self.con.execute(f"""SELECT n.symbol, n.ts, n.title, n.url, n.publisher, n.source, n.tags
                                    FROM '{path}' n JOIN want USING (symbol) ORDER BY n.ts DESC""").fetchall()
        self.con.unregister("want")
        out = {}
        for s, ts, title, url, pub, src, tags in rows:
            out.setdefault(s, []).append({"ts": ts, "title": title, "url": url, "publisher": pub,
                                          "source": src, "tags": [t for t in (tags or "").split(",") if t]})
        return out

    # ------------------------------------------------------------ analytics
    def score_moves(self, today):
        """For each stock on today's list: previous score, change, days on the radar, first day seen."""
        if not self._has("scores"):
            return {}
        g = self._p("scores", "*.parquet")
        rows = self.con.execute(f"""
            WITH s AS (SELECT * FROM '{g}'),
                 prev AS (SELECT max(date) AS d FROM s WHERE date < '{today}'),
                 agg AS (SELECT symbol, count(DISTINCT date) AS days_on, min(date) AS first_seen
                         FROM s GROUP BY symbol)
            SELECT a.symbol, a.days_on, a.first_seen, p.spring
            FROM agg a LEFT JOIN s p ON p.symbol = a.symbol AND p.date = (SELECT d FROM prev)""").fetchall()
        return {s: {"days_on": int(n), "first_seen": str(f), "prev_spring": ps} for s, n, f, ps in rows}

    def previous_scan_date(self, today):
        if not self._has("scores"):
            return None
        rows = self.con.execute(
            f"SELECT max(date) FROM '{self._p('scores', '*.parquet')}' WHERE date < '{today}'").fetchall()
        d = rows[0][0] if rows else None
        return str(d) if d else None

    def breadth(self, limit=90):
        """Share of the whole universe that sits in the washed-out zone, per day."""
        if not self._has("quotes"):
            return []
        rows = self.con.execute(f"""
            SELECT date, count(*) AS n, avg(CASE WHEN ratio >= 2 AND pos <= 0.25 THEN 1.0 ELSE 0.0 END) AS washed,
                   median(pos) AS median_pos
            FROM '{self._p('quotes', '*.parquet')}' GROUP BY date ORDER BY date DESC LIMIT {int(limit)}""").fetchall()
        return [{"date": str(d), "n": int(n), "washed": float(w), "median_pos": float(mp)} for d, n, w, mp in rows][::-1]

    def benchmark(self, since_dates):
        """Median return of the whole universe from each start date to the latest snapshot."""
        if not self._has("quotes") or not since_dates:
            return {}
        g = self._p("quotes", "*.parquet")
        out = {}
        for d0 in sorted(set(since_dates)):
            row = self.con.execute(f"""
                WITH q AS (SELECT * FROM '{g}'),
                     a AS (SELECT symbol, price FROM q WHERE date = (SELECT min(date) FROM q WHERE date >= '{d0}')),
                     b AS (SELECT symbol, price FROM q WHERE date = (SELECT max(date) FROM q))
                SELECT median(b.price / a.price - 1), count(*) FROM a JOIN b USING (symbol)
                WHERE a.price > 0""").fetchall()
            out[d0] = float(row[0][0]) if row and row[0][0] is not None else None
        return out
