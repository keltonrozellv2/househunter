"""
Hand-verified unit tests for underwrite.py.

Run from the project root:
    python -m unittest discover -s tests

Each test anchors on a number you can check with a calculator, so you can trust
the engine before Claude Code builds anything on top of it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import underwrite as uw  # noqa: E402


class TestLoanBasis(unittest.TestCase):

    def test_pi_known_value(self):
        # $100k @ 6% / 30yr is the textbook $599.55 monthly P&I
        self.assertAlmostEqual(uw.monthly_pi(100_000, 0.06, 30), 599.55, places=2)

    def test_pi_zero_loan(self):
        self.assertEqual(uw.monthly_pi(0, 0.06, 30), 0.0)

    def test_pi_zero_rate(self):
        # 0% => straight-line: 120000 / 360 = 333.33...
        self.assertAlmostEqual(uw.monthly_pi(120_000, 0.0, 30), 333.3333, places=3)

    def test_funding_fee_rolled_in(self):
        # 250,000 * 2.15% = 5,375 financed
        self.assertEqual(uw.loan_amount(250_000, 0.0215, False), 255_375.0)

    def test_funding_fee_waived_when_exempt(self):
        self.assertEqual(uw.loan_amount(250_000, 0.0215, True), 250_000.0)


class TestGeorgiaTax(unittest.TestCase):

    def test_assessment_and_millage(self):
        # 250k * 40% = 100k assessed; 30 mills => 100000 * 30 / 1000 = 3000
        self.assertAlmostEqual(
            uw.ga_property_tax(250_000, 30.0, 0.0, 0.40), 3000.0, places=2)

    def test_homestead_lowers_tax(self):
        # exemption of 10k off assessed => (100000-10000)*30/1000 = 2700
        self.assertAlmostEqual(
            uw.ga_property_tax(250_000, 30.0, 10_000, 0.40), 2700.0, places=2)

    def test_rental_tax_higher_than_owner(self):
        owner = uw.ga_property_tax(250_000, 30.0, 10_000, 0.40)
        rental = uw.ga_property_tax(250_000, 30.0, 0.0, 0.40)
        self.assertGreater(rental, owner)


class TestRatios(unittest.TestCase):

    def test_cap_rate(self):
        self.assertAlmostEqual(uw.cap_rate(12_000, 200_000), 0.06, places=6)

    def test_dscr(self):
        self.assertAlmostEqual(uw.dscr(12_000, 10_000), 1.2, places=6)

    def test_grm(self):
        self.assertAlmostEqual(uw.grm(200_000, 20_000), 10.0, places=6)

    def test_one_percent_rule(self):
        self.assertAlmostEqual(uw.one_percent_rule(2_000, 200_000), 0.01, places=6)

    def test_coc_returns_none_when_no_cash_in(self):
        # VA $0 down with credits covering closing => near-zero cash => n/a
        self.assertIsNone(uw.cash_on_cash(5_000, cash_invested=0.0))

    def test_coc_computes_with_real_cash(self):
        self.assertAlmostEqual(
            uw.cash_on_cash(3_000, cash_invested=10_000), 0.30, places=6)


class TestUnderwriteIntegration(unittest.TestCase):

    def setUp(self):
        self.deal = uw.DealInputs(
            purchase_price=235_000,
            market_value=242_000,
            annual_rate=0.06125,
            monthly_rent=1_850,
            insurance_annual=1_600,
            millage_rate=30.0,
            homestead_exemption_assessed=10_000,
            lender_credit=3_000,
            seller_concession=4_000,
        )

    def test_loan_includes_funding_fee(self):
        r = uw.underwrite(self.deal)
        self.assertAlmostEqual(r.loan_amount, 235_000 * 1.0215, places=2)

    def test_rental_piti_exceeds_owner_piti(self):
        # losing the homestead exemption raises taxes, so rental PITI is higher
        r = uw.underwrite(self.deal)
        self.assertGreater(r.piti_rental, r.piti_owner)

    def test_negative_spread_when_listed_below_value(self):
        # 235k list vs 242k value => negative spread (a deal)
        r = uw.underwrite(self.deal)
        self.assertLess(r.list_to_value_spread, 0)

    def test_cashflow_screen_flag_is_consistent(self):
        r = uw.underwrite(self.deal, min_cash_flow=0.0)
        self.assertEqual(r.passes_cashflow_screen, r.monthly_cash_flow >= 0.0)

    def test_true_cashflow_below_simple_test(self):
        # reserves should make true cash flow lower than naive rent-minus-PITI
        r = uw.underwrite(self.deal)
        self.assertLess(r.monthly_cash_flow, r.simple_rent_minus_piti)

    def test_noi_excludes_debt_service(self):
        # NOI must be well above cash flow, since NOI ignores the mortgage
        r = uw.underwrite(self.deal)
        self.assertGreater(r.noi_annual, r.monthly_cash_flow * 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
