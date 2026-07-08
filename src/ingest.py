"""
ingest.py — Stage 1 of the funnel: pull active listings and normalize them.

Sources:
  * RentCast /listings/sale — ONE area search per city/zip returns the whole
    pool (that's 1 billable call for many candidates, the cheapest way to
    spend the 50/month budget).
  * data/sample_listings.json — offline fixtures so the whole pipeline runs
    end-to-end with zero API calls (default when no key / --sample).

Every listing is normalized into models.Listing regardless of source.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import requests

from cache import Cache
from models import Listing

RENTCAST_BASE = "https://api.rentcast.io/v1"
SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_listings.json"

# RentCast propertyType values -> our normalized types
_TYPE_MAP = {
    "single family": "single_family",
    "single-family": "single_family",
    "condo": "condo",
    "townhouse": "townhouse",
    "manufactured": "manufactured",
    "mobile": "manufactured",
    "multi-family": "multi_family",
    "multi family": "multi_family",
    "apartment": "multi_family",
    "land": "land",
}


def _normalize_type(raw_type: str) -> str:
    return _TYPE_MAP.get((raw_type or "").strip().lower(), (raw_type or "unknown").lower())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize_rentcast_listing(item: dict, source: str = "rentcast") -> Listing:
    """Map one RentCast sale-listing record into our Listing dataclass."""
    hoa = item.get("hoa") or {}
    address = item.get("addressLine1") or item.get("formattedAddress", "")
    return Listing(
        id=item.get("id") or _slug(item.get("formattedAddress", address)),
        address=address,
        city=item.get("city", ""),
        state=item.get("state", "GA"),
        zip_code=str(item.get("zipCode", "")),
        county=item.get("county", "") or "",
        list_price=float(item.get("price") or 0),
        beds=float(item.get("bedrooms") or 0),
        baths=float(item.get("bathrooms") or 0),
        sqft=item.get("squareFootage"),
        lot_sqft=item.get("lotSize"),
        year_built=item.get("yearBuilt"),
        property_type=_normalize_type(item.get("propertyType", "")),
        construction=(item.get("construction") or "").lower(),
        hoa_monthly=float(hoa.get("fee") or 0),
        days_on_market=item.get("daysOnMarket"),
        latitude=item.get("latitude"),
        longitude=item.get("longitude"),
        status=(item.get("status") or "active").lower(),
        listing_url=item.get("listingUrl", ""),
        source=source,
        raw=item,
    )


def fetch_rentcast_listings(cache: Cache, rc_cfg: dict, geo: dict,
                            max_price: float, refresh: bool = False) -> list[Listing]:
    """One /listings/sale search per target city (or zip, if configured).

    Each area search = 1 billable RentCast call, cached for cache_ttl_days.listings.
    """
    api_key = os.environ.get("RENTCAST_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "RENTCAST_API_KEY is not set. Copy .env.example to .env and add your "
            "key (https://app.rentcast.io/app/api), or run with --sample."
        )

    ttl = 0 if refresh else rc_cfg["cache_ttl_days"]["listings"]
    cap = rc_cfg["free_monthly_call_cap"]
    warn = rc_cfg["warn_at_calls"]

    # Prefer explicit zips when provided (tighter areas), else target cities.
    areas = ([{"zipCode": z} for z in geo.get("zips") or []]
             or [{"city": c, "state": "GA"} for c in geo["target_cities"]])

    listings: list[Listing] = []
    for area in areas:
        params = {**area, "status": "Active", "propertyType": "Single Family",
                  "maxPrice": int(max_price), "limit": 500}
        cached = cache.get("/listings/sale", params, ttl)
        if cached is None:
            cache.check_budget(cap, warn)
            resp = requests.get(
                f"{RENTCAST_BASE}/listings/sale",
                params=params, headers={"X-Api-Key": api_key}, timeout=30)
            resp.raise_for_status()
            cached = resp.json()
            cache.set("/listings/sale", params, cached)
            used = cache.record_call()
            print(f"  RentCast listing search {area} -> {len(cached)} results "
                  f"({used}/{cap} calls this month)")
        else:
            print(f"  RentCast listing search {area} -> {len(cached)} results (cached)")
        listings.extend(normalize_rentcast_listing(item) for item in cached)

    # de-dupe across overlapping areas
    seen: set[str] = set()
    unique = [l for l in listings if not (l.id in seen or seen.add(l.id))]
    return unique


def load_sample_listings(path: Path = SAMPLE_PATH) -> list[Listing]:
    """Offline fixtures — same shape as RentCast records."""
    with open(path) as f:
        data = json.load(f)
    return [normalize_rentcast_listing(item, source="sample") for item in data]
