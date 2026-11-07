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

Rates are computed from counts. Their MOEs use the Census Bureau's approximation formulas: sqrt of summed squared MOEs for sums and differences, and the proportion formula for rates (falling back to the ratio formula when the proportion formula's radicand is negative).
