"""
valuation.py — estimated market value with a range and provenance (spec §5).

Combines every available estimate:
  * RentCast value AVM (primary)
  * comps median $/sqft x subject sqft
  * county appraised (fair-market) value
  * ATTOM AVM if enabled
Point estimate = median of the components. Range = min..max, widened by the
AVM's own low/high band when it's the only component. Confidence is stated in
plain English — an AVM is never presented as an appraisal.
"""

from __future__ import annotations

import statistics
from typing import Optional

from models import Enrichment, Listing, ValueEstimate


def comps_value(comps: list, subject_sqft: Optional[float]) -> Optional[float]:
    """Median comp $/sqft × subject sqft. Needs sqft on both sides."""
    if not subject_sqft:
        return None
    ppsf = [c["price"] / c["squareFootage"]
            for c in comps
            if c.get("price") and c.get("squareFootage")]
    if not ppsf:
        return None
    return statistics.median(ppsf) * subject_sqft


def estimate_value(listing: Listing, e: Enrichment) -> ValueEstimate:
    components: dict[str, float] = {}

    if e.avm_value:
        components["RentCast AVM"] = e.avm_value
    cv = comps_value(e.comps, listing.sqft)
    if cv:
        components[f"comps $/sqft × {listing.sqft:.0f} sqft ({len(e.comps)} comps)"] = cv
    if e.county_appraised_value:
        components["county appraised value"] = e.county_appraised_value
    if e.attom_avm:
        components["ATTOM AVM (2nd opinion)"] = e.attom_avm

    if not components:
        # nothing better than list price — say so honestly
        return ValueEstimate(
            point=listing.list_price, low=listing.list_price * 0.95,
            high=listing.list_price * 1.05,
            method="list price only (no AVM/comps/county data available)",
            confidence="LOW — no independent estimate; range is a ±5% placeholder",
            components={}, list_to_value_spread=0.0)

    values = list(components.values())
    point = statistics.median(values)
    low, high = min(values), max(values)

    # widen with the AVM's own published band where available
    if e.avm_value_low:
        low = min(low, e.avm_value_low)
    if e.avm_value_high:
        high = max(high, e.avm_value_high)

    spread_pct = (high - low) / point if point else 0.0
    n = len(components)
    if n >= 3 and spread_pct <= 0.10:
        confidence = f"HIGH — {n} independent estimates within {spread_pct:.0%}"
    elif n >= 2 and spread_pct <= 0.15:
        confidence = f"MEDIUM — {n} estimates within {spread_pct:.0%}"
    else:
        confidence = (f"LOW — {n} estimate(s), range spans {spread_pct:.0%}; "
                      "treat as a rough guide, not an appraisal")

    lvs = (listing.list_price - point) / point if point else 0.0
    return ValueEstimate(
        point=point, low=low, high=high,
        method=" + ".join(components.keys()),
        confidence=confidence, components=components,
        list_to_value_spread=lvs)
