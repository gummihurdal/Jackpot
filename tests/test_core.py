"""Offline tests of the rules. Run:  python -m unittest discover -s tests"""
import os, sys, tempfile, unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar.gates import cash_gate, filing_gate, news_gate, share_gate
from radar.history import rebound_dna, stabilisation
from radar.scores import potential_score, quality_score, setup_score, spring_score
from radar.sources import classify, is_relevant, name_key, sponsor_query
from radar.store import Store
from radar.universe import norm_name, theme_of


def stock(**kw):
    m = {"eps_ttm": 1.0, "eps_fwd": 1.2, "pe_fwd": 12, "price": 10.0, "ratio": 3.0, "pos": 0.1, "cap_usd": 1e9}
    m.update(kw)
    return m


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CashGate(unittest.TestCase):
    def test_self_funding_net_cash_passes(self):
        ok, parts, notes, reason = cash_gate(stock(), {"totalCash": 500, "totalDebt": 100, "freeCashflow": 50,
                                                       "ebitda": 80, "currentRatio": 2.5})
        self.assertTrue(ok)
        self.assertEqual(parts, {"funding": 1.0, "debt": 1.0, "liquidity": 1.0})

    def test_short_runway_rejected(self):
        m = stock()
        ok, *_, reason = cash_gate(m, {"totalCash": 100, "totalDebt": 0, "freeCashflow": -80})
        self.assertFalse(ok)
        self.assertIn("15 months", reason)

    def test_long_runway_passes_and_marks_burning(self):
        m = stock()
        ok, parts, *_ = cash_gate(m, {"totalCash": 500, "totalDebt": 0, "freeCashflow": -100})
        self.assertTrue(ok)
        self.assertAlmostEqual(m["runway_years"], 5.0)
        self.assertEqual(parts["funding"], 0.9)

    def test_heavy_debt_rejected_but_utilities_get_more_room(self):
        info = {"totalCash": 0, "totalDebt": 500, "freeCashflow": 10, "ebitda": 100}
        self.assertFalse(cash_gate(stock(), dict(info, sector="Industrials"))[0])
        self.assertTrue(cash_gate(stock(), dict(info, sector="Utilities"))[0])

    def test_debt_without_profit_rejected(self):
        ok, *_, reason = cash_gate(stock(), {"totalCash": 10, "totalDebt": 500, "freeCashflow": 5, "ebitda": -3})
        self.assertFalse(ok)

    def test_missing_data_rejected(self):
        self.assertFalse(cash_gate(stock(), {})[0])

    def test_weak_liquidity_rejected(self):
        self.assertFalse(cash_gate(stock(), {"totalCash": 100, "totalDebt": 0, "freeCashflow": 5,
                                             "currentRatio": 0.5})[0])

    def test_financials_judged_on_profit(self):
        self.assertTrue(cash_gate(stock(), {"sector": "Financial Services", "returnOnEquity": 0.14})[0])
        self.assertFalse(cash_gate(stock(), {"sector": "Financial Services", "returnOnEquity": 0.02})[0])
        self.assertFalse(cash_gate(stock(eps_ttm=-1), {"sector": "Financial Services", "returnOnEquity": 0.2})[0])


class ShareGate(unittest.TestCase):
    def test_thresholds(self):
        self.assertFalse(share_gate(0.25)[0])
        self.assertFalse(share_gate(8.0)[0])
        self.assertTrue(share_gate(0.19)[0])
        self.assertEqual(share_gate(0.15)[1], 0.3)
        self.assertEqual(share_gate(0.0)[1], 0.85)
        self.assertEqual(share_gate(-0.08)[1], 1.0)
        self.assertEqual(share_gate(None)[1], 0.5)


class FilingGate(unittest.TestCase):
    base = {"offering": None, "shelf": None, "unregistered": None, "distress": None, "late": None}

    def test_clean(self):
        ok, sc, notes, flags, _ = filing_gate(dict(self.base), burning=True)
        self.assertTrue(ok and sc == 1.0 and notes and not flags)

    def test_no_data_is_neutral(self):
        self.assertEqual(filing_gate(None, True)[:2], (True, None))

    def test_offering_rejects_only_cash_burners(self):
        f = dict(self.base, offering="2026-09-01")
        self.assertFalse(filing_gate(f, burning=True)[0])
        ok, sc, _, flags, _ = filing_gate(f, burning=False)
        self.assertTrue(ok and sc == 0.4 and flags)

    def test_shelf_flags_cash_burners_only(self):
        f = dict(self.base, shelf={"date": "2026-05-12", "form": "S-3"})
        self.assertTrue(filing_gate(f, True)[3])
        self.assertFalse(filing_gate(f, False)[3])

    def test_listing_notice_only_warns(self):
        ok, sc, _, flags, _ = filing_gate(dict(self.base, notice={"date": "2026-03-25", "what": "x"}), False)
        self.assertTrue(ok and sc == 0.6 and flags)

    def test_distress_and_late_reject(self):
        self.assertFalse(filing_gate(dict(self.base, distress={"date": "2026-08-01", "what": "x"}), False)[0])
        self.assertFalse(filing_gate(dict(self.base, late="2026-08-01"), False)[0])


