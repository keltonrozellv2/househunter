"""
enrich.py — Stage 2 of the funnel: the expensive per-property "CarFax" pull.

Only runs on candidates that cleared the Stage-1 prescore gate (or that the
user flagged serious). One property costs ~4 RentCast calls:
    value AVM + rent AVM + property records (comps ride along on the AVMs).

Everything is cached; a property already pulled inside its TTL costs zero.
Schools: no free tier gives clean ratings, so we surface the district and a
GreatSchools search URL instead of pretending to know (spec §3).
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Optional

import requests

from cache import Cache
from models import Enrichment, Listing

RENTCAST_BASE = "https://api.rentcast.io/v1"

# County school districts around Fort Eisenhower — heuristic mapping used when
# no boundary source (ATTOM) is enabled. City -> district name.
_DISTRICTS = {
    "grovetown": "Columbia County School District",
    "evans": "Columbia County School District",
    "martinez": "Columbia County School District",
    "harlem": "Columbia County School District",
    "appling": "Columbia County School District",
    "augusta": "Richmond County School System",
    "hephzibah": "Richmond County School System",
    "blythe": "Richmond County School System",
}

# Fallback county millage rates (total, incl. school + county M&O), used only
# when RentCast tax records don't include enough to derive the real one.
_COUNTY_MILLAGE_FALLBACK = {
    "richmond": 34.4,   # Augusta-Richmond consolidated, ballpark
    "columbia": 26.5,   # Columbia County, ballpark
}


def school_info(listing: Listing) -> tuple[str, str]:
    """(district_name, greatschools_search_url) for a listing — surfaced, not gated."""
    district = _DISTRICTS.get(listing.city.strip().lower(),
                              f"{listing.county or 'local'} school district")
    q = urllib.parse.quote(f"{listing.zip_code}")
    url = f"https://www.greatschools.org/search/search.page?q={q}"
    return district, url


def fallback_millage(listing: Listing, default: float) -> float:
    return _COUNTY_MILLAGE_FALLBACK.get((listing.county or "").strip().lower()
                                        .replace(" county", ""), default)


def _rc_get(cache: Cache, endpoint: str, params: dict, ttl_days: float,
            rc_cfg: dict, api_key: str) -> Optional[dict]:
    """Cached, budget-guarded RentCast GET. Returns None on a 404 (no data)."""
    cached = cache.get(endpoint, params, ttl_days)
    if cached is not None:
        return cached if cached != "__none__" else None
    cache.check_budget(rc_cfg["free_monthly_call_cap"], rc_cfg["warn_at_calls"])
    resp = requests.get(f"{RENTCAST_BASE}{endpoint}", params=params,
                        headers={"X-Api-Key": api_key}, timeout=30)
    used = cache.record_call()
    print(f"    RentCast {endpoint} ({used}/{rc_cfg['free_monthly_call_cap']} calls)")
    if resp.status_code == 404:
        cache.set(endpoint, params, "__none__")  # cache the miss too
        return None
    resp.raise_for_status()
    data = resp.json()
    cache.set(endpoint, params, data)
    return data


def deep_pull(listing: Listing, cache: Cache, rc_cfg: dict) -> Enrichment:
    """The Stage-2 CarFax pull: AVM + rent + records for ONE serious candidate."""
    api_key = os.environ.get("RENTCAST_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RENTCAST_API_KEY missing — cannot deep-pull live data.")

    ttl = rc_cfg["cache_ttl_days"]
    addr = listing.full_address
    e = Enrichment()

    avm = _rc_get(cache, "/avm/value", {"address": addr}, ttl["avm"], rc_cfg, api_key)
    if avm:
        e.avm_value = avm.get("price")
        e.avm_value_low = avm.get("priceRangeLow")
        e.avm_value_high = avm.get("priceRangeHigh")
        e.comps = avm.get("comparables") or []
        e.sources.append("RentCast value AVM")

    rent = _rc_get(cache, "/avm/rent/long-term", {"address": addr},
                   ttl["avm"], rc_cfg, api_key)
    if rent:
        e.rent_estimate = rent.get("rent")
        e.rent_low = rent.get("rentRangeLow")
        e.rent_high = rent.get("rentRangeHigh")
        e.sources.append("RentCast rent AVM")

    records = _rc_get(cache, "/properties", {"address": addr},
                      ttl["property_records"], rc_cfg, api_key)
    if records:
        rec = records[0] if isinstance(records, list) else records
        assessments = rec.get("taxAssessments") or {}
        taxes = rec.get("propertyTaxes") or {}
        if assessments:
            latest = assessments[max(assessments)]
            e.county_assessed_value = latest.get("value")
        if taxes:
            latest_tax = taxes[max(taxes)]
            e.tax_annual = latest_tax.get("total")
        # GA assesses at 40%: appraised (fair-market) value backs out of assessed
        if e.county_assessed_value:
            e.county_appraised_value = e.county_assessed_value / 0.40
        # derive an effective millage from actual tax / assessed when possible
        if e.tax_annual and e.county_assessed_value:
            e.millage_rate = e.tax_annual / e.county_assessed_value * 1000.0
        feats = rec.get("features") or {}
        if not listing.construction and feats.get("exteriorType"):
            listing.construction = str(feats["exteriorType"]).lower()
        if not listing.year_built and rec.get("yearBuilt"):
            listing.year_built = rec["yearBuilt"]
        e.sources.append("RentCast property records (county assessor data)")

    e.school_district, e.greatschools_url = school_info(listing)
    return e


def enrich_from_sample(listing: Listing) -> Enrichment:
    """Sample mode: enrichment fixtures ride inside the listing's raw payload."""
    s = listing.raw.get("sample_enrichment", {})
    e = Enrichment(
        avm_value=s.get("avm_value"),
        avm_value_low=s.get("avm_value_low"),
        avm_value_high=s.get("avm_value_high"),
        rent_estimate=s.get("rent_estimate"),
        rent_low=s.get("rent_low"),
        rent_high=s.get("rent_high"),
        comps=s.get("comps", []),
        county_appraised_value=s.get("county_appraised_value"),
        county_assessed_value=s.get("county_assessed_value"),
        millage_rate=s.get("millage_rate"),
        tax_annual=s.get("tax_annual"),
        sources=["SAMPLE DATA — not live market data"],
    )
    e.school_district, e.greatschools_url = school_info(listing)
    return e
