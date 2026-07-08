"""
models.py — shared dataclasses for the deal-scanner pipeline.

`Listing` is the normalized shape every data source must map into (RentCast,
sample fixtures, or anything added later). `Analysis` is a Listing plus every
computed artifact the pipeline attaches on its way to the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    """One for-sale property, normalized from any source."""
    id: str                             # stable key (source id or address slug)
    address: str
    city: str
    state: str
    zip_code: str
    county: str = ""
    list_price: float = 0.0
    beds: float = 0
    baths: float = 0
    sqft: Optional[float] = None
    lot_sqft: Optional[float] = None
    year_built: Optional[int] = None
    property_type: str = "single_family"   # normalized: single_family, manufactured, ...
    construction: str = ""                  # brick, brick_veneer, masonry, frame, ...
    hoa_monthly: float = 0.0
    days_on_market: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    status: str = "active"
    listing_url: str = ""
    source: str = ""                        # rentcast | sample
    raw: dict = field(default_factory=dict)  # untouched source payload

    @property
    def full_address(self) -> str:
        return f"{self.address}, {self.city}, {self.state} {self.zip_code}"


@dataclass
class Enrichment:
    """Stage-2 deep-pull data attached to a serious candidate."""
    avm_value: Optional[float] = None          # RentCast value AVM
    avm_value_low: Optional[float] = None
    avm_value_high: Optional[float] = None
    rent_estimate: Optional[float] = None      # RentCast rent AVM (monthly)
    rent_low: Optional[float] = None
    rent_high: Optional[float] = None
    comps: list = field(default_factory=list)  # [{price, sqft, address, ...}]
    county_appraised_value: Optional[float] = None
    county_assessed_value: Optional[float] = None
    millage_rate: Optional[float] = None
    tax_annual: Optional[float] = None
    attom_avm: Optional[float] = None          # optional second opinion
    school_district: str = ""
    schools: list = field(default_factory=list)
    greatschools_url: str = ""
    sources: list = field(default_factory=list)  # provenance strings


@dataclass
class ValueEstimate:
    """Section-5 value output: point estimate + range + provenance."""
    point: float = 0.0                 # median of available estimates
    low: float = 0.0
    high: float = 0.0
    method: str = ""                   # e.g. "RentCast AVM + comps $/sqft + county appraisal"
    confidence: str = ""               # plain-English confidence note
    components: dict = field(default_factory=dict)  # name -> value used
    list_to_value_spread: float = 0.0  # (list - value) / value; negative = deal


@dataclass
class CommuteResult:
    minutes: Optional[float] = None
    miles: Optional[float] = None
    provider: str = ""                 # google | openrouteservice | haversine
    is_peak: bool = False              # only Google with departure_time is true peak
    label: str = ""                    # e.g. "28 min (OFF-PEAK estimate)"


@dataclass
class Score:
    total: float = 0.0                 # 0-100 weighted
    subscores: dict = field(default_factory=dict)   # factor -> (points, max, why)
    vote: str = "NO"                   # YES | NO
    reasons: list = field(default_factory=list)     # top reasons for the vote
    disqualifiers: list = field(default_factory=list)


@dataclass
class OfferRec:
    offer_price: float = 0.0
    strategy: str = ""                 # which tactic fired and why
    concession_dollars: float = 0.0
    concession_pct_of_price: float = 0.0
    notes: list = field(default_factory=list)


@dataclass
class Analysis:
    """Everything the pipeline knows about one candidate, ready to report."""
    listing: Listing
    enrichment: Enrichment = field(default_factory=Enrichment)
    value: Optional[ValueEstimate] = None
    commute: Optional[CommuteResult] = None
    underwrite: Optional[object] = None       # underwrite.UnderwriteResult
    score: Optional[Score] = None
    offer: Optional[OfferRec] = None
    prescore: Optional[float] = None          # Stage-1 cheap score (0-100)
    hard_filter_fails: list = field(default_factory=list)
    stage: str = "stage1"                     # stage1 | stage2
    notes: list = field(default_factory=list)
