# Hawaiʻi District Profiles

**Live demo:** https://know-your-district.onrender.com · No login needed.

## Evaluate in 3 minutes

**1. Ask three questions.** Open the [live demo](https://know-your-district.onrender.com) and paste each of these into the question box:

| Paste this | What to look for |
|---|---|
| `What is the median household income in Senate District 12?` | A cited figure with its margin of error, and the badge **✓ All N numbers verified against cited sources** above the answer. Every number was checked against the data the tools returned before the answer was shown. |
| `What share of households in House District 51 are limited-English speaking?` | A **low-reliability caveat** next to the figure: its margin of error is more than 30% of the estimate, so the answer says to treat it with caution rather than presenting it as precise. |
| `What assistance programs might help a family of 4 on Oʻahu with a child under 5?` | Programs taken from official agency pages, each fact quoted with its source. The answer says the family *may qualify* and should confirm with the agency; it never states eligibility as fact. |

Open **Sources** under any answer to see the table, year and source link behind each figure.

**2. Open the [district map](https://know-your-district.onrender.com/map).** Choose a measure to shade the districts. Hatched districts have low-reliability estimates. A sortable table is the accessible alternative to the map. Select a district to put a question about it in the chat box, then press **Ask**.

**3. Open a one-page profile:** [State Senate District 12](https://know-your-district.onrender.com/district/senate/12/profile). It's print-ready, built straight from the data with no AI involved, and includes the district's legislator with the source and date. Any district's profile is one click from the chat page (under **Build a question**) and from each row of the map's table.

The [About page](https://know-your-district.onrender.com/about) explains the sources, how answers are checked, and the limitations.

---

An AI assistant that gives Hawaiʻi legislators and staff sourced, nonpartisan social and economic profiles of all 51 State House and 25 State Senate districts. Built for HAIC 2026.

- **Chat:** ask in plain language. Every figure comes from a data tool and is cited (source, table, year, boundary vintage), with caveats for low-reliability estimates.
- **District map:** shade districts by any measure, with a sortable table alternative. Select a district to prefill a question about it in the chat.
- **One-page profiles:** a print-ready page for any district, including its serving legislator with source and fetch date, built straight from the data with no AI involved.
- **Assistance programs:** official facts about SNAP, Med-QUEST, WIC and others, framed as "may qualify, confirm with the agency".
- **Legislators:** which district a legislator represents, and who represents a district, from the member listings on capitol.hawaii.gov. Names are as written there; party is not recorded. Every legislator shown, in chat answers, under them in the UI, and on the one-page profile, comes with the note "as listed on capitol.hawaii.gov on November 10, 2026, a listing that does not reflect the Nov 3, 2026 general election". When a name isn't found, the answer says the roster may predate the election, not just "not found".
- **About:** `/about` explains the sources, the grounding guarantee and the limitations.

## Architecture

```mermaid
flowchart LR
  subgraph Sources["Official sources (fetched by build scripts)"]
    ACS["Census API<br/>ACS 2020–2024 5-year"]
    RC["2021 Reapportionment<br/>Commission final report (PDF)"]
    AG["Agency web pages<br/>(DHS, DOH, HPHA, DOE, ...)"]
    CB["Census 2022 cartographic<br/>boundary files"]
    CAP["capitol.hawaii.gov<br/>House + Senate member listings"]
  end
  subgraph Build["scripts/ (verify, then write)"]
    P1["pull_acs.py<br/>checks variable IDs, computes MOEs"]
    P2["build_district_context.py<br/>parses + cross-checks tables"]
    P3["verify_programs.py<br/>re-checks every quote"]
    P4["build_map.py<br/>simplifies shapes"]
    P5["fetch_legislators.py<br/>one serving member per district"]
  end
  subgraph Data["data/"]
    D1[districts.json]
    D2[district_context.json]
    D3[programs.json]
    D4[district_shapes.json]
    D5[legislators.json]
  end
  subgraph Tools["agent/ tools (pure functions, no model)"]
    T1["get_district_profile · compare_districts<br/>rank_districts · statewide_context"]
    T2["find_programs · district_program_context<br/>list_available_metrics"]
    T3["find_district_by_legislator"]
  end
  LLM["Claude (agent/chat.py)<br/>narrates tool output only"]
  WEB["FastAPI web/app.py<br/>/chat · /map · /district/…/profile · /about · /readyz"]
  UI["Browser UI<br/>plain HTML/CSS/JS, WCAG 2.1 AA"]

  ACS --> P1 --> D1
  RC --> P2 --> D2
  AG --> P3 --> D3
  CB --> P4 --> D4
  CAP --> P5 --> D5
  D1 & D2 & D5 --> T1
  D3 & D1 --> T2
  D5 --> T3
  T1 & T2 & T3 <--> LLM
  LLM --> WEB
  T1 --> WEB
  D4 --> WEB
  WEB <--> UI
```

The model never reads the data files directly. It calls the tools, and the tools return values with their citations. The map and the one-page profiles bypass the model entirely.

## How grounding works

1. **Facts are built and verified before the model sees them.**
   - `pull_acs.py` confirms every Census variable ID against `variables.json`, fetches estimates with margins of error, and flags values where MOE / estimate > 30%.
   - `build_district_context.py` cross-checks every reapportionment figure in the PDF (island tables against statewide tables, sums against the stated totals) and leaves out anything that fails.
   - `verify_programs.py` re-fetches each agency page and confirms every quoted fact word for word.
2. **Tools return values, never prose.** Every value carries a citation and ready-formatted display strings, plus a caveat if it's low reliability. Any number a comparison needs is computed by the tool, so the model never does arithmetic: the ACS-vs-reapportionment population gap, confidence ranges, medians of districts.
3. **The system prompt forbids anything else.** The model may state only numbers from this turn's tool results. It must cite every figure, carry every caveat, never call someone eligible for a program, and say so when the data can't answer a question.
4. **Every answer is checked before it's shown** (`check_answer_runtime` in [agent/grounding.py](agent/grounding.py)):
   - every number in an answer must match a number in the tool results (rounding to the stated precision is allowed; numbers the user typed may be restated);
   - every low-reliability value stated must have its caveat next to it;
   - every program named must come from a tool result;
   - every legislator named must come from a tool result: any roster legislator named by first and last name, and any name after "Senator" or "Representative". Each must carry the roster's as-listed note;
   - no sentence may state eligibility as fact.
5. **Failed answers get one retry, then a visible warning.** `/chat` runs the check on every answer. If it fails, the model gets the specific problems once and is asked to fix them using only tool results; the rejected draft is removed from the conversation. If the second answer still fails, it is returned with `verified: false` and the list of problems, and the UI shows a "Not verified" warning above the answer. Every response carries `verification: {verified, numbers_checked, numbers_supported, retried, issues}`. A verified answer shows "✓ All N numbers verified against cited sources". Both results are text (not just color) and are announced by the page's live region.
6. **The live guardrail suite uses the same function**, plus question-specific checks. It caught the model computing a confidence range itself, which led to the tools providing `range_display`. The web layer also marks the low-reliability values an answer states, so the UI can flag them visibly.

## Run it

To evaluate, you don't need to install anything: use the [live demo](https://know-your-district.onrender.com). To run it yourself you need Python 3.12 and, for chat only, your own Anthropic API key. No database or other service is needed.

```
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt       # runtime + build scripts + tests, pinned (requirements.txt is runtime only)
python -m playwright install chromium  # only for the browser accessibility tests
```

Create `.env` in the repo root:

```
CENSUS_API_KEY=...       # only needed to rebuild data/districts.json
ANTHROPIC_API_KEY=...    # needed for chat
```

Start the web app:

```
uvicorn web.app:app --reload
```

Then open http://127.0.0.1:8000. You can also chat in a terminal with `python -m agent.chat`.

The data files are committed. To rebuild them from the sources:

```
python scripts/pull_acs.py                # verifies every variable ID first; the release year is ACS_YEAR at the top
python scripts/compare_acs.py             # old-vs-new report after changing releases (reports/acs_<old>_vs_<new>.json)
python scripts/build_district_context.py   # downloads the ~400 MB report, or pass --pdf PATH
python scripts/verify_programs.py          # re-verifies program quotes and stamps date_verified
python scripts/build_map.py
python scripts/fetch_legislators.py       # re-fetches the roster; fails unless every district has one serving member
```

## Configuration

All settings are environment variables (or `.env` locally). On Render, set them on the service's **Environment** page; see [DEPLOY.md](DEPLOY.md). Nothing secret is in the code or the repo.

| Variable | Default | Demo value | Meaning |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | *(none)* | your key | Required for chat. Without it the map, profiles and `/about` still work and chat says it's unavailable. |
| `CHAT_MODEL` | `claude-sonnet-5-5` | `claude-sonnet-5-5` | Claude model for chat |
| `CHAT_EFFORT` | `medium` | `medium` | Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`) |
| `CHAT_MAX_TOKENS` | `8000` | `8000` | Max tokens per model response |
| `CHAT_MAX_TOOL_CALLS` | `10` | `10` | Tool calls allowed per question |
| `CHAT_SERVER_FALLBACK` | `1` | `1` | Retry on a fallback model if a safety filter declines (`0` to disable) |
| `RATE_LIMIT_PER_MINUTE` | `6` | `30` | Questions per client IP per minute. Raise it for the demo: everyone on a venue's wifi shares one IP. |
| `RATE_LIMIT_PER_DAY` | `150` | `600` | Questions per client IP per day (same shared-IP reason) |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | `30` | `60` | Questions per minute across all users; this is the cap on API spend |
| `RATE_LIMIT_GLOBAL_PER_DAY` | `500` | `500` | Questions per day across all users: a hard daily cap on API spend. When it's reached, chat says the demo has hit today's limit and points to the map, profiles and about page, which keep working. The 24-hour window slides; it doesn't reset at midnight. |
| `CHAT_MAX_CONCURRENT` | `4` | `4` | Questions processed at once (more get "busy, try again") |
| `CHAT_MAX_MESSAGES` | `20` | `20` | Questions plus answers per conversation (so 10 questions); then the user starts a new conversation |
| `SESSION_TTL_MINUTES` | `60` | `60` | A conversation held on the server expires after this many minutes without a question |
| `SESSION_MAX` | `200` | `200` | Conversations held in memory at once; when full, the least recently used idle one is dropped |
| `CHAT_MAX_MESSAGE_CHARS` | `2000` | `2000` | Max characters in one message |
| `CHAT_MAX_TOTAL_CHARS` | `20000` | `20000` | Max characters across all questions in one conversation |
| `ALLOWED_ORIGINS` | *(none)* | *(leave unset)* | Comma-separated origins allowed cross-origin access. The UI is served from the same origin, so it doesn't need this. |
| `TRUST_PROXY` | `0` | `2` | How many proxies in front of the app append to `X-Forwarded-For`, so rate limits apply per client. The app uses the entry that many places from the right; entries further left are client-sent and can be forged. **Render needs `2`**: a request arrives as `<client>, <proxy>` (measured on the live service; Render doesn't document it). With too few entries, requests share one bucket. `0` ignores the header (local runs). |
| `PORT` | set by the host | *(set by Render)* | Port the web process binds to (the start command in `render.yaml`). Locally, uvicorn defaults to 8000. |
| `CENSUS_API_KEY` | *(none)* | *(not needed)* | Only for `scripts/pull_acs.py` when rebuilding data |

Rate limits and conversations live in memory in one process, so the start command runs a single uvicorn worker. Don't scale to more than one instance: each would keep its own limits.

## Deploy

Hosted on Render at https://know-your-district.onrender.com: one always-on Python web service on the **Starter** instance type, deployed automatically from `main`. [render.yaml](render.yaml) records the service settings (start command, `/healthz` health check, auto-deploy, environment variables; the API key is set only in the dashboard). [DEPLOY.md](DEPLOY.md) covers where the key lives, checking `/readyz?deep=1`, confirming the deployed commit, and suspending the service after the event. Render reads `.python-version` (3.12) and installs the runtime-only `requirements.txt`.

## Operations

- `GET /healthz`: liveness.
- `GET /readyz`: public, and limited to what monitoring needs: `ready`, `chat_available`, data counts (districts, context, programs, map shapes, serving legislators), data dates (program verification, roster fetch) and whether chat is configured. `?deep=1` adds one field, `ai_service`: whether the AI service is reachable with the configured key (this uses no tokens). The model, effort, rate limits and proxy setting are not exposed by any endpoint; check them on the host's Environment page.
- **AI service down or out of credits:** the user sees a clear message ("temporarily unavailable…", "overloaded…") with links to the map and the about page, which still work. Upstream error text, keys and stack traces never reach the browser; details go to the server log with the request ID.
- **Request log:** each `/chat` request writes one JSON line to stdout (`"event":"chat"`): request ID (also the `X-Request-ID` response header), status and error code, latency in ms, session status and turn, tool calls and their names, verification result, and token counts: input, output, cache read and cache creation (totals, `cache_hit_ratio`, and an `api_calls` list with the same four counts for each model call). It never logs the question, the answer or tool inputs. Unexpected errors are logged as the exception type and the request ID only (`chat failed: ValueError request_id=…`), never the exception message or a traceback, because those can quote input. Questions don't reach the host's access logs either: they are sent only in the `POST /chat` body, never in a URL. When the map hands a question to the chat page, it travels in the URL fragment (`/#ask=…`), which browsers never send to the server; the page puts it in the question box, removes it from the address bar, and waits for the user to press Ask. Older `/?ask=…` links are prefilled the same way, but a query string is sent to the server, so the map no longer creates them.
- **Prompt caching:** every model request has two cache breakpoints, the system prompt and the last block of the newest message (a question or a batch of tool results). Each call writes the conversation so far, and the next call in the same conversation (the tool round-trip or a follow-up question) reads it back, earlier tool results included, at the cache-read price. The marker is added when the request is sent and never stored, so the history never carries more than one. In the log, a healthy follow-up shows most of its prompt under `cache_read_input_tokens` and only the new turn under `cache_creation_input_tokens`. Cache entries last 5 minutes from their last use, so a follow-up asked after a longer pause pays to write the conversation again.
- **Daily cap:** `RATE_LIMIT_GLOBAL_PER_DAY` (default 500) stops chat for everyone once the day's questions are used, with a friendly message pointing to the map and profiles.
- **Conversations are held on the server.** The browser sends only a random session ID and its new question; it never sends earlier answers, so a client can't plant text in the model's history. Each session keeps the full conversation (including tool calls and tool results) in memory for 60 minutes without activity (`SESSION_TTL_MINUTES`), up to `SESSION_MAX` sessions. Grounding checks use every tool result from the session, so a follow-up can restate an earlier figure. Each answer's Sources panel lists the lookups made for it plus any earlier lookup in the conversation whose result supports a number or fact the answer states (a distinctive number, a legislator or a program; district numbers and years alone don't count), with its citations and source links, labelled "from an earlier lookup in this conversation". An unknown or expired session ID starts a new conversation, and the UI says so. A server restart ends all conversations.
- **Security:** the API key stays on the server. The pages run under a strict content security policy with no inline scripts, plus HSTS, `nosniff`, no-referrer and frame-blocking headers. Model output is rendered with DOM APIs, never `innerHTML`. `POST /chat` bodies over 16 KB are rejected with a JSON `413` before they are parsed. A link can prefill a question but never send one. There are no accounts, cookies or analytics.

## Third-party services, AI models and data

Everything the live demo needs at review time is the Render service and the Anthropic API. The data sources are read only by the build scripts; their results are committed in `data/`, so the app doesn't call them while it runs.

| What | Used for | Needed while the app runs? |
|---|---|---|
| [Anthropic Claude API](https://docs.anthropic.com), model `claude-sonnet-5-5` | Writes chat answers from tool results. Questions and tool results are sent to it. | Yes, for chat only. The map, profiles and `/about` work without it. |
| [Render](https://render.com) (Starter web service) | Hosts the live demo | Yes |
| [U.S. Census Bureau API](https://www.census.gov/data/developers/data-sets/acs-5year.html), ACS 2020–2024 5-year (and the ACS 2023 5-year release for the archived comparison) | District measures and margins of error | No (`scripts/pull_acs.py`) |
| Census cartographic boundary files, 2022 | District shapes on the map | No (`scripts/build_map.py`) |
| [2021 Reapportionment Commission final report](https://elections.hawaii.gov/wp-content/uploads/2021-Reapportionment-Commission-Final-Report-952-pages-February-25-2022.pdf), Hawaiʻi Office of Elections | Areas each district covers; permanent-resident population | No (`scripts/build_district_context.py`) |
| [capitol.hawaii.gov](https://www.capitol.hawaii.gov/legislature/legislators.aspx?chamber=H) member listings | Legislator roster | No (`scripts/fetch_legislators.py`) |
| Official agency pages on humanservices.hawaii.gov, medquest.hawaii.gov, health.hawaii.gov, hpha.hawaii.gov, dod.hawaii.gov, hawaiipublicschools.org, elderlyaffairs.com and benefits.va.gov | The 10 assistance programs' quoted facts | No (`scripts/verify_programs.py`) |

Open-source libraries: FastAPI, Uvicorn, the Anthropic Python SDK and python-dotenv at runtime ([requirements.txt](requirements.txt)); pytest, Playwright, axe-core and the build scripts' parsers for development ([requirements-dev.txt](requirements-dev.txt)). The browser UI uses no framework, CDN, font or analytics service. Code is under the [MIT License](LICENSE).

## Known limitations

- **The legislator roster doesn't reflect the November 3, 2026 general election.** It was fetched on November 10, 2026, when capitol.hawaii.gov still listed members for the 2026 regular session. Every name is shown with that note. Re-running `scripts/fetch_legislators.py` once the site lists the new Legislature updates it.
- **Census figures are survey estimates, not counts.** Small districts and small groups have large margins of error. Values whose margin of error is more than 30% of the estimate are flagged, not hidden.
- **12 measures and 10 programs only.** No unemployment, crime, school results, trends over time or areas smaller than a district. The assistant says so when asked and names where that data lives.
- **Program facts are a snapshot** quoted from agency pages and re-checked on 2026-11-07. Income limits and waiting lists change; the tool never says someone is eligible.
- **The grounding check verifies numbers, names, programs, caveats and eligibility wording, not every sentence.** An answer that passes can still be worded badly. An answer that fails after one retry is shown with a "Not verified" warning rather than blocked.
- **Chat depends on the Anthropic API and a spending cap.** The demo stops answering after 500 questions in 24 hours, or if the API is down or out of credit. The map and profiles keep working.
- **Answers arrive all at once (no streaming)** and can take up to a minute for a question that needs several lookups.
- **One server process, memory only.** Conversations and rate limits are lost on a restart or deploy, a conversation holds 10 questions, and the app can't scale past one instance without moving that state to a shared store.
- **The map draws 2022 boundary files** (the Census Bureau reports no change for Hawaiʻi since) and leaves out the Northwestern Hawaiian Islands at map scale.
- **English only**, with Hawaiian place names spelled with ʻokina and kahakō. The last live guardrail run (2026-11-13, on the current 2024 data) passed 12 of 12 questions; that is a small fixed set, not proof that every possible answer is grounded, which is why every answer is also checked at runtime (see [Tests](#tests)).

## Data

| File | Source | Notes |
|---|---|---|
| `data/districts.json` | U.S. Census Bureau ACS 2020–2024 5-year, 2024 district boundaries | 12 measures per district: total population, median age, median household income, poverty rate, SNAP households (count and rate), renter cost burden, veterans (count and rate), Armed Forces (count and rate), limited-English households. Each has its MOE, table, variable IDs and source URL. |
| `data/district_context.json` | 2021 Reapportionment Commission Final Report and Plan (Office of Elections) | Areas covered (Appendix D) and permanent-resident population (Tables 1–10), with page citations. |
| `data/programs.json` | Official agency pages | 10 programs, every fact with a verbatim quote and verification date. |
| `data/district_shapes.json` | Census 2022 cartographic boundary files (from TIGER/Line) | Simplified to 73 KB. The Northwestern Hawaiian Islands are not drawn at map scale. |
| `data/legislators.json` | Hawaiʻi State Legislature member listings ([House](https://www.capitol.hawaii.gov/legislature/legislators.aspx?chamber=H), [Senate](https://www.capitol.hawaii.gov/legislature/legislators.aspx?chamber=S)), fetched 2026-11-10 (UTC) | Name as written on the site (party suffix removed), chamber, district, official profile URL, source URL, fetch date, and the site's own status text ("Appointed 08/09/2026", "Term of Office Ended: 06/30/2026"). The site says "(Showing Representatives/Senators for the 2026 regular session)" and doesn't mention the November 3, 2026 general election, so the file is marked **pre-election** membership. Two districts list a member whose term ended (House 28, Senate 18); they are kept with `serving: false`. No party affiliation. |
| `data/archive/districts_acs2023.json` | The previous release (ACS 2023 5-year, 2022 districts) | Kept for comparison; `reports/acs_2023_vs_2024.json` lists every value old vs new and the 31 low-reliability flags that changed (12 newly flagged, 19 no longer flagged). |

**Boundary vintage.** The Census Bureau lists the state legislative district vintage for the 2024 ACS as "2024 Legislative Session Year" ([Geography Boundaries by Year, 2024](https://www.census.gov/programs-surveys/acs/geography-acs/geography-boundaries-by-year.2024.html)), and for 5-year estimates says to "use the last year of the estimate period to determine the vintage." Its [2024 geography changes](https://www.census.gov/programs-surveys/acs/technical-documentation/table-and-geography-changes/2024/geography-changes.html) page does not list Hawaiʻi among the states with new plans and says all other states had no changes to their SLD boundaries from the 2022 cycle. The map still draws the 2022 cartographic boundary files.

Rates are computed from counts. Their MOEs use the Census Bureau's approximation formulas: the square root of summed squared MOEs for sums and differences, and the proportion formula for rates (falling back to the ratio formula when the proportion formula's radicand is negative).

## Tests

```
python -m pytest -m "not live"     # everything that doesn't call the AI service
python -m pytest -m live -s        # live guardrail suite (uses the API and costs tokens); reports p50/p95 latency per question
```

| Suite | What it checks |
|---|---|
| `test_legislators.py` | Roster only from capitol.hawaii.gov, no party, one serving member per district; every member found by name ignoring case/ʻokina/kahakō; shared names return candidates; no fuzzy matches; profile carries the legislator; name grounding check; the UI chip names a real legislator; the as-listed note travels with every legislator (tool results, /chat `roster_note`, one-pager, /about) and not-found says the roster may predate the election |
| `test_verification.py` | The runtime check (counts, each failure type), the one-retry loop in `/chat` (verified, fixed on retry, still failing, retry API error), and that `Agent.revise` drops the rejected draft from the history |
| `test_caching.py` | One moving `cache_control` marker on the last message of every request plus the system prompt's, never stored; each request starts with the previous request's messages (prefix stability); cache read and creation tokens per request and per API call in the log |
| `test_deploy.py` | `render.yaml` describes one Starter web service that binds `$PORT` on 0.0.0.0 with one worker, health-checks `/healthz`, auto-deploys `main`, and never contains the API key; Python version pinned; runtime requirements only; every requirement pinned with `==` to the version the tests ran on; every env var the app reads is documented; the client IP is the 2nd `X-Forwarded-For` entry from the right on Render, so a forged entry can't dodge the rate limit and visitors behind the same proxy aren't lumped together |
| `test_acs_release.py` | Data matches the one `ACS_YEAR` constant; boundary vintage documented; the archived release and the comparison report agree with both files |
| `test_tools.py`, `test_context.py` | All 76 districts load; values match the data; invalid input returns an error, never a guess; every response is cited; display strings use consistent decimals |
| `test_grounding.py`, `test_programs.py` | The number, caveat, program and eligibility checkers; program data is official, quoted, and has no unquoted numbers |
| `test_web.py`, `test_hardening.py` | `/chat` with a mocked agent; server-held sessions (history comes from the server, expiry, LRU cap, client-sent history rejected, failed turns not saved); validation, rate limiting, key never exposed, AI-service failures, readiness (`/readyz` shows no config, with or without `?deep=1`), unexpected errors logged as type and request ID only, the 16 KB body cap on `/chat`, the map handoff in the URL fragment with no auto-submit, CORS, config, `/about` |
| `test_profile_page.py`, `test_map.py` | One-page profile built only from tool data; map data matches the tools |
| `test_a11y.py` | axe-core (WCAG 2.0/2.1 A/AA plus best practices) on every page and state; keyboard paths; focus rings; 200% zoom; 375px phone width; profile prints on one page |
| `test_chat_guardrails.py` (live) | Real questions on Sonnet; every number traced to tool results, caveats present, programs sourced, no eligibility claims |

Latest offline run: 438 passed. Reports are in [reports/](reports/): `a11y_report.json`, `guardrail_report.json` (the last live run, 2026-11-13, on the current ACS 2020–2024 5-year data: 12/12 questions passed on `claude-sonnet-5-5`; latency per question p50 4.8 s, p95 15.4 s), `guardrail_report_acs2023.json` (the first live run, on the ACS 2023 5-year data: 7/10, or 9/10 after fixing the checker; an offline test replays it against the archived file) and `acs_2023_vs_2024.json`.