class News(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("Acme Prices $150 Million Public Offering of Common Stock"), ["dilution"])
        self.assertEqual(classify("Foo Announces $500M Share Repurchase Program"), ["buyback"])
        self.assertEqual(classify("Bar receives Complete Response Letter from FDA"), ["trial_setback"])
        self.assertEqual(classify("Company files for Chapter 11"), ["distress"])
        self.assertEqual(classify("Qux raises full-year guidance after strong quarter"), ["guidance_raise"])
        self.assertEqual(classify("INVESTOR ALERT: Rosen Law Firm reminds Baz investors"), ["lawyer_notice"])
        self.assertEqual(classify("Quarterly results in line with expectations"), [])

    def test_relevance(self):
        self.assertTrue(is_relevant("Sterling Sees Nearly $8B Scope of Work", "Sterling Infrastructure, Inc.", "STRL"))
        self.assertFalse(is_relevant("RPM Banks On Coatings Growth", "Sterling Infrastructure, Inc.", "STRL"))
        self.assertTrue(is_relevant("Why STRL stock fell today", "Sterling Infrastructure, Inc.", "STRL"))

    def test_gate(self):
        dil = [{"ts": iso(5), "title": "X prices public offering", "tags": ["dilution"]}]
        self.assertFalse(news_gate(dil, burning=True)[0])
        ok, flags, adj, _ = news_gate(dil, burning=False)
        self.assertTrue(ok and flags)
        self.assertFalse(news_gate([{"ts": iso(3), "title": "X files for Chapter 11", "tags": ["distress"]}], False)[0])
        old = [{"ts": iso(200), "title": "X files for Chapter 11", "tags": ["distress"]}]
        self.assertTrue(news_gate(old, False)[0])
        good = [{"ts": iso(2), "title": "buyback", "tags": ["buyback"]}, {"ts": iso(3), "title": "x", "tags": ["trial_win"]}]
        self.assertEqual(news_gate(good, False)[2], 6)


