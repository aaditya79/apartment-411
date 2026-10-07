# Apartment 411

**Get the 411 on any NYC apartment before you sign.**

Apartment 411 is a chat agent for New York City renters. Give it an address, paste a listing or attach your lease, and it tells you what the listing won't:
- Does the landlord fix things? What do tenants complain about?
- Rats and bedbugs, evictions and housing-court cases.
- How the owner runs their *other* buildings, and how this building compares with its neighbors.
- How much direct sun your window really gets, by floor and by side of the building.
- What the walk home from the subway looks like at night.
- Whether your lease follows New York rules, and what NY/NYC law says about heat, deposits and fees.

Then it helps you act: it drafts a repair letter that cites the city's own open violations.

It's built for someone about to sign a lease in NYC, or a current tenant whose landlord is slow to make repairs. Everything it says comes from public city records through tool calls, and every tool call is shown in the chat with its arguments and result.

**Live:** see `deploy_url` in [`submission.json`](submission.json) (Columbia login via IAP). The first query after a quiet spell can take up to a minute while city data loads.

## Sample queries for graders

Run these three in order, in one session. They're the first three cards on the start screen ("Start here"), word for word, so each is one click. The cards stay above the conversation after the first answer: scroll up or press **Examples** in the header to click the next one in the same session (only **New search** starts a new session):

1. **Should I rent here?** "I'm thinking of renting at 155 East 92nd Street in Manhattan. Should I worry about anything?"
2. **The landlord** "Who owns 155 East 92nd Street in Manhattan, and how do they treat tenants in their other buildings?"
3. **Fact-check a listing** "The listing says 'sun-drenched 4th floor in a well-maintained building' for 155 East 92nd Street. Is that true?"

What to expect:
- **Query 1** runs the building lookup, then maintenance, complaints, pests, court, landlord and neighborhood in parallel. It opens with the most important finding and its comparison (no overall score), then red flags, green flags and questions to ask.
- **Query 2** reuses the landlord portfolio from query 1: the buildings where the registered head officer appears on HPD registrations, where this building ranks among them, and the caveat that the match is by name.
- **Query 3** calls the listing fact-check and gives each claim its own verdict. "Well-maintained" is not supported by city records: the building's open violations per apartment are well above the median for nearby rentals, including hazardous ones. "Sun-drenched" can't be verified: direct sun on the 4th floor depends on which way the windows face — the street side gets a few hours today, the light-court side almost none.

The start screen has nine example cards, so every tool is one click away. After the three above, "More to try" has:
- **Sunlight** "How much direct sun does a 4th-floor apartment at 155 East 92nd Street, Manhattan get?"
- **Vs. the block** "Is 155 East 92nd Street, Manhattan worse than its block?"
- **Review a lease** attaches the fictional sample lease and reviews it.
- **Get a repair made** "I live at 155 East 92nd Street, Manhattan. My bathroom ceiling has been leaking for months. Write a letter to my landlord."
- **The walk home** "How safe is the walk home from the subway at night to 155 East 92nd Street, Manhattan?"
- **Know your rights** "What are my rights if my landlord won't fix the heat in NYC?"

## The tools

There are 13 tools in [`tools.py`](tools.py). ⭐ marks the six I believe are original to this project. The app shows the same list under "What I can check" on the start screen, and typing `/` in the chat box opens it as a menu. Picking a check shows it as a removable chip (e.g. `/estimate_sunlight ×`) and puts its example question in the input as a grey hint. You type your own question, or send it empty to ask the example. The pick is only a hint: it's passed to the model as a note to prefer that check if it fits, and the model still decides which tools to call. Each tool:
- returns JSON with interpreted facts, their context (per apartment, vs the area, over what period) and a `note` with the caveat;
- on failure, returns `{"error", "next_step"}` telling the model what to do next, never a stack trace;
- if it's about a building, takes an optional address, defaulting to the building being discussed, which the harness keeps in session state, so the model never handles city IDs. When one turn covers two buildings (a comparison), a call without an address is refused with an error asking for it, instead of silently using the last building looked up.

