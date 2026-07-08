#!/usr/bin/env python3
"""
main.py — Fort Eisenhower Deal Scanner: orchestrates the whole pipeline.

    python main.py --sample              # full run on bundled sample data (0 API calls)
    python main.py                       # live Stage 1 (cached RentCast searches)
    python main.py --refresh             # force-refresh listing searches
    python main.py --deep --confirm      # spend Stage-2 deep-pull calls on gated candidates
    python main.py --address "123 Main St, Grovetown, GA 30813"   # analyze one address
    python main.py --min-score 70 --no-commute --top 10

The two-stage "CarFax" funnel (config.yaml -> pipeline) protects RentCast's
50-call/month free tier: Stage 1 is one cheap area search per city; Stage 2's
~4-call deep pull only fires on candidates that clear the prescore gate AND
you confirm the spend.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from dotenv import load_dotenv

import commute as commute_mod
import enrich as enrich_mod
import ingest
import offer as offer_mod
import report as report_mod
import score as score_mod
import underwrite as uw
import valuation
from cache import BudgetExhausted, Cache
from models import Analysis, CommuteResult, Listing


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# Stage 1 — hard filter + cheap prescore (no per-property API calls)
# --------------------------------------------------------------------------- #

def hard_filter(l: Listing, buy_box: dict) -> list[str]:
    """Return the list of buy-box failures (empty = passes)."""
    fails = []
    if l.list_price > buy_box["max_price"]:
        fails.append(f"price ${l.list_price:,.0f} > max ${buy_box['max_price']:,}")
    if l.property_type not in ("single_family",):
        fails.append(f"property type '{l.property_type}' not in buy box")
    if l.property_type in buy_box["exclude"]:
        fails.append(f"excluded type: {l.property_type}")
    if l.beds < buy_box["min_beds"]:
        fails.append(f"{l.beds:.0f} beds < {buy_box['min_beds']}")
    if l.baths < buy_box["min_baths"]:
        fails.append(f"{l.baths:.0f} baths < {buy_box['min_baths']}")
    return fails


def estimate_piti_from_price(price: float, cfg: dict, listing: Listing) -> float:
    """Cheap owner-phase PITI estimate from list price alone (Stage-1 gate).

    Uses the verified underwrite math with the county fallback millage — no API
    calls. The real BAH check reruns in Stage 2 against the est. market value.
    """
    ua = cfg["underwriting_assumptions"]
    buyer = cfg["buyer"]
    loan = uw.loan_amount(price, buyer["va_funding_fee_pct"], buyer["disability_exempt"])
    pi = uw.monthly_pi(loan, buyer["interest_rate"])
    millage = enrich_mod.fallback_millage(listing, ua["millage_rate"])
    tax = uw.ga_property_tax(price, millage, ua["homestead_exemption_assessed"],
                             ua["assessment_ratio"])
    return pi + tax / 12 + ua["insurance_annual"] / 12 + listing.hoa_monthly


def prescore(l: Listing, cfg: dict, commute_min: float | None) -> float:
    """0-100 cheap screen deciding who deserves a Stage-2 deep pull."""
    box = cfg["buy_box"]
    pts = 0.0

    # BAH headroom is the dominant factor (50 pts)
    piti = estimate_piti_from_price(l.list_price, cfg, l)
    cap = box["max_monthly_piti"]
    if piti <= cap:
        pts += 30 + 20 * min(1.0, (cap - piti) / 300)   # more headroom = better
    # construction preference (15)
    if any(p.replace("_", " ") in (l.construction or "") for p in
           box["construction_preference"]):
        pts += 15
    else:
        pts += 7
    # commute (15)
    if commute_min is not None:
        if commute_min <= box["max_commute_min_peak"]:
            pts += 15 * (1 - commute_min / (box["max_commute_min_peak"] * 1.5))
    else:
        pts += 7
    # freshness/DOM (10): both fresh and stale are workable, reward data present
    pts += 10 if l.days_on_market is not None else 5
    # age (10)
    if l.year_built:
        pts += 10 if l.year_built >= 1990 else (6 if l.year_built >= 1978 else 2)
    else:
        pts += 5
    return round(min(100.0, pts), 1)


# --------------------------------------------------------------------------- #
# Stage 2 — deep pull + full analysis
# --------------------------------------------------------------------------- #

def analyze(a: Analysis, cfg: dict, cache: Cache, sample_mode: bool) -> Analysis:
    """Enrich -> value -> underwrite -> score -> offer for one candidate."""
    l = a.listing
    ua = cfg["underwriting_assumptions"]
    buyer = cfg["buyer"]

    if sample_mode:
        a.enrichment = enrich_mod.enrich_from_sample(l)
    else:
        a.enrichment = enrich_mod.deep_pull(l, cache, cfg["rentcast"])
    e = a.enrichment

    a.value = valuation.estimate_value(l, e)

    rent = e.rent_estimate
    if not rent:
        # last-resort heuristic, loudly labeled — never silently invented
        rent = a.value.point * 0.0085
        a.notes.append(f"No rent AVM available — using heuristic 0.85% of value "
                       f"(${rent:,.0f}/mo). Treat rental math as ROUGH.")

    millage = e.millage_rate or enrich_mod.fallback_millage(l, ua["millage_rate"])

    deal = uw.DealInputs(
        purchase_price=l.list_price,
        market_value=a.value.point,
        annual_rate=buyer["interest_rate"],
        monthly_rent=rent,
        va_funding_fee_pct=buyer["va_funding_fee_pct"],
        disability_exempt=buyer["disability_exempt"],
        insurance_annual=ua["insurance_annual"],
        hoa_monthly=l.hoa_monthly or ua["hoa_monthly"],
        millage_rate=millage,
        homestead_exemption_assessed=ua["homestead_exemption_assessed"],
        assessment_ratio=ua["assessment_ratio"],
        vacancy_pct=ua["vacancy_pct"],
        maintenance_pct=ua["maintenance_pct"],
        capex_pct=ua["capex_pct"],
        mgmt_pct=ua["mgmt_pct"],
        closing_costs_pct=ua["closing_costs_pct"],
        lender_credit=buyer["lender_credit"],
    )
    a.underwrite = uw.underwrite(deal, ua["min_cashflow_when_rented"])

    # break-even targets: what price/rent would clear the gates for this home
    min_cf = ua["min_cashflow_when_rented"]
    a.flip = {
        "price_cashflow": uw.breakeven_price(deal, min_cf),
        "rent_needed": uw.breakeven_rent(deal, min_cf),
        "price_bah": uw.max_price_for_piti(cfg["buy_box"]["max_monthly_piti"], deal),
    }
    a.flip["buy_at"] = min(a.flip["price_cashflow"], a.flip["price_bah"])

    a.score = score_mod.score_analysis(a, cfg)
    a.offer = offer_mod.recommend_offer(
        l.list_price, a.value, l.days_on_market,
        ua["closing_costs_pct"], buyer["lender_credit"],
        ua["seller_concession_cap_pct"])
    a.stage = "stage2"
    return a


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fort Eisenhower Deal Scanner")
    p.add_argument("--sample", action="store_true",
                   help="run on bundled sample data (no API calls)")
    p.add_argument("--refresh", action="store_true",
                   help="force-refresh RentCast listing searches (ignores cache TTL)")
    p.add_argument("--address", help="analyze a single address (forces a deep pull)")
    p.add_argument("--deep", action="store_true",
                   help="run Stage-2 deep pulls on candidates that clear the prescore gate")
    p.add_argument("--confirm", action="store_true",
                   help="pre-approve the deep-pull API spend (skips the prompt)")
    p.add_argument("--max-deep", type=int, default=None,
                   help="deep-pull at most N candidates (highest prescore first) "
                        "to protect the monthly RentCast budget")
    p.add_argument("--min-score", type=float, default=None,
                   help="only report candidates scoring at least this (default: all)")
    p.add_argument("--no-commute", action="store_true", help="skip commute routing")
    p.add_argument("--top", type=int, default=None, help="print only the top N cards")
    p.add_argument("--attom", action="store_true",
                   help="enable ATTOM enrichment (requires ATTOM_API_KEY)")
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    cfg = load_config()
    cache = Cache()
    geo = cfg["geography"]
    box = cfg["buy_box"]
    sample_mode = args.sample or not os.environ.get("RENTCAST_API_KEY", "").strip()

    print("=== Fort Eisenhower Deal Scanner ===")
    if sample_mode:
        print("MODE: SAMPLE DATA (no API calls; add RENTCAST_API_KEY to .env "
              "and drop --sample for live mode)\n")
    else:
        used = cache.calls_this_month()
        print(f"MODE: LIVE — RentCast budget {used}/"
              f"{cfg['rentcast']['free_monthly_call_cap']} calls used this month\n")

    # ---------------- Stage 1: ingest ----------------
    print("Stage 1 — ingest + hard filter + prescore")
    try:
        if sample_mode:
            listings = ingest.load_sample_listings()
        elif args.address:
            listings = []  # single-address mode builds its own listing below
        else:
            listings = ingest.fetch_rentcast_listings(
                cache, cfg["rentcast"], geo, box["max_price"], args.refresh)
    except BudgetExhausted as exc:
        print(f"\n✗ {exc}")
        return 1

    if args.address and not sample_mode:
        parts = [s.strip() for s in args.address.split(",")]
        listings = [Listing(
            id=parts[0].lower().replace(" ", "-"), address=parts[0],
            city=parts[1] if len(parts) > 1 else "", state="GA",
            zip_code=parts[-1].split()[-1] if len(parts) > 2 else "",
            list_price=box["max_price"], source="manual")]
        print(f"  single-address mode: {args.address}")

    print(f"  {len(listings)} listings ingested")

    # ---------------- hard filter + commute + prescore ----------------
    candidates: list[Analysis] = []
    dropped = 0
    for l in listings:
        fails = hard_filter(l, box)
        if fails:
            dropped += 1
            continue
        a = Analysis(listing=l)
        if not args.no_commute:
            a.commute = commute_mod.commute_to_base(
                l.latitude, l.longitude, geo["base_lat"], geo["base_lng"], cache)
            if (a.commute.minutes is not None
                    and a.commute.minutes > box["max_commute_min_peak"]):
                dropped += 1
                continue
        else:
            a.commute = CommuteResult(label="skipped (--no-commute)")
        a.prescore = prescore(l, cfg, a.commute.minutes if a.commute else None)
        candidates.append(a)

    threshold = cfg["pipeline"]["stage1_wide_screen"]["prescore_threshold"]
    gated = [a for a in candidates if a.prescore >= threshold]
    print(f"  {dropped} dropped by hard filters/commute; {len(candidates)} remain; "
          f"{len(gated)} clear the prescore gate (≥{threshold})")

    # ---------------- Stage 2: deep pull + analysis ----------------
    to_analyze = gated if (sample_mode or args.deep or args.address) else []
    if args.max_deep is not None and len(to_analyze) > args.max_deep:
        to_analyze = sorted(to_analyze, key=lambda a: -a.prescore)[: args.max_deep]
        print(f"  --max-deep: limiting to the top {args.max_deep} by prescore")
    if not sample_mode and gated and not args.deep and not args.address:
        est_calls = len(gated) * cfg["pipeline"]["stage2_deep_pull"]["calls_per_property"]
        print(f"\nStage 2 skipped — rerun with --deep --confirm to spend "
              f"~{est_calls} RentCast calls on {len(gated)} gated candidates.")
    if to_analyze and not sample_mode and not args.confirm:
        est_calls = len(to_analyze) * cfg["pipeline"]["stage2_deep_pull"]["calls_per_property"]
        used = cache.calls_this_month()
        cap = cfg["rentcast"]["free_monthly_call_cap"]
        answer = input(f"\nDeep-pull {len(to_analyze)} candidates ≈ {est_calls} RentCast "
                       f"calls ({used}/{cap} already used). Proceed? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing spent. Rerun with --confirm to pre-approve.")
            return 0

    analyzed: list[Analysis] = []
    if to_analyze:
        print(f"\nStage 2 — deep pull + underwrite ({len(to_analyze)} candidates)")
        for a in to_analyze:
            try:
                analyzed.append(analyze(a, cfg, cache, sample_mode))
            except BudgetExhausted as exc:
                print(f"\n✗ {exc}\n  Stopping deep pulls; reporting what completed.")
                break
            except Exception as exc:
                print(f"  ✗ {a.listing.full_address}: {exc}")

    if args.min_score is not None:
        analyzed = [a for a in analyzed if a.score.total >= args.min_score]

    if not analyzed:
        print("\nNo candidates fully analyzed — nothing to report.")
        return 0

    # ---------------- Report ----------------
    csv_path, html_path = report_mod.write_outputs(analyzed)
    ranked = sorted(analyzed, key=lambda a: -a.score.total)

    shown = ranked[: args.top] if args.top else ranked
    for a in shown:
        print("\n" + "=" * 78)
        print(report_mod.render_card(a))

    print("\n" + "=" * 78)
    print(f"\n{len(ranked)} candidates analyzed — "
          f"{sum(1 for a in ranked if a.score.vote == 'YES')} BUY / "
          f"{sum(1 for a in ranked if a.score.vote == 'NO')} PASS")
    print(f"Ranked CSV:  {csv_path}")
    print(f"Dashboard:   {html_path}  (open in a browser)")
    print(f"Cards:       {report_mod.REPORTS_DIR}/")
    print(f"\n{report_mod.DISCLAIMER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
