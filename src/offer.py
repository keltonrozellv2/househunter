"""
offer.py — recommended offer price + seller-concession ask (spec §7).

Real buyer's-agent tactics, driven by (a) list-to-value spread and (b) days on
market:
  * stale + overpriced  -> anchor 3-8% under list, justify with comps
  * fresh, at/under val -> offer at or just under value
  * under value + hot   -> offer at value, win on terms, never chase past range
Concessions: target = min(closing costs - lender credit, 4% VA concession cap),
with the VA nuance (customary closing costs sit OUTSIDE the 4% cap) noted.
"""

from __future__ import annotations

from models import OfferRec, ValueEstimate


def recommend_offer(list_price: float, value: ValueEstimate,
                    days_on_market: int | None,
                    closing_costs_pct: float, lender_credit: float,
                    concession_cap_pct: float) -> OfferRec:
    dom = days_on_market if days_on_market is not None else 21  # assume normal
    spread = value.list_to_value_spread   # + = overpriced, - = under value
    est = value.point

    notes: list[str] = []

    # ---------------- offer price ----------------
    if spread > 0.02 and dom >= 45:
        # overpriced AND stale — anchor low
        offer = min(list_price * 0.94, est)
        strategy = (f"Stale listing ({dom} DOM) priced {spread:.1%} over value — "
                    f"anchor ~6% under list and justify with comps.")
    elif spread > 0.02:
        # overpriced but fresh — offer value, let them counter
        offer = est
        strategy = (f"Listed {spread:.1%} over est. value — offer at value "
                    f"(${est:,.0f}) and negotiate from the comps.")
    elif dom >= 45:
        # fairly priced but stale — modest discount
        offer = min(list_price * 0.97, est)
        strategy = (f"Fairly priced but stale ({dom} DOM) — seller fatigue "
                    f"justifies ~3% under list.")
    elif spread < -0.03 and dom <= 10:
        # under-priced and fresh = competitive; pay value, win on terms
        offer = min(est, value.high)
        strategy = (f"Listed {-spread:.1%} UNDER value with only {dom} DOM — "
                    f"likely competitive. Offer at est. value, lead with strength "
                    f"(fast close, clean terms). Do NOT exceed the value range "
                    f"(${value.high:,.0f}).")
    else:
        # normal case — at or just under the lower of list/value
        offer = min(list_price, est)
        strategy = "Priced near value — offer at the lower of list and est. value."

    offer = round(offer / 500) * 500  # human-looking number

    if offer > value.high:
        notes.append(f"⚠ offer exceeds top of value range (${value.high:,.0f}) — "
                     "do not proceed without a specific justification.")

    # ---------------- seller concession ----------------
    est_closing = offer * closing_costs_pct
    concession = min(max(0.0, est_closing - lender_credit),
                     offer * concession_cap_pct)
    concession = round(concession / 100) * 100
    pct = concession / offer if offer else 0.0

    notes.append(
        f"Ask nets against the ${lender_credit:,.0f} lender credit (est. closing "
        f"costs ~${est_closing:,.0f}) so you're not double-asking.")
    notes.append(
        "VA nuance: the 4% cap applies to 'seller concessions' (prepaids, points, "
        "funding fee, etc.). Seller-paid CUSTOMARY buyer closing costs are counted "
        "separately, so a well-structured ask can exceed 4% legally.")

    return OfferRec(offer_price=offer, strategy=strategy,
                    concession_dollars=concession,
                    concession_pct_of_price=pct, notes=notes)
