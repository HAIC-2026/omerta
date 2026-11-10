"""Print-ready one-page district profile, built only from tool data (never model text)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from agent import tools

CHAMBER_NAMES = {"house": "State House", "senate": "State Senate"}

# Display order and short row labels (the full ACS label is kept as a tooltip-free note).
ROWS = [
    ("total_population", "Total population"),
    ("median_age", "Median age (years)"),
    ("median_household_income", "Median household income"),
    ("poverty_rate", "Poverty rate"),
    ("snap_households", "Households receiving SNAP"),
    ("snap_household_rate", "Share of households receiving SNAP"),
    ("renter_cost_burden_rate", "Renters paying 30%+ of income on rent"),
    ("veterans", "Veterans (civilian, 18+)"),
    ("veteran_rate", "Veteran share of adults"),
    ("armed_forces", "People in the Armed Forces (16+)"),
    ("armed_forces_rate", "Armed Forces share of people 16+"),
    ("limited_english_household_rate", "Limited-English households"),
]


def e(x) -> str:
    return escape("" if x is None else str(x))


def render(chamber: str, district: int) -> tuple[int, str]:
    """Returns (status, html). Invalid input returns 404 with the tool's error message."""
    p = tools.get_district_profile(chamber, district)
    if "error" in p:
        body = f"<main><h1>District not found</h1><p>{e(p['error'])}</p><p><a href=\"/\">Back to the chat</a></p></main>"
        return 404, _page("District not found", body)

    meta = tools.load_data()["metadata"]
    title = f"Hawaiʻi {CHAMBER_NAMES[p['chamber']]} District {p['district']}"
    m = p["metrics"]
    keys = [k for k, _ in ROWS if k in m] + [k for k in m if k not in dict(ROWS)]
    labels = dict(ROWS)

    rows = []
    flagged = []
    for k in keys:
        v = m[k]
        lr = v["low_reliability"]
        if lr:
            flagged.append(k)
        mark = ('<span class="lr" aria-hidden="true">⚠</span>'
                '<span class="sr">Low reliability. </span>' if lr else "")
        rel = (f"Low: MOE is {e(v['moe_percent_of_estimate'])}% of estimate" if lr
               else (f"MOE is {e(v['moe_percent_of_estimate'])}% of estimate"
                     if v.get("moe_percent_of_estimate") is not None else "n/a"))
        rows.append(
            f"<tr{' class=\"flag\"' if lr else ''}><th scope=\"row\">{mark}{e(labels.get(k, v['label']))}</th>"
            f"<td>{e(v['value_display'] or 'n/a')}</td><td>{e(v['moe_display'] or 'n/a')}</td>"
            f"<td>{rel}</td><td>{e(v['table_id'])}</td></tr>")

    ctx = p.get("district_context") or {}
    ctx_html = ""
    if ctx:
        parts = []
        areas = (ctx.get("areas") or {})
        if areas:
            listed = areas.get("areas_as_written") or areas.get("islands_as_written") or []
            parts.append(
                f"<p><strong>Areas covered (as written in the report):</strong> {e(', '.join(listed))}."
                f"{' Includes House Districts ' + e(', '.join(map(str, areas['representative_districts_listed']))) + '.' if areas.get('representative_districts_listed') else ''}"
                f" <span class=\"cite\">Source: {e(areas['citation'])}.</span></p>")
        rp = ctx.get("reapportionment_population")
        if rp:
            parts.append(
                f"<p><strong>Reapportionment population:</strong> {e(rp['permanent_resident_population_display'])} "
                f"permanent residents (statewide target {e(f'{rp['statewide_target']:,}')}; "
                f"deviation {e(rp['statewide_deviation_pct'])}%). "
                f"<span class=\"cite\">Source: {e(rp['citation'])}.</span></p>"
                f"<p class=\"small\">Population basis, as stated by the Commission: “{e(rp['population_basis']['exclusion_verbatim'])}” "
                f"<span class=\"cite\">({e(rp['population_basis']['exclusion_citation'])})</span></p>")
        cmp_ = ctx.get("acs_vs_reapportionment")
        if cmp_:
            parts.append(
                f"<p><strong>ACS population minus reapportionment population:</strong> {e(cmp_['display'])}. "
                f"<span class=\"small\">{e(cmp_['note'])}</span></p>")
        ctx_html = ('<section aria-labelledby="ctx-h"><h2 id="ctx-h">District context</h2>'
                    + "".join(parts) + "</section>")

    leg = p.get("legislator") or {}
    leg_html = ""
    if leg.get("serving"):
        role = "Senator" if p["chamber"] == "senate" else "Representative"
        names = ", ".join(e(x["name_first_last"]) for x in leg["serving"])
        prof = leg["serving"][0]["profile_url"]
        # One header line (the page must print on one sheet); the full citation is in Sources.
        leg_html = (f"<p class=\"sub legislator\"><strong>{role}:</strong> {names}, "
                    f"{e(leg['as_listed_note'])}.</p>")

    rule = meta.get("low_reliability_rule", "")
    caveat_html = ""
    if flagged:
        caveat_html = (
            '<section aria-labelledby="cav-h"><h2 id="cav-h">Reliability caveats</h2>'
            f"<p><span aria-hidden=\"true\">⚠</span> {len(flagged)} value(s) are marked low reliability "
            f"({e(rule)}). Treat them as approximate and avoid drawing firm conclusions from small "
            "differences. Margins of error are at the "
            f"{e(meta.get('moe_confidence_level', ''))} confidence level.</p></section>")

    acs_tables = sorted({m[k]["table_id"] for k in keys})
    citations = [tools._citation(acs_tables)]
    if ctx.get("areas"):
        citations.append(ctx["areas"]["citation"])
    if ctx.get("reapportionment_population"):
        citations.append(ctx["reapportionment_population"]["citation"])
    if leg_html:
        citations.append(leg["citation"])
    src_urls = []
    first = m[keys[0]]["source_url"]
    src_urls.append(("Census API query", first if isinstance(first, str) else first[0]))
    if ctx.get("source_url"):
        src_urls.append(("2021 Reapportionment Commission final report (PDF)", ctx["source_url"]))
    if leg_html:
        src_urls.append(("Legislator profile (capitol.hawaii.gov)", prof))

    generated = datetime.now(timezone.utc).strftime("%B %d, %Y %H:%M UTC")
    body = f"""
<header>
  <p class="kicker">District profile · nonpartisan Census data</p>
  <h1>{e(title)}</h1>
  <p class="sub">{e(p['name'])}. Data: {e(meta['dataset'])}, {e(meta['boundary_vintage'])}. Generated {e(generated)}.</p>
  {leg_html}
  <p class="noprint"><button type="button" id="print-btn">Print or save as PDF</button> <a href="/">Back to the chat</a></p>
</header>
<main>
<section aria-labelledby="m-h">
  <h2 id="m-h">Key measures</h2>
  <table>
    <caption class="sr">Key measures for {e(title)}, with margins of error ({e(meta.get('moe_confidence_level', ''))} confidence)</caption>
    <thead><tr><th scope="col">Measure</th><th scope="col">Estimate</th><th scope="col">Margin of error</th><th scope="col">Reliability</th><th scope="col">ACS table</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
{caveat_html}
{ctx_html}
<section aria-labelledby="src-h">
  <h2 id="src-h">Sources</h2>
  <ul>{''.join(f'<li>{e(c)}</li>' for c in citations)}</ul>
  <p class="small">{' · '.join(f'<a href="{e(u)}">{e(label)}</a>' for label, u in src_urls)}</p>
  <p class="small">Every figure on this page comes directly from the data files behind the chat tool; none is written by an AI model. Not an official product of the Hawaiʻi State Legislature or the U.S. Census Bureau.</p>
</section>
</main>"""
    return 200, _page(title, body)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · Hawaiʻi District Profiles</title>
<link rel="stylesheet" href="/static/print.css">
<script src="/static/print.js" defer></script>
</head>
<body>
{body}
</body>
</html>"""