class Names(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(name_key("Sterling Infrastructure, Inc."), "sterling")
        self.assertEqual(name_key("China Hongqiao Group Limited"), "china hongqiao")
        self.assertEqual(sponsor_query("Zealand Pharma A/S"), "Zealand Pharma")
        self.assertEqual(norm_name("Nokia Oyj"), "nokia")

    def test_themes(self):
        self.assertEqual(theme_of("Healthcare", "Biotechnology"), "Pharma & Biotech")
        self.assertEqual(theme_of("Healthcare", "Drug Manufacturers—General"), "Pharma & Biotech")
        self.assertEqual(theme_of("Industrials", "Aerospace & Defense"), "Defence")
        self.assertEqual(theme_of("Energy", "Uranium"), "Mining")
        self.assertEqual(theme_of("Energy", "Oil & Gas E&P"), "Oil & Gas")


class History(unittest.TestCase):
    def test_rebound_dna_counts_full_recoveries(self):
        closes = [100, 60, 50, 70, 95, 100, 120, 70, 60, 115, 130]
        d = rebound_dna(closes)
        self.assertEqual(d["recovered"], 2)
        self.assertFalse(d["open"])

    def test_open_drawdown(self):
        d = rebound_dna([100] + [40] * 200)
        self.assertTrue(d["open"] and d["open_weeks"] == 200 and d["recovered"] == 0)

    def test_stabilisation_states(self):
        self.assertEqual(stabilisation([100] * 30 + [50])["state"], "falling")
        self.assertEqual(stabilisation([100] * 30 + [50, 51, 53, 56, 60])["state"], "turning")
        self.assertEqual(stabilisation([100] * 30 + [50, 50.5, 51, 50.6])["state"], "basing")


class Scores(unittest.TestCase):
    def test_bounds_and_weights(self):
        self.assertEqual(spring_score(100, 100, 100), 100)
        self.assertEqual(spring_score(0, 0, 0, news_adj=-6), 0)
        self.assertEqual(spring_score(80, 80, 80, potential=80), 80)
        self.assertEqual(spring_score(80, 80, 80, structural=True), 68)

    def test_quality_prefers_strong_company(self):
        strong = quality_score(stock(), {"returnOnEquity": 0.25, "profitMargins": 0.2, "grossMargins": 0.6,
                                         "revenueGrowth": 0.2, "freeCashflow": 5, "numberOfAnalystOpinions": 8,
                                         "recommendationMean": 1.8, "targetMeanPrice": 16})
        weak = quality_score(stock(eps_ttm=-1, eps_fwd=-1, pe_fwd=None),
                             {"returnOnEquity": -0.2, "profitMargins": -0.3, "grossMargins": 0.1,
                              "revenueGrowth": -0.1, "freeCashflow": -5})
        self.assertGreater(strong, 85)
        self.assertLess(weak, 25)

    def test_setup_rewards_wide_range_near_low(self):
        good = setup_score(stock(ratio=4, pos=0.02), {"recovered": 3, "open": True, "open_weeks": 20}, {"state": "turning"})
        poor = setup_score(stock(ratio=2, pos=0.25), {"recovered": 0, "open": True, "open_weeks": 300}, {"state": "falling"})
        self.assertGreater(good, 90)
        self.assertLess(poor, 35)

    def test_pharma_potential_uses_pipeline(self):
        soon = (date.today() + timedelta(days=150)).strftime("%Y-%m")
        rich = potential_score(stock(upside=0.8), {"totalRevenue": 4e8, "grossMargins": 0.8, "revenueGrowth": 0.3,
                                                   "numberOfAnalystOpinions": 6, "recommendationMean": 1.6},
                               {"p3": 3, "p2": 4, "next_p3": {"date": soon}})
        thin = potential_score(stock(), {}, {"p3": 0, "p2": 0, "next_p3": None})
        self.assertGreater(rich, 90)
        self.assertLess(thin, 40)


class Database(unittest.TestCase):
    def test_cache_news_and_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(tmp)
            today = date.today().isoformat()
            s.cache_save("info", {"AAA": {"d": today, "p": 10.0, "v": {"sector": "Energy"}}})
            c = s.cache_load("info")
            self.assertEqual(c["AAA"]["v"]["sector"], "Energy")
            self.assertTrue(Store.fresh(c["AAA"], 5, price=10.5))
            self.assertFalse(Store.fresh(c["AAA"], 5, price=13.0))      # price moved too far
            old = {"d": (date.today() - timedelta(days=9)).isoformat(), "p": 10.0, "v": 1}
            self.assertFalse(Store.fresh(old, 5))

            row = {"symbol": "AAA", "ts": iso(1), "title": "t", "url": "u1", "publisher": "p", "source": "x", "tags": "buyback"}
            s.save_news([row, dict(row), dict(row, url="u2"), dict(row, url="u3", ts=iso(400))])
            got = s.news_for(["AAA"])["AAA"]
            self.assertEqual(len(got), 2)                                # duplicate and stale rows dropped
            self.assertEqual(got[0]["tags"], ["buyback"])

            def q(p1, p2):
                return [{"symbol": "AAA", "country": "X", "price": p1, "pos": 0.1, "ratio": 3.0, "cap_usd": 1e9},
                        {"symbol": "BBB", "country": "X", "price": p2, "pos": 0.9, "ratio": 1.2, "cap_usd": 1e9}]
            s.save_quotes("2026-01-01", q(10, 10))
            s.save_quotes("2026-01-02", q(12, 11))
            self.assertAlmostEqual(s.benchmark(["2026-01-01"])["2026-01-01"], 0.15)
            self.assertAlmostEqual(s.breadth()[-1]["washed"], 0.5)

            sc = lambda sp: [{"symbol": "AAA", "theme": "T", "price": 1, "spring": sp, "quality": 1, "safety": 1, "setup": 1}]
            s.save_scores("2026-01-01", sc(70))
            s.save_scores("2026-01-02", sc(75))
            mv = s.score_moves("2026-01-02")["AAA"]
            self.assertEqual((mv["days_on"], mv["prev_spring"], mv["first_seen"]), (2, 70, "2026-01-01"))
            self.assertEqual(s.previous_scan_date("2026-01-02"), "2026-01-01")
            # a read must never block the write that follows it
            s.save_scores("2026-01-03", sc(80))
            self.assertEqual(s.previous_scan_date("2026-01-04"), "2026-01-03")
            s.save_quotes("2026-01-03", q(13, 12))
            self.assertIsNotNone(s.benchmark(["2026-01-01"])["2026-01-01"])
            s.cache_save("info", {"BBB": {"d": today, "p": 1.0, "v": {}}})


if __name__ == "__main__":
    unittest.main()
