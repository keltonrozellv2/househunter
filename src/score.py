"""
score.py — the weighted 0-100 rubric and the YES/NO vote (spec §6).

Every factor returns (points_0_to_1, why) so the report can print transparent
sub-scores. Weights come from config.yaml. The vote requires ALL of:
  score >= yes_threshold
  AND rental-conversion cash flow >= min_cashflow_when_rented
  AND owner-phase PITI <= max_monthly_piti  (the BAH governor — HARD)
  AND no disqualifier (manufactured/mobile, failed commute, VA MPR blocker).
"""

from __future__ import annotations

from typing import Optional

from models import Analysis, Score

# construction quality tiers for value retention
_CONSTRUCTION_TIERS = {
    "brick": 1.0, "brick veneer": 1.0, "brick_veneer": 1.0, "masonry": 1.0,
    "stone": 1.0, "concrete": 0.9, "stucco": 0.7, "hardiplank": 0.7,
    "fiber cement": 0.7, "cement": 0.7, "vinyl": 0.5, "frame": 0.45,
    "wood": 0.45, "aluminum": 0.4,
}

_GOOD_DISTRICT_HINTS = ("columbia",)   # Columbia Co. schools rate above Richmond Co.


def _construction_score(construction: str, year_built: Optional[int]) -> tuple[float, str]:
    c = (construction or "").lower()
    base, label = 0.5, "unknown construction"
    for key, tier in _CONSTRUCTION_TIERS.items():
        if key in c:
            base, label = tier, c
            break
    bonus = 0.0
    if year_built and year_built >= 2010:
        bonus = 0.15  # newer roof/systems
    elif year_built and year_built >= 1995:
        bonus = 0.05
    return min(1.0, base + bonus), f"{label}, built {year_built or '?'}"


def _cashflow_score(monthly_cf: float) -> tuple[float, str]:
    # $0 = barely passing (0.5); +$300 = full marks; negative decays fast
    if monthly_cf >= 300:
        return 1.0, f"${monthly_cf:,.0f}/mo true cash flow when rented"
    if monthly_cf >= 0:
        return 0.5 + 0.5 * monthly_cf / 300, f"${monthly_cf:,.0f}/mo when rented (thin)"
    return max(0.0, 0.5 + monthly_cf / 400), f"NEGATIVE ${-monthly_cf:,.0f}/mo when rented"


def _spread_score(spread: float) -> tuple[float, str]:
    # -10% under value = full marks; at value = 0.5; +10% over = 0
    s = max(0.0, min(1.0, 0.5 - spread * 5))
    if spread < -0.02:
        why = f"listed {-spread:.1%} UNDER est. value"
    elif spread > 0.02:
        why = f"listed {spread:.1%} OVER est. value"
    else:
        why = "listed at est. value"
    return s, why


def _school_score(district: str) -> tuple[float, str]:
    d = (district or "").lower()
    if any(h in d for h in _GOOD_DISTRICT_HINTS):
        return 0.8, f"{district} (well-regarded; verify on GreatSchools)"
    if d:
        return 0.5, f"{district} (mixed ratings; verify per-school on GreatSchools)"
    return 0.5, "district unknown — check GreatSchools link"


def _commute_score(minutes: Optional[float], cap: float) -> tuple[float, str]:
    if minutes is None:
        return 0.5, "commute unknown"
    if minutes > cap:
        return 0.0, f"{minutes:.0f} min exceeds {cap:.0f}-min cap"
    # 15 min or less = full marks; at the cap = 0.1
    frac = max(0.0, min(1.0, (cap - minutes) / (cap - 15)))
    return 0.1 + 0.9 * frac, f"{minutes:.0f} min to base"


def _appreciation_score(dom: Optional[int]) -> tuple[float, str]:
    # v1 proxy: days-on-market (zip trend data would cost extra API calls)
    if dom is None:
        return 0.5, "no DOM data"
    if dom <= 10:
        return 0.9, f"{dom} DOM — hot demand"
    if dom <= 30:
        return 0.7, f"{dom} DOM — normal market"
    if dom <= 60:
        return 0.5, f"{dom} DOM — cooling"
    return 0.3, f"{dom} DOM — stale/slow segment"