| Tool | What it answers | Data |
|---|---|---|
| `look_up_building` | Which building is this? Apartments, year built, floors, type, HPD registration, registered owner, head officer and agent. Rejects silent geocoder mismatches ("123 Fake Street" is not "123 West 123 St"), asks which borough when an address exists in several, and, when the match is in a different borough than the one named, says to add the ZIP code (GeoSearch ranks by ZIP, not borough name). | GeoSearch, PLUTO, HPD registrations and contacts |
| `check_maintenance_record` | Does the landlord fix things? Open violations by hazard class, per apartment, how long they've been open, and how fast violations since 2023 were closed. | HPD violations |
| `get_tenant_complaints` | What do tenants complain about? Complaints since 2023 by category and month, counted once per complaint, as a rate per 100 apartments (the unit every area comparison uses). | HPD complaints |
| `check_pests` | Rat inspections since 2023 and the owner's bedbug filings, plus the bedbug-disclosure rule when filings show infestations. | Health Dept. rodent inspections, HPD bedbug filings |
| `check_evictions_and_court` | Marshal evictions since 2023 and HPD housing-court cases, in plain English. | Evictions, HPD litigation |
| `get_landlord_portfolio` | Who is behind the LLC, and how do they run their other buildings? Lists the buildings where the same person is a registered contact (e.g. head officer, which isn't proof of ownership) and ranks this building among them. It also decides where that belongs in a report (`flag_placement`: red, green or neither, from this building's rate and rank against the portfolio), with a sentence the model copies, so a badly ranked building is never a green flag. Idea credit: JustFix's Who Owns What. | HPD contacts and registrations, PLUTO, evictions, litigation, snapshot |
| ⭐ `get_neighborhood_context` | Is this building better or worse than its block? Percentile of open violations per apartment among every registered rental within a radius, with ties handled, plus heat complaints per 100 apartments and the share of lots that failed a rat inspection. | PLUTO, HPD registrations, snapshot |
| ⭐ `draft_repair_request` | A polite, firm repair letter about the problems the tenant described, in formal wording, citing matching open violations (ID, date, code section) as supporting records, plus HPD's escalation steps. It never adds a condition the tenant didn't mention: with no problem stated it asks what's wrong (listing the kinds of problems city records show in the building, as examples), and other tenants' apartment numbers never go in the letter. | HPD violations, registration |
| ⭐ `estimate_sunlight` | How much direct sun does this window get? Details below. | DOB building footprints, NOAA sun position, GeoSearch |
| ⭐ `fact_check_listing` | Checks a listing's claims against city records: supported, partly supported, not supported, or can't verify. Details below. | Calls the tools above, plus 311 noise complaints |
| ⭐ `review_lease` | Reviews an uploaded or pasted lease against verified NY/NYC rules and city records. Details below. | NY/NYC law (official pages), HPD, Health Dept. |
| `tenant_rules` | What does NY/NYC law say about heat, security deposits, late fees, application and broker fees, and required lease disclosures? Returns only rules verified against an official page, each with its link, so general rights answers quote those figures instead of the model's memory. | NY/NYC law and HPD pages (the same verified set the lease review uses) |
| ⭐ `night_walk_check` | What does the walk home from the subway look like at night in the records? Details below. | NYPD complaints (current and historic), NYPD shootings, MTA stations, snapshot |

