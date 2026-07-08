"""
underwrite.py — the financial core of the Fort Eisenhower deal-scanner.

Pure functions, standard-library only (no external deps) so you can sanity-check
the numbers before Claude Code builds the rest of the tool. Run the tests with:

    python -m unittest discover -s tests

Everything here uses REAL investor conventions:
  - VA $0-down loan with a financeable funding fee
  - Georgia's 40% assessment ratio + homestead exemption (lost when you rent it out)
  - NOI excludes debt service AND capex (standard); buyer cash flow includes them
  - cap rate, cash-on-cash, DSCR, GRM, 1% rule

Nothing here is financial or legal advice — it's decision-support math.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Loan basis
# --------------------------------------------------------------------------- #

def loan_amount(purchase_price: float,
                va_funding_fee_pct: float = 0.0215,
                disability_exempt: bool = False) -> float:
    """VA is $0 down, but the funding fee is rolled into the loan.

    First-use funding fee is 2.15% of the loan; waived (0%) if the buyer has a
    qualifying service-connected disability.
    """
    fee_pct = 0.0 if disability_exempt else va_funding_fee_pct
    financed_fee = purchase_price * fee_pct
    return purchase_price + financed_fee


def monthly_pi(loan: float, annual_rate: float, term_years: int = 30) -> float:
    """Monthly principal + interest via standard amortization."""
    if loan <= 0:
        return 0.0
    r = annual_rate / 12.0
    n = term_years * 12
    if r == 0:
        return loan / n
    factor = (1 + r) ** n
    return loan * r * factor / (factor - 1)


# --------------------------------------------------------------------------- #
# Georgia property tax
# --------------------------------------------------------------------------- #

def ga_property_tax(market_value: float,
                    millage_rate: float,
                    homestead_exemption_assessed: float = 0.0,
                    assessment_ratio: float = 0.40) -> float:
    """Annual GA property tax.

    Georgia assesses at 40% of fair market value. Millage is charged per $1,000
    of taxable (assessed) value. The homestead exemption reduces the assessed
    value — but ONLY while the home is your primary residence. Pass
    homestead_exemption_assessed=0 for the rental phase.
    """
    assessed = market_value * assessment_ratio
    taxable = max(0.0, assessed - homestead_exemption_assessed)
    return taxable * millage_rate / 1000.0


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

@dataclass
class DealInputs:
    purchase_price: float
    market_value: float               # est. value (AVM/comps), for value-spread & tax
    annual_rate: float
    monthly_rent: float               # RentCast rent AVM

    # loan
    term_years: int = 30
    va_funding_fee_pct: float = 0.0215
    disability_exempt: bool = False

    # carrying costs
    insurance_annual: float = 1600.0
    hoa_monthly: float = 0.0

    # Georgia tax
    millage_rate: float = 30.0
    homestead_exemption_assessed: float = 0.0   # applied only while owner-occupied
    assessment_ratio: float = 0.40

    # rental assumptions (fractions of gross rent)
    vacancy_pct: float = 0.08
    maintenance_pct: float = 0.10
    capex_pct: float = 0.08
    mgmt_pct: float = 0.09

    # cash to close
    closing_costs_pct: float = 0.03
    lender_credit: float = 3000.0
    seller_concession: float = 0.0


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class UnderwriteResult:
    loan_amount: float
    monthly_pi: float
    annual_debt_service: float

    # owner-occupied phase (you living there now)
    tax_owner_annual: float
    piti_owner: float

    # rental-conversion phase (homestead exemption gone)
    tax_rental_annual: float
    piti_rental: float
    monthly_cash_flow: float          # true cash flow after all reserves
    simple_rent_minus_piti: float     # the "rent beats the mortgage" test you described

    # investor ratios (rental phase)
    noi_annual: float
    cap_rate: float
    dscr: float
    grm: float
    one_percent: float
    cash_on_cash: Optional[float]     # None when near-zero cash in (VA $0 down)
    cash_invested: float

    # deal spread
    est_value: float
    list_to_value_spread: float       # negative = listed below est. value

    passes_cashflow_screen: bool
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Ratios
# --------------------------------------------------------------------------- #

def cap_rate(noi_annual: float, purchase_price: float) -> float:
    return noi_annual / purchase_price if purchase_price else 0.0


def dscr(noi_annual: float, annual_debt_service: float) -> float:
    return noi_annual / annual_debt_service if annual_debt_service else 0.0


def grm(purchase_price: float, annual_rent: float) -> float:
    return purchase_price / annual_rent if annual_rent else 0.0


def one_percent_rule(monthly_rent: float, purchase_price: float) -> float:
    return monthly_rent / purchase_price if purchase_price else 0.0


def cash_on_cash(annual_cash_flow: float, cash_invested: float,
                 min_cash_threshold: float = 500.0) -> Optional[float]:
    """CoC is meaningless with near-zero cash in (VA $0 down). Return None then."""
    if cash_invested <= min_cash_threshold:
        return None
    return annual_cash_flow / cash_invested


# --------------------------------------------------------------------------- #
# Main underwrite
# --------------------------------------------------------------------------- #

def underwrite(d: DealInputs, min_cash_flow: float = 0.0) -> UnderwriteResult:
    loan = loan_amount(d.purchase_price, d.va_funding_fee_pct, d.disability_exempt)
    pi = monthly_pi(loan, d.annual_rate, d.term_years)
    annual_ds = pi * 12

    monthly_ins = d.insurance_annual / 12.0

    # --- owner-occupied phase (homestead exemption applies) ---
    tax_owner = ga_property_tax(d.market_value, d.millage_rate,
                                d.homestead_exemption_assessed, d.assessment_ratio)
    piti_owner = pi + tax_owner / 12.0 + monthly_ins + d.hoa_monthly

    # --- rental phase (homestead exemption LOST -> higher tax) ---
    tax_rental = ga_property_tax(d.market_value, d.millage_rate,
                                 0.0, d.assessment_ratio)
    piti_rental = pi + tax_rental / 12.0 + monthly_ins + d.hoa_monthly

    rent = d.monthly_rent

    # true monthly cash flow (no double counting — build straight from rent)
    monthly_cash_flow = (
        rent
        - pi
        - tax_rental / 12.0
        - monthly_ins
        - d.hoa_monthly
        - rent * d.vacancy_pct
        - rent * d.maintenance_pct
        - rent * d.capex_pct
        - rent * d.mgmt_pct
    )

    # the buyer's own headline test: does rent beat the mortgage payment?
    simple_rent_minus_piti = rent - piti_rental

    # NOI (standard): excludes debt service AND capex reserve
    egi_annual = rent * 12 * (1 - d.vacancy_pct)
    opex_annual = (
        rent * 12 * d.maintenance_pct
        + rent * 12 * d.mgmt_pct
        + d.insurance_annual
        + tax_rental
        + d.hoa_monthly * 12
    )
    noi_annual = egi_annual - opex_annual

    # cash invested (VA $0 down => mostly closing costs, net of credits)
    closing = d.purchase_price * d.closing_costs_pct
    cash_invested = max(0.0, closing - d.lender_credit - d.seller_concession)

    coc = cash_on_cash(monthly_cash_flow * 12, cash_invested)

    # negative spread = listed below est. value = potential deal
    spread = (d.purchase_price - d.market_value) / d.market_value \
        if d.market_value else 0.0

    notes = []
    if d.homestead_exemption_assessed > 0:
        notes.append(
            f"Property tax rises ${tax_rental - tax_owner:,.0f}/yr when it converts "
            f"to a rental (homestead exemption lost)."
        )
    if coc is None:
        notes.append("Cash-on-cash n/a — VA $0-down leaves near-zero cash invested.")
    if simple_rent_minus_piti > 0 and monthly_cash_flow < 0:
        notes.append(
            "Rent beats the mortgage payment, but goes negative once vacancy, "
            "maintenance, capex and management reserves are included."
        )

    return UnderwriteResult(
        loan_amount=loan,
        monthly_pi=pi,
        annual_debt_service=annual_ds,
        tax_owner_annual=tax_owner,
        piti_owner=piti_owner,
        tax_rental_annual=tax_rental,
        piti_rental=piti_rental,
        monthly_cash_flow=monthly_cash_flow,
        simple_rent_minus_piti=simple_rent_minus_piti,
        noi_annual=noi_annual,
        cap_rate=cap_rate(noi_annual, d.purchase_price),
        dscr=dscr(noi_annual, annual_ds),
        grm=grm(d.purchase_price, rent * 12),
        one_percent=one_percent_rule(rent, d.purchase_price),
        cash_on_cash=coc,
        cash_invested=cash_invested,
        est_value=d.market_value,
        list_to_value_spread=spread,
        passes_cashflow_screen=monthly_cash_flow >= min_cash_flow,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Demo — your actual scenario
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    deal = DealInputs(
        purchase_price=235000,
        market_value=242000,          # est. value came back ABOVE list -> good spread
        annual_rate=0.06125,
        monthly_rent=1850,
        insurance_annual=1600,
        millage_rate=30.0,            # ~Columbia County ballpark; tool pulls the real one
        homestead_exemption_assessed=10000,
        vacancy_pct=0.08,
        maintenance_pct=0.10,
        capex_pct=0.08,
        mgmt_pct=0.09,                # set 0.0 if you self-manage
        lender_credit=3000,
        seller_concession=4000,
    )
    r = underwrite(deal, min_cash_flow=0.0)

    print("=== FORT EISENHOWER DEAL SCANNER — underwrite demo ===")
    print(f"Loan (incl. 2.15% VA funding fee): ${r.loan_amount:,.0f}")
    print(f"Monthly P&I:                        ${r.monthly_pi:,.2f}")
    print(f"PITI while you live there:          ${r.piti_owner:,.2f}")
    print(f"PITI once it's a rental:            ${r.piti_rental:,.2f}")
    print(f"Est. rent:                          ${deal.monthly_rent:,.2f}")
    print(f"Rent - PITI (your headline test):   ${r.simple_rent_minus_piti:,.2f}")
    print(f"TRUE monthly cash flow (w/ reserves): ${r.monthly_cash_flow:,.2f}")
    print(f"Cap rate:                           {r.cap_rate*100:,.2f}%")
    print(f"DSCR:                               {r.dscr:,.2f}")
    print(f"GRM:                                {r.grm:,.2f}")
    print(f"1% rule:                            {r.one_percent*100:,.2f}%")
    coc = "n/a" if r.cash_on_cash is None else f"{r.cash_on_cash*100:,.1f}%"
    print(f"Cash-on-cash:                       {coc} (cash in ${r.cash_invested:,.0f})")
    print(f"List-to-value spread:               {r.list_to_value_spread*100:,.1f}%")
    print(f"Passes cash-flow screen:            {r.passes_cashflow_screen}")
    for n in r.notes:
        print(f"  note: {n}")
