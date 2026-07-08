# CLAUDE.md — Househunter (Fort Eisenhower Deal Scanner)

This repo builds a command-line + local-dashboard tool that screens the for-sale
housing market around **Fort Eisenhower, GA** and grades each home as **BUY / PASS**
for a VA-loan owner-occupant who will live in the home now and convert it to a
rental later.

## Start here
1. Read **BUILD_PROMPT.md** — it is the full spec (data sources, financial model,
   scoring rubric, output format, guardrails, tech stack, file structure).
2. Follow the **Build Order** in that file.
3. **Before writing code that depends on an assumption, ask the user the open
   questions listed at the end of BUILD_PROMPT.md.**

## What already exists (don't rewrite unless asked)
- `config.yaml` — all buyer constraints and assumptions live here. Note the
  **BAH governor**: `max_monthly_piti: 1509` (BAH $1,809 − ~$300 utilities) is a
  HARD filter. At 6.125% this implies roughly a ~$195k–$205k purchase ceiling.
- `src/underwrite.py` — the financial core (PITI, GA property tax with homestead
  exemption, rental cash flow, cap rate / CoC / DSCR / GRM / 1% rule). **The math
  is verified — build on it, don't replace it.**
- `tests/test_underwrite.py` — 20 passing unit tests. Run with
  `python -m unittest discover -s tests`. Keep them green; add tests for new math.

## Hard rules
- Prefer official APIs and government public records. **Do not scrape sites whose
  terms prohibit it** (Zillow, Redfin, Realtor.com). Any scraping is a last-resort,
  opt-in module that respects robots.txt.
- **RentCast free tier = 50 calls/month.** Use the two-stage funnel in
  `config.yaml` (wide/free Stage 1, deep RentCast pull only on confirmed serious
  candidates). Cache everything; never dodge the cap with multiple accounts.
- Keep all secrets in `.env` (see `.env.example`); never commit them.
- Every property report must print: BUY/PASS vote + reasoning, estimated value +
  range, recommended offer, recommended seller concession, and a
  "decision-support, not financial or legal advice" line.
