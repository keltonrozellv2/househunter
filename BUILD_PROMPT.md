# Claude Code Build Prompt — "Fort Eisenhower Deal Scanner"

> Paste everything below into Claude Code (in the desktop app, terminal, or your IDE). Pick whichever model you like — Fable 5 or Opus both work. Read the whole spec first, then follow the **Build Order** and **ask me the questions in the final section before writing code that depends on an assumption.**

---

## 1. Role & Objective

You are building a command-line + local-dashboard tool called **`deal-scanner`** that continuously screens the for-sale housing market around **Fort Eisenhower, GA** (formerly Fort Gordon; base coordinates ≈ 33.4204, -82.1387) and grades each candidate home as a **buy / pass** for an owner-occupant who intends to live in the home now and convert it to a rental in a few years.

The tool must produce, for every home that clears the buy-box:
1. A **YES / NO vote** with a transparent, weighted score and plain-English reasoning.
2. An **estimated market value** (with a confidence range and how it was derived).
3. A **recommended offer price**.
4. A **recommended seller concession** toward closing costs (in $ and % of price).
5. A one-page **report card** per property + a ranked **CSV / dashboard** of all candidates.

Use **real underwriting tactics** that investors and buyer's agents actually use (comps-based valuation, cap rate, cash-on-cash, DSCR, GRM, days-on-market and list-to-value spread for offer strategy, construction-type value retention). Do not invent metrics.

This is decision-support, **not** financial or legal advice — print that disclaimer in every report.

---

## 2. Buyer Profile & Buy-Box (put ALL of this in `config.yaml` so it's editable)

```yaml
buyer:
  loan_type: VA
  interest_rate: 0.06125          # 6.125%
  lender_credit: 3000             # applied to closing costs
  down_payment: 0                 # VA $0 down
  va_first_use: true              # affects funding fee
  va_funding_fee_pct: 0.0215      # 2.15% first use, financed into loan; set 0 if disability-exempt
  disability_exempt: false        # if true, funding fee = 0
  occupancy: owner_then_rental    # live in now, rent later

buy_box:
  max_price: 250000
  property_types: [single_family] # detached SFR only for v1
  min_beds: 3                     # ASK USER to confirm
  min_baths: 2                    # ASK USER to confirm
  max_commute_min_peak: 40        # to Fort Eisenhower, weekday PM peak
  construction_preference: [brick, brick_veneer, masonry]  # scored, not required
  exclude: [manufactured, mobile, leased_land]  # value-retention + VA financing risk
  min_school_rating: null         # ASK USER; e.g. 6 on GreatSchools 1-10, or null to just surface it

counties: [Richmond, Columbia]    # + McDuffie/Jefferson edges if commute allows
target_cities: [Grovetown, Evans, Harlem, Augusta, Hephzibah, Martinez]

underwriting_assumptions:         # sane defaults; all editable
  insurance_annual: 1600          # GA SFR estimate
  vacancy_pct: 0.08
  maintenance_pct_of_rent: 0.10
  capex_reserve_pct_of_rent: 0.08
  property_mgmt_pct_of_rent: 0.09 # set 0 if self-managing
  hoa_monthly: 0                  # per-listing override
  homestead_exemption_now: true   # owner-occupied; LOST when converted to rental (taxes rise)
  min_cashflow_when_rented: 0     # rent must beat PITI+reserves once converted
  seller_concession_cap_pct: 0.04 # VA caps "concessions" at 4% of value (see notes)
  closing_costs_estimate_pct: 0.03
```

---

## 3. Data Sources (APIs first, official public records second, scraping last)

**Primary — RentCast API (free tier: 50 calls/month, no credit card).**
Use it for: for-sale listing search, property records + tax-assessor/appraisal data, AVM value estimate, rent estimate, and sale/rent comps. Because the free tier is only 50 calls/month:
- Cache **every** response to a local SQLite DB (`cache.db`) keyed by endpoint+params with a TTL (e.g. 7 days for property records, 24h for listings).
- Never re-request a property already cached within TTL.
- Batch/scope listing pulls by zip so one call returns many candidates.
- Log remaining monthly call budget and warn at 40/50 used.

