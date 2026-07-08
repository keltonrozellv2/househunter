# Househunter — Fort Eisenhower Deal Scanner

Screens the for-sale housing market around **Fort Eisenhower, GA** and grades
each home **BUY / PASS** for a VA-loan owner-occupant who will live in the home
now and convert it to a rental later. For every candidate it produces:

- a **BUY/PASS vote** with a transparent 0–100 score and plain-English reasons,
- an **estimated market value** with a range and how it was derived,
- a **recommended offer price**,
- a **recommended seller concession** (in $ and % of price, with the VA 4%-cap nuance),
- a one-page **report card**, a ranked **results.csv**, and a local **dashboard.html**.

> This is decision-support, **not** financial or legal advice. Estimates are
> model outputs, not appraisals.

## Quick start

```bash
pip install -r requirements.txt

# full pipeline on bundled sample data — zero API calls
python main.py --sample

# run the math tests (40, all offline)
python -m unittest discover -s tests
```

Open `dashboard.html` in a browser for the sortable ranked table; per-property
markdown cards land in `reports/`.

## Live mode

```bash
cp .env.example .env     # then paste your keys into .env (never commit it)
python main.py           # Stage 1: one cached RentCast search per target city
python main.py --deep --confirm   # Stage 2: deep-pull gated candidates (~4 calls each)
```

| Key | Needed for | Notes |
|---|---|---|
| `RENTCAST_API_KEY` | listings, value AVM, rent AVM, comps, tax records | free tier = **50 calls/month** |
| `ORS_API_KEY` | commute routing | free; **off-peak estimates only** (labeled) |
| `GOOGLE_MAPS_API_KEY` | true PM-peak commute times | optional; needs Google billing |
| `ATTOM_API_KEY` | school boundaries, 2nd-opinion AVM | optional 30-day trial |

With no router key at all, commutes fall back to a clearly-labeled
straight-line estimate.

### The 50-call/month budget (the "CarFax" funnel)

Stage 1 spends ~1 call per target city and screens everything with free math
(hard filters, commute, a cheap prescore). Stage 2 — the ~4-call-per-property
deep pull (value AVM + rent AVM + records) — only fires on candidates that
clear the prescore gate **and** you approve the spend (`--confirm` or the
interactive prompt). Every response is cached in `cache.db` with TTLs
(config.yaml → `rentcast.cache_ttl_days`); the tool warns at 40 calls and
hard-stops at 50 with a clear message. Don't dodge the cap with extra accounts
— it violates RentCast's terms.

## CLI

```
python main.py [--sample] [--refresh] [--deep] [--confirm]
               [--address "123 Main St, Grovetown, GA 30813"]
               [--min-score 70] [--no-commute] [--top 5] [--attom]
```

- `--refresh` — ignore the listings cache TTL and re-search
- `--address` — analyze one address (forces a deep pull on it)
- `--min-score` — only report candidates at or above this score
- `--top N` — print only the top N report cards (CSV/dashboard still get all)

## How it decides (config.yaml drives everything)

1. **Hard filters:** price ≤ $250k, detached single-family, ≥3 bed / ≥2 bath,
   no manufactured/mobile/leased-land, commute ≤ 40 min.
2. **The BAH governor (the filter that actually bites):** owner-occupied PITI
   must stay ≤ **$1,509/mo** (BAH $1,809 − ~$300 utilities). At 6.125% with the
   VA funding fee financed, that's roughly a $195k–$205k ceiling.
3. **Underwriting** (`src/underwrite.py`, verified by tests): VA $0-down loan
   with the 2.15% funding fee rolled in, Georgia 40% assessment ratio +
   homestead exemption (lost when converted to a rental — taxes rise), true
   rental cash flow after vacancy/maintenance/capex reserves (mgmt is 0% —
   self-managing), cap rate, DSCR, GRM, 1% rule, cash-on-cash.
4. **Score** (weights in config): rental cash flow 25, deal spread 20,
   construction/value retention 15, schools 15, commute 10, appreciation proxy
   10, VA condition risk 5. **BUY** needs score ≥ 70 **and** rental cash flow ≥
   $0/mo **and** no disqualifier.
5. **Offer strategy:** real tactics from list-to-value spread + days on market
   (anchor under stale/overpriced listings, offer at value when fresh, never
   chase past the top of the value range). Concession ask =
   min(closing costs − $3k lender credit, 4% of price), with the VA note that
   customary closing costs sit outside the 4% concession cap.

## Guardrails

- Official APIs and public county records only; **no scraping** of sites whose
  terms forbid it (Zillow/Redfin/Realtor.com).
- All secrets in `.env` (gitignored). Keys are never printed, even in errors.
- Every estimate is labeled with its source and confidence; an AVM is never
  presented as an appraisal.
- Schools are **surfaced** (district + GreatSchools link), never silently gated.

## Project layout

```
config.yaml        buyer profile, buy-box, funnel, assumptions, score weights
main.py            pipeline orchestration + CLI
src/
  models.py        Listing / Enrichment / Analysis dataclasses
  cache.py         SQLite TTL cache + RentCast monthly call budget
  ingest.py        Stage 1 — RentCast listing search / sample fixtures
  enrich.py        Stage 2 — value AVM, rent AVM, records, schools
  commute.py       Google (peak) → ORS (off-peak) → straight-line fallback
  valuation.py     value point + range + confidence + list-to-value spread
  underwrite.py    the verified financial core (don't rewrite — build on it)
  score.py         weighted rubric → BUY/PASS with reasons
  offer.py         offer price + seller-concession strategy
  report.py        report cards, results.csv, dashboard.html
data/
  sample_listings.json   offline fixtures for --sample mode
tests/             40 unit tests, all offline
```