More detail on the original tools:
- **`estimate_sunlight`** ray-casts the sun past every nearby building's real roof height, every 5 minutes, for today, Dec 21, Mar 20 and Jun 21.
  - It labels each side of the building: street side (found from neighboring house numbers and one across the street), rear, side, light court or shared wall.
  - It reports a median plus an uncertainty range from 12 runs (floor height × window position × neighbors' heights ±10%), and names the building that blocks the sun most.
  - Walls facing the same way are numbered ("northeast side, wall 1 of 3"), so every side has a unique label.
  - With no floor, it sweeps every floor on the street side for Dec 21 and today. The panel draws the sweep as a bar chart by band of floors, and the model gets a summary (the range, where it changes most, the lowest floor with an hour of winter sun) instead of every band.
  - A floor the building doesn't have (above its floor count, or 0 or below) is refused with the real number of floors.
- **`fact_check_listing`** keeps billing terms ("heat included"), unit-level claims ("pristine") and brightness (which includes reflected light) apart from what building records can actually show, and states its rule in every evidence line.
  - The model must call it whenever listing language is quoted, and gives each claim its own verdict line. Near-duplicate claims ("bright" and "sunny" resting on the same finding) are merged into one verdict.
  - Its maintenance evidence includes the area median, and the building's complaint and rat-inspection counts from the same functions the other tools use, so the numbers agree everywhere.
  - Text addressed to an AI inside a listing ("ignore prior instructions", "[SYSTEM NOTE: …]") is removed before anything is judged, and the answer says it was found and ignored.
- **`review_lease`** works on a PDF, a Word .docx or plain text (upload up to 20 MB, or paste it). Scanned PDFs without a text layer are detected and the user is asked to paste the text.
  - It extracts rent, deposit, dates and fees with deterministic rules, with no model call inside the tool.
  - It flags internal inconsistencies, terms likely not allowed under NY law, and missing disclosures, each quoting the clause and linking the official source.
  - It cross-checks the lease with city records: registered owner vs the landlord named, apartment floor vs building floors, bedbug filings, and violations in the unit.
  - It first checks the document is a lease, with an explainable rule (landlord, tenant, rent amount, dated term and at least 6 of 8 lease markers). A research paper gets a named "doesn't look like a residential lease" error, with no flags and no missing-disclosure list. A rider or extract is reviewed as part of a lease, without disclosure findings.
  - It labels the review by the address in the document, never the building from the conversation, and says so when the two differ. Instructions addressed to an AI inside the lease are removed and noted, as in the fact-check.
- **`night_walk_check`** counts reported robberies, felony assaults, sex crimes, thefts from a person and shootings within ~60 m of the station-to-door line at night.
  - With no line named it checks every station within a 15-minute walk (up to 6); with a line, the nearest station on it.
  - Each walk carries a ready-made `incidents_phrase` (count plus its window, e.g. "2 reported incidents (9pm–5am, in the 12 months to 2026-06-30)"), which the model copies; if an answer still states a bare count, the app adds the tool's window to it, so a count never appears without its period.
  - It compares the result with about 4,000 same-length walks from every NYC station.
  - It never selects or mentions victim or suspect details.

Tools are plain functions with JSON schemas, following the class's harness: [`app.py`](app.py) is the starter's `run_agent` loop (LiteLLM → Gemini, up to 10 tool rounds), extended so one round's tool calls run in parallel (calls that pick a building run first), identical calls in a turn run once, and map-only arrays are trimmed from what the model reads.

## The app

The chat (left):
- **Start screen:** the nine example cards and "What I can check" (the 13 tools). They stay above the conversation, and **Examples** in the header scrolls back to them; clicking a card always sends into the current session. Each card is one control (heading, body and padding), also by keyboard.
- **Tool calls:** every call is a row with its name, the building it was for when a turn covers several, and a one-line summary; opening it shows the arguments and the result as colored JSON, with Copy buttons.
- **Progress:** while the agent works, a record lists each tool as it runs (from a read-only `GET /progress`). If it fails, the answer still arrives.
- **Leases:** attach a PDF, .docx or .txt with 📎. The chip shows "Attaching… N%", then "Reading…" (text extraction runs off the request loop), then "Lease attached". A message sent during the upload is held and sent when the file is ready; a failed upload says so on the chip, keeps the typed text and offers Retry.
- **`/` menu:** picks a check as a chip, a soft hint to the model.

The building file (right):
- **Map:** the building, nearby rentals colored by open violations per apartment, the landlord's other buildings, and night walks (Okabe-Ito colorblind-safe routes, with incidents in a distinct red). It stays within NYC.
- **Scorecards:** each leads with its number stated with its comparison ("9 open violations — 0.31 per apartment, vs an area median of 0.07"), in one unit per concept (violations per apartment, complaints per 100 apartments).
  - **Verdict chips:** "better than area", "about average", "worse than area" or "no record", only where a tool returned an area comparison. Within 25% of the area figure counts as about average. Cards without an area comparison (evictions, the landlord portfolio, sunlight) have no chip and say so.
  - **Summary strip:** under the heading, up to three verdicts that differ most from the area. It appears only once a tool has made an area comparison; with none, there's no strip and no chips, never a default verdict.
  - **Charts:** the portfolio shows each building's rate as a bar, with this building marked; the floor sweep and per-side sun are bar charts.
  - **Caveats:** every card keeps its caveat, in smaller type.
- **Design:** black ink and one yellow, used only for things to act on; grey for inert surfaces; red, amber and green for data verdicts only. Every text color meets 4.5:1 contrast (`python3 palettes/check_unified.py` measures it). It works down to 390 px wide, and every control has a visible focus ring.

## Guardrails

Rules the agent follows. [`tests/test_conversation.py`](tests/test_conversation.py) checks each of them except one that's a prompt rule only (zero out of zero is no evidence):
- **Every number comes from a tool result** in the conversation: no derived ratios, no mixed units (per apartment vs per 100 apartments), no overall score or "Verdict:" opener. Reports lead with the most important finding and its comparison.
- **Flags:** red only when worse than the area, with the comparison in the same sentence; a mixed or unfavorable result (several night walks that vary) is never a green flag; the landlord portfolio goes exactly where the tool's `flag_placement` says; a listing claim the records contradict is never green; zero out of zero is no evidence.
- **The landlord portfolio** is described as buildings where the person is a registered contact, never as what they own, always with the name-matching caveat.
- **Tenant rights:** statutory figures (temperatures, deadlines, caps) come only from `tenant_rules`, with their source links; the answer says it's general guidance, not a records lookup.
- **Scope:** questions outside NYC renting get a one- or two-sentence redirect with a suggestion; tenant-rights and how-to-read-a-listing questions are answered. Any address goes to the lookup, never judged by the model.
- **Floors:** a floor the building doesn't have is called out, with the real count.
- **Repair letters** state only conditions the tenant mentioned, in formal wording.
- **Documents:** instructions addressed to the assistant inside a listing or lease are ignored, and the answer says so.

## How it works

```
app.py              harness, session store, FastAPI: /chat, /upload, /sample-lease, /session, /clear
tools.py            the 13 tools, their JSON schemas, run_tool() (never raises)
nyc.py              data layer: Socrata client with retries and cache, checked geocoding, resolve_building()
sun.py              sun position, footprint ray-casting, street-side detection, uncertainty ensemble
lease.py            lease text extraction and NY/NYC rules (each with the official URL it was verified on)
build_snapshot.py   offline citywide aggregates -> data/snapshot.json.gz
index.html          the frontend (vanilla JS, Leaflet map, marked + DOMPurify)
palettes/           the color palettes explored and the contrast checkers (check_unified.py measures the live palette)
tests/              tool, session, lease, browser and end-to-end conversation tests
```

**Sessions** are server-issued `uuid4`s:
- **Only issued IDs are accepted:** resuming with a forged or unknown ID (the start-screen box, `?session=<id>` or `GET /session`) is rejected with "No session found with that ID." A chat message that carries one is never attached to it: the server answers in a fresh session it issued itself, so two clients can never share one.
- **Refresh:** a cookie resumes the session after a page refresh.
- **Resume by ID:** paste a session ID into the start screen, or open `?session=<id>`, to continue a chat in another browser. The messages, tool-call cards and building panel come back.
- **Scoped to the visitor:** resuming only works for whoever started the session: the same Columbia account (from IAP's `X-Goog-Authenticated-User-Email` header) when deployed, or the same browser locally. Anyone else gets "No session found with that ID".
- **New search:** starts a fresh session and drops the old session's attached lease.
- **In memory:** sessions live in memory, so they're lost when Cloud Run restarts the instance.

**The snapshot** (`data/snapshot.json.gz`, built 2026-10-05) holds citywide counts that are too slow to query live, for neighborhood comparisons and baselines:
- open violations, complaints, heat complaints and rodent inspections per lot;
- subway stations;
- 12 months of night-time street incidents and shootings (2025-06-30 to 2026-06-30, the latest NYPD data);
- OpenStreetMap groceries and gyms.

Each tool says how old the data it used is.

## Sunlight validation

My building, 4th floor, checked against [ShadeMap](https://shademap.app) and my own experience:

- **Street side (facing northwest, ~299°):** the model gives about 1 h of direct sun on Oct 5 (range 0.9–1.8 h), roughly 2:20–3:20pm in the typical run. ShadeMap shows the street in sun at 2pm and the facade in shadow by 4pm. That matches ShadeMap within about 10–20 minutes.
- **Shaft-facing window** (side walls facing the ~3.7 m gaps to the neighbors): the model gives little or no direct sun, which matches my experience ("it's right next to the neighbouring building").
- **Rear end wall** (8 m, facing southeast; about 4.2 h today in the model, because the buildings behind sit on lower ground): unvalidated.

## Data sources

| Source | Used for |
|---|---|
| [NYC GeoSearch](https://geosearch.planninglabs.nyc/) | Address → BBL/BIN/coordinates |
| [PLUTO](https://data.cityofnewyork.us/d/64uk-42ks) | Apartments, year built, floors, owner of record |
| [HPD registrations](https://data.cityofnewyork.us/d/tesw-yqqr), [contacts](https://data.cityofnewyork.us/d/feu5-w2e2) | Owner, head officer, agent; landlord portfolio |
| [HPD violations](https://data.cityofnewyork.us/d/wvxf-dwi5) | Maintenance record, repair letters |
| [HPD complaints](https://data.cityofnewyork.us/d/ygpa-z7cr) | Tenant complaints |
| [HPD litigation](https://data.cityofnewyork.us/d/59kj-x8nc), [evictions](https://data.cityofnewyork.us/d/6z8x-wfk4) | Court cases and evictions |
| [Bedbug filings](https://data.cityofnewyork.us/d/wz6d-d3jb), [rodent inspections](https://data.cityofnewyork.us/d/p937-wjvj) | Pests |
| [Building footprints](https://data.cityofnewyork.us/d/5zhs-2jue) | Roof heights for the sun model |
| [NYPD complaints (current year)](https://data.cityofnewyork.us/d/5uac-w243), [historic](https://data.cityofnewyork.us/d/qgea-i56i), [shootings](https://data.cityofnewyork.us/d/5ucz-vwe8) | Night walk |
| [311 service requests](https://data.cityofnewyork.us/d/erm2-nwe9) | Noise complaints (listing "quiet" claims) |
| [MTA subway stations](https://data.ny.gov/d/39hk-dx4f) | Nearest station |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Map tiles; groceries and gyms in the snapshot |
| NY/NYC law pages ([nysenate.gov](https://www.nysenate.gov/legislation), [nyc.gov](https://www.nyc.gov/site/hpd/index.page), [hcr.ny.gov](https://hcr.ny.gov/leases), [DCWP](https://www.nyc.gov/site/dca/about/FAQ-Broker-Fees.page)) | Lease rules, violation deadlines, repair escalation steps |

Data quirks I found and handle:
- **Shootings:** the file has latitude and longitude swapped in every row through 2025, and two time formats.
- **GeoSearch:** it silently answers with its nearest guess, even for addresses that don't exist.
- **NYPD lag:** NYPD publishes about 3 months behind.

## Credits

- [JustFix's Who Owns What](https://whoownswhat.justfix.org/) inspired the landlord-portfolio idea.
- [ShadeMap](https://shademap.app) was used to validate the sunlight model.
- Map data and the grocery and gym locations are © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.

## Known limits

- **Coverage:** full records exist only for HPD-registered rentals with 3+ apartments. Houses, many co-ops and condos have little or no HPD data. Sunlight and the night walk work anywhere.
- **"Open" ≠ unfixed:** an open violation can be fixed but never certified by the owner.
- **Landlord matching** is by the person's name on HPD registrations. It can include a namesake, and it misses LLCs run by other officers.
- **Sunlight is direct sun only:** no reflected light, trees, fire escapes, window recesses or buildings newer than the footprint data.
- **Crime figures are reported incidents only,** with NYPD locations offset to intersections, and the walk is a straight-line corridor. Counts describe places and times, not people.
- **No commute planning:** commute estimates were cut, and the agent says so instead of guessing.
- **Lease review and tenant rules are a checklist, not legal advice.** They only state rules I could verify on an official page.
- **City data lag:** city records update daily at best; NYPD lags about 3 months; neighborhood comparisons use the snapshot (date above).
- **Sessions live in memory** on one Cloud Run instance, so they're lost if the instance restarts (including resuming by ID).

## Run it locally

You need:
- a Google Cloud project with Vertex AI enabled;
- `gcloud auth application-default login`;
- [uv](https://docs.astral.sh/uv/).

Then:

```bash
uv run app.py                     # http://localhost:8000
```

Optional extras:

```bash
export NYC_OPEN_DATA_APP_TOKEN=…  # optional: a free NYC Open Data app token avoids throttling
uv run python -m tools "155 East 92nd Street, Manhattan"   # every tool once, with timings
uv run python -m tests.test_sessions                    # session rules (no model calls)
uv run python -m tests.test_lease                       # the sample lease's planted issues
uv run python -m tests.test_tools                       # harness and tool behavior (no model calls)
uv run python -m tests.test_conversation                # end to end with the model, against a running server
uv run --with playwright python -m tests.test_ui        # the page in Chrome: cards, uploads, sessions, map, panel
python3 palettes/check_unified.py                       # contrast and color-distance checks for the palette
uv run build_snapshot.py                                # rebuild the citywide snapshot (~10 min)
```

## Deployment

Cloud Run (us-east1), with continuous deploy from this repo's `main` branch:
- **Build:** Google Cloud's buildpacks, entrypoint `uvicorn app:app --host 0.0.0.0 --port $PORT`.
- **Access:** Identity-Aware Proxy for columbia.edu.
- **Secrets:** `NYC_OPEN_DATA_APP_TOKEN` is read from Secret Manager, never committed.
- **Instance:** 1 vCPU and 512 MiB (the server peaked at ~330 MB with 3 parallel sessions and a PDF lease review), min instances 1, max instances 1, CPU always allocated.

Why those settings:
- **One instance:** sessions are in memory, so there must be exactly one instance.
- **Always-on CPU:** keeps the cache-warming thread running, so the graders' first queries are fast.
- **Not less than 1 vCPU:** Cloud Run only allows that with request-based billing and a concurrency of 1, which would make a second visitor wait for the first.

**Cost:** about **$44/month** (≈ $1.47/day) at Cloud Run's instance-based rates ($0.000018/vCPU-s, $0.000002/GiB-s, after the monthly free tier), plus a little for Gemini calls.

> **After grades are released, set min instances to 0 (or delete the service) to stop the always-on charge.**

## Submission

Authors: aup2005
