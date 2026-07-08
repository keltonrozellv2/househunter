"""
Tests for the pipeline math built on top of underwrite.py:
valuation, scoring/vote gates (incl. the BAH governor), offer/concession,
the Stage-1 prescore PITI estimate, and the cache/budget layer.

Run from the project root:
    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import underwrite as uw  # noqa: E402
import valuation  # noqa: E402
import offer as offer_mod  # noqa: E402
import score as score_mod  # noqa: E402
from cache import Cache, BudgetExhausted, cache_key  # noqa: E402
from models import (Analysis, CommuteResult, Enrichment, Listing,  # noqa: E402
                    ValueEstimate)
import main  # noqa: E402


def make_listing(**over) -> Listing:
    base = dict(id="t1", address="1 Test St", city="Grovetown", state="GA",
                zip_code="30813", county="Columbia", list_price=180_000,
                beds=3, baths=2, sqft=1600, year_built=2015,
                property_type="single_family", construction="brick",
                days_on_market=20, latitude=33.45, longitude=-82.20)
    base.update(over)
    return Listing(**base)


# --------------------------------------------------------------------------- #
# Valuation
# --------------------------------------------------------------------------- #

class TestValuation(unittest.TestCase):

    def test_comps_value_median_ppsf(self):
        comps = [{"price": 200_000, "squareFootage": 2000},   # $100/sqft
                 {"price": 330_000, "squareFootage": 3000},   # $110/sqft
                 {"price": 240_000, "squareFootage": 2000}]   # $120/sqft
        # median ppsf = 110; subject 1500 sqft => 165,000
        self.assertAlmostEqual(valuation.comps_value(comps, 1500), 165_000.0)

    def test_comps_value_none_without_sqft(self):
        self.assertIsNone(valuation.comps_value([{"price": 1, "squareFootage": 1}], None))

    def test_point_is_median_of_components(self):
        l = make_listing(list_price=190_000, sqft=None)  # sqft None -> no comps comp
        e = Enrichment(avm_value=200_000, county_appraised_value=210_000)
        v = valuation.estimate_value(l, e)
        self.assertAlmostEqual(v.point, 205_000.0)  # median of two = mean
        self.assertLessEqual(v.low, 200_000)
        self.assertGreaterEqual(v.high, 210_000)

    def test_negative_spread_when_listed_below_value(self):
        l = make_listing(list_price=180_000, sqft=None)
        e = Enrichment(avm_value=200_000)
        v = valuation.estimate_value(l, e)
        self.assertAlmostEqual(v.list_to_value_spread, -0.10)

    def test_no_data_falls_back_to_list_price(self):
        l = make_listing(list_price=150_000)
        v = valuation.estimate_value(l, Enrichment())
        self.assertEqual(v.point, 150_000)
        self.assertIn("LOW", v.confidence)


# --------------------------------------------------------------------------- #
# Offer & concession
# --------------------------------------------------------------------------- #

class TestOffer(unittest.TestCase):

    def _value(self, point, low=None, high=None, spread=None):
        return ValueEstimate(point=point, low=low or point * 0.95,
                             high=high or point * 1.05,
                             list_to_value_spread=spread or 0.0)

    def test_concession_capped_at_4pct(self):
        # closing 3% of 200k = 6000; minus 3000 credit = 3000 -> under the 8000 cap
        v = self._value(200_000)
        o = offer_mod.recommend_offer(200_000, v, 20, 0.03, 3000, 0.04)
        self.assertAlmostEqual(o.concession_dollars, 3000, delta=100)

    def test_concession_hits_cap_when_costs_high(self):
        # closing 6% = 12000 - 0 credit = 12000, but 4% cap = 8000
        v = self._value(200_000)
        o = offer_mod.recommend_offer(200_000, v, 20, 0.06, 0, 0.04)
        self.assertAlmostEqual(o.concession_dollars, o.offer_price * 0.04, delta=100)

    def test_stale_overpriced_anchors_under_list(self):
        v = self._value(190_000, spread=0.05)  # listed 5% over value
        o = offer_mod.recommend_offer(200_000, v, 60, 0.03, 3000, 0.04)
        self.assertLess(o.offer_price, 200_000 * 0.95)

    def test_never_recommends_above_value_range_silently(self):
        v = self._value(200_000, high=205_000, spread=-0.05)
        o = offer_mod.recommend_offer(190_000, v, 5, 0.03, 3000, 0.04)
        self.assertLessEqual(o.offer_price, 205_000)


# --------------------------------------------------------------------------- #
# Scoring & vote gates
# --------------------------------------------------------------------------- #

class _ScoreBase(unittest.TestCase):

    def setUp(self):
        self.cfg = main.load_config()

    def analysis(self, listing=None, rent=1900, value=None, commute_min=18.0):
        l = listing or make_listing()
        value = value or l.list_price * 1.03
        e = Enrichment(rent_estimate=rent, school_district="Columbia County School District")
        a = Analysis(listing=l, enrichment=e)
        a.value = valuation.estimate_value(l, Enrichment(avm_value=value))
        a.commute = CommuteResult(minutes=commute_min, label="test")
        deal = uw.DealInputs(
            purchase_price=l.list_price, market_value=value,
            annual_rate=0.06125, monthly_rent=rent,
            millage_rate=26.5, homestead_exemption_assessed=10_000,
            mgmt_pct=0.0)
        a.underwrite = uw.underwrite(deal)
        return a

    def score(self, a):
        return score_mod.score_analysis(a, self.cfg)


class TestVoteGates(_ScoreBase):

    def test_good_deal_votes_yes(self):
        a = self.analysis(rent=1950)
        s = self.score(a)
        self.assertGreater(a.underwrite.monthly_cash_flow, 0)
        self.assertEqual(s.vote, "YES")

    def test_bah_governor_is_a_hard_no(self):
        # $250k blows past the $1,509 PITI ceiling regardless of score
        l = make_listing(list_price=250_000)
        a = self.analysis(listing=l, rent=2600, value=260_000)
        s = self.score(a)
        self.assertGreater(a.underwrite.piti_owner,
                           self.cfg["buy_box"]["max_monthly_piti"])
        self.assertEqual(s.vote, "NO")
        self.assertTrue(any("BAH" in d for d in s.disqualifiers))

    def test_negative_cashflow_is_a_no(self):
        a = self.analysis(rent=1500)  # rent too low to carry the house
        s = self.score(a)
        self.assertLess(a.underwrite.monthly_cash_flow, 0)
        self.assertEqual(s.vote, "NO")

    def test_manufactured_is_disqualified(self):
        l = make_listing(property_type="manufactured", list_price=95_000)
        a = self.analysis(listing=l, rent=1400, value=100_000)
        s = self.score(a)
        self.assertEqual(s.vote, "NO")
        self.assertTrue(any("manufactured" in d for d in s.disqualifiers))

    def test_commute_over_cap_is_disqualified(self):
        a = self.analysis(commute_min=55.0)
        s = self.score(a)
        self.assertEqual(s.vote, "NO")
        self.assertTrue(any("commute" in d for d in s.disqualifiers))

    def test_subscores_sum_to_total(self):
        a = self.analysis()
        s = self.score(a)
        self.assertAlmostEqual(
            s.total, sum(v["points"] for v in s.subscores.values()), places=0)
        self.assertEqual(sum(v["max"] for v in s.subscores.values()), 100)


# --------------------------------------------------------------------------- #
# Stage-1 prescore PITI estimate — must agree with the verified underwrite math
# --------------------------------------------------------------------------- #

class TestPrescorePiti(_ScoreBase):

    def test_piti_estimate_matches_underwrite(self):
        l = make_listing(list_price=180_000)
        est = main.estimate_piti_from_price(180_000, self.cfg, l)
        ua = self.cfg["underwriting_assumptions"]
        deal = uw.DealInputs(
            purchase_price=180_000, market_value=180_000,
            annual_rate=self.cfg["buyer"]["interest_rate"], monthly_rent=0,
            insurance_annual=ua["insurance_annual"],
            millage_rate=26.5,  # Columbia fallback used by estimate
            homestead_exemption_assessed=ua["homestead_exemption_assessed"])
        r = uw.underwrite(deal)
        self.assertAlmostEqual(est, r.piti_owner, places=2)

    def test_hard_filter_drops_bad_types_and_sizes(self):
        box = self.cfg["buy_box"]
        self.assertFalse(main.hard_filter(make_listing(), box))
        self.assertTrue(main.hard_filter(make_listing(beds=2), box))
        self.assertTrue(main.hard_filter(make_listing(baths=1), box))
        self.assertTrue(main.hard_filter(
            make_listing(property_type="manufactured"), box))
        self.assertTrue(main.hard_filter(
            make_listing(list_price=300_000), box))


# --------------------------------------------------------------------------- #
# Cache & RentCast budget
# --------------------------------------------------------------------------- #

class TestCacheBudget(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.cache = Cache(Path(self.tmp.name))

    def tearDown(self):
        self.cache.close()
        os.unlink(self.tmp.name)

    def test_roundtrip_and_ttl(self):
        self.cache.set("/x", {"a": 1}, {"hello": "world"})
        self.assertEqual(self.cache.get("/x", {"a": 1}, ttl_days=7),
                         {"hello": "world"})
        self.assertIsNone(self.cache.get("/x", {"a": 1}, ttl_days=0))  # expired
        self.assertIsNone(self.cache.get("/x", {"a": 2}, ttl_days=7))  # other params

    def test_key_is_param_order_independent(self):
        self.assertEqual(cache_key("/e", {"a": 1, "b": 2}),
                         cache_key("/e", {"b": 2, "a": 1}))

    def test_budget_hard_stop(self):
        for _ in range(3):
            self.cache.record_call("rentcast")
        self.assertEqual(self.cache.calls_this_month("rentcast"), 3)
        with self.assertRaises(BudgetExhausted):
            self.cache.check_budget(cap=3, warn_at=2)
        # a different API is unaffected
        self.cache.check_budget(cap=3, warn_at=2, api="ors")


if __name__ == "__main__":
    unittest.main(verbosity=2)
