"""
report.py — per-property report cards, ranked results.csv, dashboard.html.

The dashboard is a single self-contained file: candidate data is embedded as
JSON at generation time (a file:// page can't fetch the CSV cross-origin), no
external scripts/fonts/services. Every report ends with the disclaimer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from models import Analysis

DISCLAIMER = ("This is decision-support, not financial or legal advice. "
              "Estimates are model outputs, not appraisals — verify everything "
              "with your lender, agent, and inspector.")

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"


def _money(x) -> str:
    return f"${x:,.0f}" if x is not None else "n/a"


def render_card(a: Analysis) -> str:
    """One-page markdown report card (also printed to console)."""
    l, e, v, uw, s, o = (a.listing, a.enrichment, a.value, a.underwrite,
                         a.score, a.offer)
    coc = ("n/a — near-zero cash in (VA $0 down)" if uw.cash_on_cash is None
           else f"{uw.cash_on_cash:.1%}")
    commute = a.commute.label if a.commute else "not computed"
    one_pct = uw.one_percent * 100

    lines = [
        f"# {l.full_address}",
        "",
        f"**VOTE: {s.vote}** — score {s.total}/100",
        *[f"- {r}" for r in s.reasons],
        "",
        "## The deal",
        f"| | |",
        f"|---|---|",
        f"| List price | {_money(l.list_price)} ({l.days_on_market if l.days_on_market is not None else '?'} days on market) |",
        f"| Est. market value | {_money(v.point)} (range {_money(v.low)}–{_money(v.high)}) |",
        f"| Value method | {v.method} |",
        f"| Value confidence | {v.confidence} |",
        f"| List-to-value spread | {v.list_to_value_spread:+.1%} (negative = deal) |",
        f"| **Recommended offer** | **{_money(o.offer_price)}** |",
        f"| **Recommended seller concession** | **{_money(o.concession_dollars)} ({o.concession_pct_of_price:.1%} of price)** |",
        "",
        f"*Offer strategy:* {o.strategy}",
        *[f"> {n}" for n in o.notes],
        "",
        "## While you live there (owner-occupied)",
        f"- Loan (incl. VA funding fee): {_money(uw.loan_amount)}",
        f"- Monthly P&I: {_money(uw.monthly_pi)}",
        f"- **PITI: {_money(uw.piti_owner)}/mo** (BAH governor: must stay ≤ $1,509)",
        f"- Property tax: {_money(uw.tax_owner_annual)}/yr (with homestead exemption)",
        "",
        "## When you convert it to a rental",
        f"- Projected rent: {_money(e.rent_estimate)}/mo"
        + (f" (range {_money(e.rent_low)}–{_money(e.rent_high)})" if e.rent_low else ""),
        f"- PITI as rental: {_money(uw.piti_rental)}/mo (homestead exemption lost; "
        f"tax rises to {_money(uw.tax_rental_annual)}/yr)",
        f"- Rent − PITI (headline test): {_money(uw.simple_rent_minus_piti)}/mo",
        f"- **True cash flow (after vacancy/maint/capex reserves): "
        f"{_money(uw.monthly_cash_flow)}/mo**",
        f"- Cap rate {uw.cap_rate:.2%} · DSCR {uw.dscr:.2f} · GRM {uw.grm:.1f} · "
        f"1% rule {one_pct:.2f}% · Cash-on-cash {coc}",
        f"- VA note: owner-occupancy (move in ≈60 days) is required now; renting "
        f"later is fine. When you buy your next home, VA bonus entitlement may "
        f"allow a second VA loan while keeping this one as a rental.",
        "",
        "## Property & area",
        f"- {l.beds:.0f} bed / {l.baths:.0f} bath · {l.sqft or '?'} sqft · "
        f"built {l.year_built or '?'} · {l.construction or 'unknown construction'}",
        f"- County: {l.county or '?'} — assessed {_money(e.county_assessed_value)}, "
        f"appraised {_money(e.county_appraised_value)}",
        f"- Commute to Fort Eisenhower: {commute}",
        f"- Schools: {e.school_district} — verify ratings: {e.greatschools_url}",
        "",
        "## Score breakdown",
        "| Factor | Points | Why |",
        "|---|---|---|",
        *[f"| {k.replace('_', ' ')} | {sv['points']}/{sv['max']} | {sv['why']} |"
          for k, sv in s.subscores.items()],
        "",
        f"*Data sources: {'; '.join(e.sources) or 'listing feed only'}*",
        "",
        f"---",
        f"*{DISCLAIMER}*",
    ]
    return "\n".join(lines)


def to_row(a: Analysis) -> dict:
    """Flatten one analysis into a CSV/dashboard row."""
    l, v, uw, s, o = a.listing, a.value, a.underwrite, a.score, a.offer
    return {
        "vote": s.vote,
        "score": s.total,
        "address": l.full_address,
        "city": l.city,
        "list_price": round(l.list_price),
        "est_value": round(v.point),
        "value_low": round(v.low),
        "value_high": round(v.high),
        "spread_pct": round(v.list_to_value_spread * 100, 1),
        "offer": round(o.offer_price),
        "concession": round(o.concession_dollars),
        "piti_owner": round(uw.piti_owner),
        "rent_est": round(a.enrichment.rent_estimate or 0),
        "cash_flow_rented": round(uw.monthly_cash_flow),
        "cap_rate_pct": round(uw.cap_rate * 100, 2),
        "dscr": round(uw.dscr, 2),
        "grm": round(uw.grm, 1),
        "commute_min": round(a.commute.minutes) if a.commute and a.commute.minutes else None,
        "beds": l.beds, "baths": l.baths, "sqft": l.sqft,
        "year_built": l.year_built,
        "construction": l.construction,
        "days_on_market": l.days_on_market,
        "top_reason": s.reasons[0] if s.reasons else "",
    }


def write_outputs(analyses: list[Analysis], out_dir: Path = ROOT) -> tuple[Path, Path]:
    """results.csv (ranked) + dashboard.html + per-property cards in reports/."""
    ranked = sorted(analyses, key=lambda a: -a.score.total)
    rows = [to_row(a) for a in ranked]

    csv_path = out_dir / "results.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    REPORTS_DIR.mkdir(exist_ok=True)
    for a in ranked:
        slug = a.listing.id.replace("/", "-")[:80]
        (REPORTS_DIR / f"{slug}.md").write_text(render_card(a))

    html_path = out_dir / "dashboard.html"
    html_path.write_text(_render_dashboard(rows))
    return csv_path, html_path


def _render_dashboard(rows: list[dict]) -> str:
    data = json.dumps(rows)
    n_yes = sum(1 for r in rows if r["vote"] == "YES")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fort Eisenhower Deal Scanner</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1d21; --muted:#6b7280; --line:#e5e7eb;
           --yes:#0a7f4b; --yes-bg:#e6f5ee; --no:#b3261e; --no-bg:#fbeae9;
           --accent:#1d4ed8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#101317; --fg:#e7eaee; --muted:#9aa2ad; --line:#2a2f36;
             --yes:#4ade80; --yes-bg:#12291d; --no:#f87171; --no-bg:#2d1615;
             --accent:#93b4ff; }} }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
          font:14px/1.5 system-ui, sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); margin-bottom:16px; }}
  .wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:8px; }}
  table {{ border-collapse:collapse; width:100%; min-width:1100px; }}
  th, td {{ padding:8px 10px; text-align:right; border-bottom:1px solid var(--line);
            white-space:nowrap; }}
  th {{ cursor:pointer; user-select:none; position:sticky; top:0;
        background:var(--bg); font-weight:600; }}
  th:hover {{ color:var(--accent); }}
  td.addr, th.addr {{ text-align:left; max-width:280px; overflow:hidden;
                      text-overflow:ellipsis; }}
  .vote {{ font-weight:700; padding:2px 8px; border-radius:10px; }}
  .YES {{ color:var(--yes); background:var(--yes-bg); }}
  .NO  {{ color:var(--no);  background:var(--no-bg); }}
  .neg {{ color:var(--no); }} .pos {{ color:var(--yes); }}
  .why {{ color:var(--muted); font-size:12px; max-width:340px; white-space:normal;
          text-align:left; }}
  .disclaimer {{ margin-top:16px; color:var(--muted); font-size:12px; }}
</style></head><body>
<h1>Fort Eisenhower Deal Scanner</h1>
<div class="sub">{len(rows)} candidates · {n_yes} BUY votes · click a column header to sort</div>
<div class="wrap"><table id="t"><thead><tr></tr></thead><tbody></tbody></table></div>
<p class="disclaimer">{DISCLAIMER}</p>
<script>
const DATA = {data};
const COLS = [
  ["vote","Vote"],["score","Score"],["address","Address"],["list_price","List $"],
  ["est_value","Est. value"],["spread_pct","Spread %"],["offer","Offer $"],
  ["concession","Concession $"],["piti_owner","PITI $"],["rent_est","Rent est."],
  ["cash_flow_rented","CF rented $"],["cap_rate_pct","Cap %"],["dscr","DSCR"],
  ["commute_min","Commute min"],["beds","Bd"],["baths","Ba"],["sqft","Sqft"],
  ["year_built","Built"],["days_on_market","DOM"],["top_reason","Top reason"]];
let sortKey = "score", asc = false;
const fmt = (k,v) => v==null ? "" :
  ["list_price","est_value","offer","concession","piti_owner","rent_est"].includes(k)
    ? "$"+v.toLocaleString()
  : k==="cash_flow_rented" ? "$"+v.toLocaleString() : v;
function render() {{
  const head = document.querySelector("thead tr");
  head.innerHTML = COLS.map(([k,label]) =>
    `<th class="${{k==='address'||k==='top_reason'?'addr':''}}" data-k="${{k}}">${{label}}${{
      k===sortKey ? (asc?" ▲":" ▼") : ""}}</th>`).join("");
  head.querySelectorAll("th").forEach(th => th.onclick = () => {{
    const k = th.dataset.k;
    if (k===sortKey) asc = !asc; else {{ sortKey = k; asc = (k==="address"); }}
    render();
  }});
  const rows = [...DATA].sort((a,b) => {{
    const x=a[sortKey], y=b[sortKey];
    const c = (x==null)-(y==null) || (typeof x==="string" ? String(x).localeCompare(String(y)) : x-y);
    return asc ? c : -c;
  }});
  document.querySelector("tbody").innerHTML = rows.map(r => `<tr>${{
    COLS.map(([k]) => {{
      if (k==="vote") return `<td><span class="vote ${{r.vote}}">${{r.vote==="YES"?"BUY":"PASS"}}</span></td>`;
      if (k==="address") return `<td class="addr" title="${{r.address}}">${{r.address}}</td>`;
      if (k==="top_reason") return `<td class="why">${{r.top_reason}}</td>`;
      const cls = (k==="cash_flow_rented"||k==="spread_pct") ? (r[k] < 0 === (k==="cash_flow_rented") ? "neg":"pos") : "";
      return `<td class="${{cls}}">${{fmt(k, r[k])}}</td>`;
    }}).join("")}}</tr>`).join("");
}}
render();
</script></body></html>"""