def _va_condition_score(year_built: Optional[int], dom: Optional[int]) -> tuple[float, str]:
    if year_built is None:
        return 0.5, "age unknown — VA appraisal risk unassessed"
    if year_built >= 2000:
        return 1.0, "modern build — low VA MPR risk"
    if year_built >= 1978:
        return 0.7, "pre-2000 — check roof/HVAC age"
    return 0.3, "pre-1978 — lead-paint + systems age = VA MPR risk"


def score_analysis(a: Analysis, cfg: dict) -> Score:
    weights = cfg["scoring"]["weights"]
    threshold = cfg["scoring"]["yes_threshold"]
    buy_box = cfg["buy_box"]
    min_cf = cfg["underwriting_assumptions"]["min_cashflow_when_rented"]

    uw = a.underwrite
    listing = a.listing

    factors = {
        "rental_cash_flow": _cashflow_score(uw.monthly_cash_flow),
        "value_vs_list": _spread_score(a.value.list_to_value_spread),
        "value_retention_construction": _construction_score(
            listing.construction, listing.year_built),
        "school_quality": _school_score(a.enrichment.school_district),
        "commute": _commute_score(
            a.commute.minutes if a.commute else None,
            buy_box["max_commute_min_peak"]),
        "appreciation_proxy": _appreciation_score(listing.days_on_market),
        "va_condition_risk": _va_condition_score(
            listing.year_built, listing.days_on_market),
    }

    subscores, total = {}, 0.0
    for name, (frac, why) in factors.items():
        w = weights[name]
        pts = frac * w
        subscores[name] = {"points": round(pts, 1), "max": w, "why": why}
        total += pts
    total = round(total, 1)

    # ---- disqualifiers (hard NOs regardless of score) ----
    dq = []
    if listing.property_type in ("manufactured", "mobile"):
        dq.append("manufactured/mobile home — excluded (value retention + VA financing)")
    if a.commute and a.commute.minutes is not None and \
            a.commute.minutes > buy_box["max_commute_min_peak"]:
        dq.append(f"commute {a.commute.minutes:.0f} min exceeds "
                  f"{buy_box['max_commute_min_peak']}-min cap")
    if uw.piti_owner > buy_box["max_monthly_piti"]:
        dq.append(f"PITI ${uw.piti_owner:,.0f}/mo exceeds BAH governor "
                  f"${buy_box['max_monthly_piti']:,}/mo — unaffordable on BAH")
    if listing.year_built and listing.year_built < 1950:
        dq.append("pre-1950 — high risk of VA Minimum Property Requirement blockers")

    cashflow_ok = uw.monthly_cash_flow >= min_cf

    vote = "YES" if (total >= threshold and cashflow_ok and not dq) else "NO"

    # ---- top reasons, plain English ----
    reasons = []
    if vote == "YES":
        ranked = sorted(subscores.items(), key=lambda kv: -kv[1]["points"])
        reasons = [f"{subscores[k]['why']}" for k, _ in ranked[:2]]
        reasons.append(f"score {total} ≥ {threshold} and rental cash flow clears "
                       f"${min_cf:,}/mo minimum")
    else:
        reasons.extend(dq)
        if not cashflow_ok:
            reasons.append(f"rental-conversion cash flow ${uw.monthly_cash_flow:,.0f}/mo "
                           f"is below the ${min_cf:,}/mo minimum")
        if total < threshold:
            worst = sorted(subscores.items(),
                           key=lambda kv: kv[1]["points"] - kv[1]["max"])[:2]
            for k, v in worst:
                reasons.append(f"weak {k.replace('_', ' ')}: {v['why']}")
        reasons = reasons[:3] or [f"score {total} below threshold {threshold}"]

    return Score(total=total, subscores=subscores, vote=vote,
                 reasons=reasons, disqualifiers=dq)
