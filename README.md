# omerta

## Data layer: ACS district profiles

`scripts/pull_acs.py` pulls 2023 ACS 5-year estimates for all 51 Hawaii State House and 25 State Senate districts (2022 boundaries) from the Census API and writes `data/districts.json`.

### Run it

1. Get a free key at https://api.census.gov/data/key_signup.html and add it to `.env` in the repo root:
   ```
   CENSUS_API_KEY=your_key_here
   ```
2. Install dependencies and run:
   ```
   python -m venv .venv
   .venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   python scripts/pull_acs.py
   ```

The script checks every variable ID (estimate and MOE) against the dataset's `variables.json` before it requests any data, and stops if one is missing. It should report 76 records.

### What's in `data/districts.json`

Each district record has `chamber`, `district`, `geoid`, `name`, and `metrics`. Every metric includes:

- `value` and `moe` (90% margin of error)
- `table_id`, `variable_ids`, `variable_labels`, `computation`
- `dataset` ("ACS 2023 5-year"), `boundary_vintage` ("2022 districts"), `source_url` (API query, no key)
- `low_reliability`: true when MOE / estimate > 30%

Metrics: total population, median age, median household income, poverty rate, SNAP households (count and rate), renter cost burden rate (30%+ of income on rent), veterans (count and rate), and limited-English household rate.

### District context from the 2021 Reapportionment Commission

`scripts/build_district_context.py` builds `data/district_context.json` from the State of Hawaiʻi [2021 Reapportionment Commission Final Report and Reapportionment Plan](https://elections.hawaii.gov/wp-content/uploads/2021-Reapportionment-Commission-Final-Report-952-pages-February-25-2022.pdf), published by the Office of Elections. For each district it records:

- the areas and communities the district covers, verbatim from Appendix D (District Boundary Descriptions), with the page number;
- the permanent-resident population the Commission used to draw it, from Tables 1–10, with page numbers;
- the report's own statement that non-permanent military and student residents were removed from the U.S. Census population to form that base.

Every figure is parsed from the PDF and cross-checked:

- each district's population matches between the island-unit and statewide tables;
- each table's deviations add up;
- each chamber's districts sum to the stated total;
- the census total minus the people removed equals the adjusted total.

Anything that fails a check is left out.

```
python scripts/build_district_context.py            # downloads the ~400 MB report
python scripts/build_district_context.py --pdf PATH  # or use a local copy
```

`get_district_profile` returns this as `district_context`. It also includes a side-by-side of the ACS total population and the reapportionment population. The difference is computed by the tool, not the model.

### Assistance programs

`data/programs.json` describes Hawaiʻi assistance programs: SNAP, Med-QUEST, WIC, H-HEAP (Hawaiʻi's LIHEAP), TANF/TAONF, the HPHA Housing Choice Voucher (Section 8) program, school meals, Kūpuna Care, veterans' services, and the Child Care Subsidy.

- **Sourcing:** every fact carries a verbatim quote from an official agency page. Income figures appear only where the agency publishes them.
- **Verification:** `python scripts/verify_programs.py` re-fetches each page and checks every quote word for word. It stamps `date_verified` only if every quote passes.
- **Tools:** `find_programs(need, household)` and `district_program_context(chamber, district)` describe programs as "may qualify, confirm with the agency". They never decide eligibility.

## Chat agent

`agent/chat.py` is a Claude agent (model set by `MODEL` at the top of the file) that answers questions using only the tools in `agent/tools.py`. Add `ANTHROPIC_API_KEY` to `.env`, then:

```
python -m agent.chat
```

## Web app

```
uvicorn web.app:app --reload
```

Then open http://127.0.0.1:8000. The FastAPI backend ([web/app.py](web/app.py)) keeps the Anthropic key on the server and exposes one `POST /chat` endpoint, which returns the answer, its citations, any low-reliability values it states, and the tool results used. Requests are rate-limited per IP (default 6 per minute and 150 per day) and globally (30 per minute), with at most 4 running at once. Override these with `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_DAY`, `RATE_LIMIT_GLOBAL_PER_MINUTE` and `CHAT_MAX_CONCURRENT`. Behind a reverse proxy, set `TRUST_PROXY=1` so limits apply per client rather than per proxy.

The frontend ([web/static/](web/static/)) is plain HTML, CSS and JS with no build step, built to meet WCAG 2.1 AA.

## Tests

`python -m pytest -m "not live"` runs every test that doesn't call the API. That includes endpoint tests with a mocked agent and the browser accessibility checks, which need `python -m playwright install chromium` once.

`python -m pytest` runs everything. The live guardrail tests in `tests/test_chat_guardrails.py` call the API and are skipped when `ANTHROPIC_API_KEY` isn't set. Run only those with `python -m pytest -m live -s` to see token usage.

## Notes on the data

Rates are computed from counts. Their MOEs use the Census Bureau's approximation formulas: sqrt of summed squared MOEs for sums and differences, and the proportion formula for rates (falling back to the ratio formula when the proportion formula's radicand is negative).
