"""
commute.py — drive time from each candidate to Fort Eisenhower.

Provider ladder (spec §3):
  1. Google Distance Matrix with departure_time = weekday 5 PM  -> TRUE PM-peak.
     Only used if GOOGLE_MAPS_API_KEY is set (needs a billing account).
  2. OpenRouteService (free key)                                -> OFF-PEAK only.
     Labeled as an estimate everywhere it appears.
  3. Haversine distance / 32 mph average                        -> crude fallback,
     clearly labeled, used only when no router key exists or a call fails.

Every result is cached per rounded (lat,lng) so an address is routed once.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from cache import Cache
from models import CommuteResult

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
COMMUTE_TTL_DAYS = 90  # roads don't move


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _next_weekday_5pm() -> int:
    """Unix ts of the next weekday 17:00 local — Google's true-peak departure."""
    now = datetime.now()
    candidate = now.replace(hour=17, minute=0, second=0, microsecond=0)
    while candidate <= now or candidate.weekday() >= 5:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=17)
    return int(candidate.timestamp())


def _round_key(lat: float, lng: float) -> dict:
    return {"lat": round(lat, 4), "lng": round(lng, 4)}  # ~11 m precision


def commute_to_base(lat: Optional[float], lng: Optional[float],
                    base_lat: float, base_lng: float,
                    cache: Cache) -> CommuteResult:
    if lat is None or lng is None:
        return CommuteResult(label="no coordinates — commute unknown")

    params = _round_key(lat, lng)
    google_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    ors_key = os.environ.get("ORS_API_KEY", "").strip()

    # -- 1. Google, true PM peak ------------------------------------------
    if google_key:
        cached = cache.get("google_commute", params, COMMUTE_TTL_DAYS)
        if cached is None:
            try:
                resp = requests.get(GOOGLE_URL, params={
                    "origins": f"{lat},{lng}",
                    "destinations": f"{base_lat},{base_lng}",
                    "departure_time": _next_weekday_5pm(),
                    "key": google_key}, timeout=20)
                resp.raise_for_status()
                el = resp.json()["rows"][0]["elements"][0]
                if el.get("status") == "OK":
                    dur = el.get("duration_in_traffic", el["duration"])["value"]
                    cached = {"min": dur / 60.0, "mi": el["distance"]["value"] / 1609.34}
                    cache.set("google_commute", params, cached)
            except Exception as exc:  # fall through to ORS; never echo the URL (key!)
                print(f"    Google commute failed ({type(exc).__name__}); "
                      f"falling back to ORS.")
        if cached:
            m = cached["min"]
            return CommuteResult(minutes=m, miles=cached["mi"], provider="google",
                                 is_peak=True, label=f"{m:.0f} min (PM-peak, Google)")

    # -- 2. OpenRouteService, off-peak ------------------------------------
    if ors_key:
        cached = cache.get("ors_commute", params, COMMUTE_TTL_DAYS)
        if cached is None:
            try:
                resp = requests.get(ORS_URL, params={
                    "api_key": ors_key,
                    "start": f"{lng},{lat}",
                    "end": f"{base_lng},{base_lat}"}, timeout=20)
                resp.raise_for_status()
                summary = resp.json()["features"][0]["properties"]["summary"]
                cached = {"min": summary["duration"] / 60.0,
                          "mi": summary["distance"] / 1609.34}
                cache.set("ors_commute", params, cached)
            except Exception as exc:  # never echo the URL — it contains the key
                print(f"    ORS commute failed ({type(exc).__name__}); "
                      f"falling back to straight-line estimate.")
        if cached:
            m = cached["min"]
            return CommuteResult(minutes=m, miles=cached["mi"],
                                 provider="openrouteservice", is_peak=False,
                                 label=f"{m:.0f} min (OFF-PEAK estimate, ORS)")

    # -- 3. Haversine fallback ---------------------------------------------
    miles = haversine_miles(lat, lng, base_lat, base_lng)
    minutes = miles / 32.0 * 60.0 * 1.25  # 32 mph avg, +25% for road routing
    return CommuteResult(minutes=minutes, miles=miles, provider="haversine",
                         is_peak=False,
                         label=f"~{minutes:.0f} min (straight-line ESTIMATE — no router available)")