**Secondary — ATTOM API (30-day free trial only, then paid).**
Optional. If the user provides a trial key, use it *only* to enrich (a) school-district/boundary data and (b) a second-opinion AVM for cross-checking RentCast. Gate all ATTOM calls behind `if attom_enabled`. Never make ATTOM required.

**County appraisal ("qCounty") — qPublic.net / Schneider "Beacon".**
Richmond and Columbia County publish assessor records (parcel, assessed value, appraised value, millage, sales history) on qPublic.net. RentCast already ingests this assessor data, so **prefer RentCast's tax/assessment fields** and only fall back to qPublic for a specific parcel when RentCast is missing it. If you fetch qPublic directly, respect robots.txt, add polite rate-limiting, and cache.

**Commute (40-min PM-peak drive to Fort Eisenhower).**
Use Google Distance Matrix API with `departure_time` set to an upcoming weekday 5:00 PM local (this is the only way to get true traffic-aware peak times; Google's free monthly credit covers low volume — requires a billing account). If the user won't enable Google billing, fall back to OpenRouteService (free key, no live traffic) and clearly label commute times as **off-peak estimates**. Cache commute results per (lat,lng) so each address is only routed once.

**Schools.**
Neither free tier gives clean ratings. For each candidate: determine the assigned elementary/middle/high school (ATTOM boundaries if enabled, else nearest-by-district heuristic) and output a **GreatSchools search URL** + the district name so the user can eyeball it. Only enforce `min_school_rating` if a rating source is actually configured; otherwise surface, don't gate.

Store all API keys in a `.env` file (never hard-code). Print a friendly setup message if a key is missing.

---

## 4. Pipeline (stages, each a separate module)

1. **Ingest** — pull active SFR listings by zip within the county set (RentCast). Normalize to a `Listing` dataclass.
2. **Hard filter** — drop anything failing the buy-box: price > max, wrong type, manufactured/mobile/leased-land, below min beds/baths.
3. **Commute filter** — drop anything over `max_commute_min_peak` to base.
4. **Enrich** — attach tax/assessor + appraisal data, AVM value, rent estimate, comps, assigned schools, construction type, year built, lot, HOA.
5. **Value** — compute estimated market value (Section 5).
6. **Underwrite** — compute PITI, and the rental-conversion cash flow + investor ratios (Section 5).
7. **Score & vote** — weighted rubric → YES/NO (Section 6).
8. **Recommend** — offer price + seller-concession ask (Section 7).
9. **Report** — per-property card + ranked CSV + simple local HTML dashboard.

---

## 5. The Financial Model (implement these exactly)

**Loan basis (VA, $0 down):**
- `financed_funding_fee = purchase_price * va_funding_fee_pct` (0 if disability-exempt)
- `loan_amount = purchase_price + financed_funding_fee`  (VA lets the funding fee be rolled in)
- No PMI (VA has none).

**Monthly P&I:** standard amortization
`M = L * r(1+r)^n / ((1+r)^n − 1)`, `r = rate/12`, `n = 360`.

**PITI (owner-occupied phase):**
`PITI = P&I + monthly_property_tax + monthly_insurance + monthly_hoa`
- Property tax: use assessed value × county millage from RentCast/qPublic; apply Georgia homestead exemption while owner-occupied.

**Rent estimate:** RentCast rent AVM for the property.

**Rental-conversion phase (the strategy that matters):** when the buyer moves out and rents it:
- Homestead exemption is **lost** → recompute property tax higher.
- `operating_expenses = vacancy + maintenance + capex_reserve + mgmt + insurance + taxes + hoa`
  (vacancy/maint/capex/mgmt as % of rent per config).
- `NOI_annual = (rent*12) − operating_expenses_annual` (NOI excludes mortgage).
- `monthly_cashflow = rent − PITI_rental − (vacancy+maint+capex+mgmt as $)`
- **Screen:** `monthly_cashflow >= min_cashflow_when_rented` (the user's core requirement: rent must beat the mortgage + reserves).

**Investor ratios (report all; use as scoring inputs, not hard gates unless noted):**
- **1% rule:** `monthly_rent / purchase_price` — flag ≥ 1.0% (rare today; informational).
- **Cap rate:** `NOI_annual / purchase_price`.
- **Cash-on-cash:** `annual_pre_tax_cashflow / total_cash_invested`, where cash invested ≈ closing costs − lender_credit − seller_concession (VA $0 down makes this small; if ~0, report "n/a — near-zero cash in" rather than infinity).
- **DSCR:** `NOI_annual / annual_debt_service` — lenders like ≥ 1.20; flag below 1.0.
- **GRM:** `purchase_price / annual_rent`.

**Value estimate (Section 5 output):**
- Primary = RentCast AVM.
- Cross-check against: (a) comps median $/sqft × subject sqft, (b) county appraised value, (c) ATTOM AVM if enabled.
- Report a **value range** = min/median/max of available estimates, plus a confidence note (tighter range + more comps = higher confidence).
- Compute **list-to-value spread** = `(list_price − est_value) / est_value` (negative = listed below value = potential deal).

---

## 6. Scoring Rubric → YES / NO Vote

Weighted 0–100 score. Print the sub-scores so the vote is transparent. Suggested weights (put them in config so they're tunable):

| Factor | Weight | Logic |
|---|---|---|
| Rental cash flow when converted | 25 | Positive & growing = full marks; negative = heavy penalty |
| Value vs. list (deal spread) | 20 | Listed below est. value scores high |
| Value retention / construction | 15 | Brick/masonry > frame > (manufactured excluded); newer roof/systems bonus |
| School quality | 15 | Assigned-school rating if available, else district reputation flag |
| Commute to base | 10 | Closer to 40-min cap = fewer points; well under = more |
| Appreciation proxy | 10 | RentCast zip price/rent trend, days-on-market, inventory |
| VA suitability / condition risk | 5 | Older/as-is/fixer flags = likely VA MPR issues (roof, septic, pre-1978 paint) |

**Vote rule:** `YES` if score ≥ threshold (default 70) **AND** rental-conversion cash flow ≥ min **AND** no disqualifying flag (manufactured, failing commute, obvious VA MPR blocker). Otherwise `NO`, with the top 1–2 reasons.

---

## 7. Offer & Seller-Concession Strategy (real tactics)

Compute a recommended offer from est. value, list price, days-on-market (DOM), and local list-to-sale ratios:
- **Below value + high DOM (stale):** offer ~3–8% under list (anchor low, justify with comps).
- **At/under value + fresh listing:** offer at or just under value.
- **Priced under value + low DOM / competitive:** offer at value, lead with strength (fast close, clean terms), don't overpay above the value range.
- Never recommend offering above the top of the value range without an explicit note on why.

**Seller concession toward closing costs:**
- Target concession = `min(estimated_closing_costs − lender_credit, price × seller_concession_cap_pct)`.
- Note the VA nuance in the output: VA caps **"seller concessions"** (prepaids, points, funding fee, etc.) at **4% of the established value**, but **seller-paid customary buyer closing costs are treated separately** and aren't counted in that 4% — so the practical ask can exceed 4% when structured correctly. Present the ask in both $ and %, and net it against the $3k lender credit so the buyer isn't double-asking.

**Also encode these VA realities as flags/notes (don't let the tool ignore them):**
- Owner-occupancy is required (intent to occupy, generally within ~60 days) — the "rent later" plan is fine; a same-day pure rental is not.
- A VA appraisal / Notice of Value must meet Minimum Property Requirements — surface likely blockers for older/as-is homes.
- When the buyer later buys their next home, VA bonus entitlement may allow a second VA loan while keeping this one as a rental — note this in the rental-conversion summary.

---

## 8. Output Format

**Per-property report card** (markdown + printed to console) containing: address, price, est. value + range + method, list-to-value spread, PITI now, projected rent, rental-conversion cash flow, cap rate / CoC / DSCR / GRM / 1% check, construction type, year built, assigned schools + GreatSchools link, commute (peak), county assessed/appraised value, **VOTE + reasoning**, **recommended offer**, **recommended seller concession**, and the not-advice disclaimer.

**Ranked dashboard:** `results.csv` (all candidates, sortable) + a single self-contained `dashboard.html` that reads the CSV and shows a sortable table with the vote color-coded. No external services in the HTML.

---

## 9. Guardrails (enforce in code and in docs)

- **Legal/ToS:** Prefer official APIs and government public-records sources. Do **not** scrape sites whose terms prohibit it (Zillow, Redfin, Realtor.com). If any scraping is used as a last resort, honor robots.txt, rate-limit, and make it a clearly-labeled optional module the user opts into.
- **Secrets:** all keys in `.env`; never commit them; add `.gitignore`.
- **API budget:** hard-stop RentCast calls at the monthly cap with a clear message; rely on cache after that.
- **Honesty:** label every estimate's source and confidence; never present an AVM as an appraisal. Print the "decision-support, not financial or legal advice" line on every report.

---

## 10. Tech Stack & Project Structure

- Python 3.11+, `requests`, `pydantic` (dataclasses/validation), `pyyaml`, `sqlite3` (stdlib) for caching, `pandas` for the CSV/ranking, `jinja2` for the HTML card/dashboard, `python-dotenv`.
- Structure:
```
deal-scanner/
  config.yaml
  .env.example
  cache.db                # created at runtime
  src/
    ingest.py             # RentCast listing search
    enrich.py             # records, AVM, rent, comps, schools, county data
    commute.py            # Distance Matrix / ORS
    valuation.py          # est value + range + spread
    underwrite.py         # PITI, cash flow, ratios (Section 5)
    score.py              # rubric + vote (Section 6)
    offer.py              # offer + concession (Section 7)
    report.py             # cards, CSV, dashboard
    cache.py              # SQLite get/set with TTL + call-budget counter
    models.py             # Listing / Analysis dataclasses
  main.py                 # orchestrates the pipeline, CLI flags
  tests/                  # unit tests for the math (fixtures with known inputs)
  README.md               # setup, keys, how to run, what the numbers mean
```
- CLI: `python main.py --refresh` (pull new listings), `--address "123 Main St"` (analyze one), `--min-score 70`, `--no-commute`, `--attom` (enable ATTOM enrich).
- Write **unit tests for the financial math** with hand-checked fixtures (a known price/rate should produce a known P&I, cap rate, DSCR, etc.). The math must be verifiably correct.

---

## 11. Build Order

1. Scaffold repo, `config.yaml`, `.env.example`, cache layer + RentCast call-budget counter.
2. `models.py` + `underwrite.py` + **tests** (get the math right first, offline, with fixtures).
3. `ingest.py` + `enrich.py` against RentCast (with caching).
4. `commute.py`, `valuation.py`, `score.py`, `offer.py`.
5. `report.py` (cards → CSV → HTML dashboard).
6. `main.py` orchestration + CLI + README.
7. Optional ATTOM enrichment behind a flag.

---

## 12. Ask me these before coding anything that assumes an answer

1. Confirm min **beds/baths** (default 3 / 2) and whether **townhomes/duplex** should ever be allowed (VA permits 2–4 unit house-hacks — currently excluded).
2. Will I **self-manage** the future rental (drop the 9% mgmt fee) or budget for a manager?
3. Do I want to enable **Google billing** for true peak-traffic commute times, or accept off-peak estimates from the free router?
4. Am I **VA-funding-fee exempt** (service-connected disability)? It changes the loan amount.
5. A **minimum school rating** to enforce, or just surface schools and let me judge?
6. Any **must-have filters** (garage, min sqft, min lot, single-story, year-built floor for VA condition)?
7. Do I have a **RentCast API key** yet (and optionally an ATTOM trial key)? If not, start with cached/sample data so I can see the pipeline run.

Once I answer, proceed with the Build Order. Keep the code clean, typed, commented where the underwriting logic lives, and don't over-engineer the v1.
