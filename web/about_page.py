"""The /about page. Dates, vintages and counts are read from the data files."""

from __future__ import annotations

from html import escape

from agent import tools


def _e(x) -> str:
    return escape("" if x is None else str(x))


def render() -> str:
    from agent.chat import MODEL
    from agent.program_tools import load_programs

    meta = tools.load_data()["metadata"]
    counts = meta.get("counts_by_chamber", {})
    ctx = tools.load_context()
    progs = load_programs()
    prog_names = ", ".join(p["name"] for p in progs["programs"])
    try:
        import json
        from web.app import SHAPES_PATH
        shapes_src = json.loads(SHAPES_PATH.read_text(encoding="utf-8"))["source"]
    except Exception:
        shapes_src = None

    ctx_item = ""
    if ctx:
        ctx_item = (f"<li><strong>District context:</strong> {_e(ctx['source']['title'])}, submitted to the "
                    f"{_e(ctx['source']['submitted_to'])} (<a href=\"{_e(ctx['source']['url'])}\">PDF</a>). "
                    "Areas each district covers (Appendix D) and the permanent-resident population used to "
                    "draw it (Tables 1–10), with page citations. The Commission states that non-permanent "
                    "military and student residents were removed from that population base.</li>")
    map_item = ""
    if shapes_src:
        map_item = (f"<li><strong>Map boundaries:</strong> {_e(shapes_src['name'])}, {_e(shapes_src['vintage'])}. "
                    f"{_e(shapes_src['note'])}</li>")

    body = f"""
<a class="skip-link" href="#main">Skip to content</a>
<header class="site-header"><div class="wrap">
  <h1>About Hawaiʻi District Profiles</h1>
  <p class="tagline"><a href="/">Chat</a> · <a href="/map">District map</a></p>
</div></header>
<main id="main" class="wrap about">
<section aria-labelledby="what">
  <h2 id="what">What this tool does</h2>
  <p>It answers questions from Hawaiʻi legislators and staff about the social and economic profile of each of the
  {_e(counts.get('house'))} State House and {_e(counts.get('senate'))} State Senate districts. You can ask in plain
  language, compare and rank districts, see them on a map, look up assistance programs, and download a one-page
  profile of any district.</p>
  <p>It is <strong>nonpartisan</strong>. It describes what the data shows. It doesn't make policy
  recommendations, judge whether a value is good or bad, or speculate about causes.</p>
</section>

<section aria-labelledby="sources">
  <h2 id="sources">Data sources and vintages</h2>
  <ul>
    <li><strong>Census data:</strong> U.S. Census Bureau, American Community Survey, {_e(meta['dataset'])} estimates,
    for {_e(meta['boundary_vintage'])} (the boundaries drawn by the 2021 Reapportionment Commission). Margins of
    error are at the {_e(meta.get('moe_confidence_level'))} confidence level. Values are flagged low reliability when
    {_e(meta.get('low_reliability_rule'))}.</li>
    {ctx_item}
    <li><strong>Assistance programs:</strong> {_e(prog_names)}. Facts are quoted from official agency web pages,
    re-checked word for word on {_e(progs['metadata'].get('date_verified'))}. Income figures appear only where the
    agency publishes them.</li>
    {map_item}
  </ul>
</section>

<section aria-labelledby="grounding">
  <h2 id="grounding">The grounding guarantee</h2>
  <p>The AI model ({_e(MODEL)}) writes the sentences, but it doesn't supply the facts:</p>
  <ul>
    <li>Every number comes from a data tool that reads the files above. The model is told never to state a number
    that didn't come from a tool in the same conversation, and never to do arithmetic on tool output. Where a
    comparison needs a computed figure (such as the gap between Census and reapportionment populations), the
    tool computes it.</li>
    <li>Every figure is cited with its source, table, year and boundary vintage, and low-reliability values carry
    a caveat.</li>
    <li>Program descriptions come only from the program tools. The model never says someone is eligible; people
    "may qualify" and must confirm with the agency.</li>
    <li>Automated tests check real answers: every number in an answer must appear in that answer's tool results,
    flagged values must carry their caveat, programs must come from tool results, and eligibility must never be
    declared.</li>
    <li>The district map and the one-page profiles are built directly from the data files, with no AI model
    involved.</li>
  </ul>
</section>

<section aria-labelledby="limits">
  <h2 id="limits">Limitations</h2>
  <ul>
    <li>ACS figures are survey estimates averaged over five years, not counts. Small districts and small groups
    have large margins of error; read flagged values as approximate.</li>
    <li>The Census population and the reapportionment population measure different things (different years,
    methods and population bases) and are not expected to match.</li>
    <li>The tool covers only the measures and programs listed here. It doesn't know who represents a district,
    and it doesn't cover unemployment, crime, schools or trends over time.</li>
    <li>Program details change. Confirm eligibility, income limits and waiting lists with the agency.</li>
    <li>AI-written answers can still contain mistakes in wording. Check important figures against the cited
    sources.</li>
  </ul>
  <p>This tool is not an official product of the Hawaiʻi State Legislature, the Reapportionment Commission or the
  U.S. Census Bureau.</p>
</section>
</main>"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About · Hawaiʻi District Profiles</title>
<link rel="stylesheet" href="/static/styles.css">
</head>
<body>{body}
<footer class="site-footer"><div class="wrap"><p>Data: {_e(meta['dataset'])}; {_e(meta['boundary_vintage'])}.</p></div></footer>
</body>
</html>"""
