# Apartment 411

**Get the 411 on any NYC apartment before you sign.**

Apartment 411 is a chat agent for New York City renters. Give it an address, paste a listing or attach your lease, and it tells you what the listing won't:
- Does the landlord fix things? What do tenants complain about?
- Rats and bedbugs, evictions and housing-court cases.
- How the owner runs their *other* buildings, and how this building compares with its neighbors.
- How much direct sun your window really gets, by floor and by side of the building.
- What the walk home from the subway looks like at night.
- Whether your lease follows New York rules, and what NY/NYC law says about heat, deposits and fees.

It can also draft a repair letter that cites the city's own open violations.

I built it for someone about to sign a lease in NYC, or a tenant whose landlord is slow to fix things. Everything it says comes from public city records through tool calls, and every tool call is shown in the chat with its arguments and result.

**Live:** [https://apartment-411-j2i7dlw5aq-ue.a.run.app](https://apartment-411-j2i7dlw5aq-ue.a.run.app) (open, no sign-in needed). The first query after a quiet spell can take up to a minute while city data loads.

## Sample queries for graders

Run these three in order, in one session. They're the first three cards on the start screen ("Start here"), word for word, so each one is a single click. The cards stay above the conversation after the first answer, so you can scroll up (or press **Examples** in the header) and click the next one in the same session. Only **New search** starts a new session.

1. **Should I rent here?** "I'm thinking of renting at 155 East 92nd Street in Manhattan. Should I worry about anything?"
2. **The landlord** "Who owns 155 East 92nd Street in Manhattan, and how do they treat tenants in their other buildings?"
3. **Fact-check a listing** "The listing says 'sun-drenched 4th floor in a well-maintained building' for 155 East 92nd Street. Is that true?"

What to expect:
- Query 1 looks up the building, then runs maintenance, complaints, pests, court, landlord and neighborhood in parallel. The answer opens with the most important finding and its comparison (there's no overall score), then red flags, green flags and questions to ask.
- Query 2 reuses the landlord portfolio from query 1. It covers the buildings where the registered head officer appears on HPD registrations, where this building ranks among them, and the caveat that the match is by name.
- Query 3 runs the listing fact-check and gives each claim its own verdict. "Well-maintained" is not supported by city records, because the building has far more open violations per apartment than the median nearby rental, including hazardous ones. "Sun-drenched" can't be verified, because direct sun on the 4th floor depends on which way the windows face: the street side gets a few hours today and the light-court side almost none.

The start screen has nine example cards in total, so every tool is one click away. "More to try" has the other six:
- **Sunlight** "How much direct sun does a 4th-floor apartment at 155 East 92nd Street, Manhattan get?"
- **Vs. the block** "Is 155 East 92nd Street, Manhattan worse than its block?"
- **Review a lease** attaches the fictional sample lease and reviews it.
- **Get a repair made** "I live at 155 East 92nd Street, Manhattan. My bathroom ceiling has been leaking for months. Write a letter to my landlord."
- **The walk home** "How safe is the walk home from the subway at night to 155 East 92nd Street, Manhattan?"
- **Know your rights** "What are my rights if my landlord won't fix the heat in NYC?"

## The tools

There are 13 tools in [`tools.py`](tools.py). ⭐ marks the six I believe are original to this project.

The same list appears under "What I can check" on the start screen, and typing `/` in the chat box opens it as a menu. Picking a check shows it as a removable chip (e.g. `/estimate_sunlight ×`) and puts its example question in the input as a grey hint. You can type your own question or send it empty to ask the example. The pick is only a hint: the model is told the user would like that check, but it still decides which tools to call.

Every tool returns JSON with interpreted facts, their context (per apartment, compared with the area, over what period) and a `note` with the caveat. When something fails, it returns `{"error", "next_step"}` telling the model what to do next instead of a stack trace. Tools about a building take an optional address and otherwise use the building being discussed, which the harness keeps in session state, so the model never handles city IDs. If one turn covers two buildings (a comparison), a call without an address is refused with an error asking for one, so the tool can't quietly answer about the wrong building.

| Tool | What it answers | Data |
|---|---|---|
| `look_up_building` | Which building is this? Apartments, year built, floors, type, HPD registration, registered owner, head officer and agent. It rejects geocoder mismatches ("123 Fake Street" is not "123 West 123 St") and asks which borough when an address exists in several. If the match is in a different borough than the one you named, it suggests adding the ZIP code, since GeoSearch ranks by ZIP and not by borough name. | GeoSearch, PLUTO, HPD registrations and contacts |
| `check_maintenance_record` | Does the landlord fix things? Open violations by hazard class, per apartment, how long they've been open, and how fast violations since 2023 were closed. | HPD violations |
| `get_tenant_complaints` | What do tenants complain about? Complaints since 2023 by category and month, counted once per complaint, as a rate per 100 apartments (the same unit as the area comparison). | HPD complaints |
| `check_pests` | Rat inspections since 2023 and the owner's bedbug filings, plus the bedbug-disclosure rule when filings show infestations. | Health Dept. rodent inspections, HPD bedbug filings |
| `check_evictions_and_court` | Marshal evictions since 2023 and HPD housing-court cases, in plain English. | Evictions, HPD litigation |
| `get_landlord_portfolio` | Who is behind the LLC, and how do they run their other buildings? Lists the buildings where the same person is a registered contact (e.g. head officer, which isn't proof of ownership) and ranks this building among them. It also decides where the portfolio goes in a report (`flag_placement`: red, green or neither, based on this building's rate and rank within the portfolio) and gives the model a sentence to use, so a badly ranked building can't end up as a green flag. Idea credit: JustFix's Who Owns What. | HPD contacts and registrations, PLUTO, evictions, litigation, snapshot |
| ⭐ `get_neighborhood_context` | Is this building better or worse than its block? Percentile of open violations per apartment among every registered rental within a radius, with ties handled, plus heat complaints per 100 apartments and the share of lots that failed a rat inspection. | PLUTO, HPD registrations, snapshot |
| ⭐ `draft_repair_request` | A polite, firm repair letter about the problems the tenant described, in formal wording, citing matching open violations (ID, date, code section) as supporting records, plus HPD's escalation steps. It only includes conditions the tenant mentioned. If no problem was stated, it asks what's wrong and lists the kinds of problems city records show in the building as examples. Other tenants' apartment numbers never go in the letter. | HPD violations, registration |
| ⭐ `estimate_sunlight` | How much direct sun does this window get? Details below. | DOB building footprints, NOAA sun position, GeoSearch |
| ⭐ `fact_check_listing` | Checks a listing's claims against city records: supported, partly supported, not supported, or can't verify. Details below. | Calls the tools above, plus 311 noise complaints |
| ⭐ `review_lease` | Reviews an uploaded or pasted lease against verified NY/NYC rules and city records. Details below. | NY/NYC law (official pages), HPD, Health Dept. |
| `tenant_rules` | What does NY/NYC law say about heat, security deposits, late fees, application and broker fees, and required lease disclosures? It only returns rules I verified against an official page, each with its link, so answers about tenant rights quote those figures instead of the model's memory. | NY/NYC law and HPD pages (the same verified set the lease review uses) |
| ⭐ `night_walk_check` | What does the walk home from the subway look like at night in the records? Details below. | NYPD complaints (current and historic), NYPD shootings, MTA stations, snapshot |

More detail on the original tools:

**`estimate_sunlight`** ray-casts the sun past every nearby building's real roof height, every 5 minutes, for today, Dec 21, Mar 20 and Jun 21. It labels each side of the building as street side (found from neighboring house numbers and one across the street), rear, side, light court or shared wall, and numbers walls that face the same way ("northeast side, wall 1 of 3"). Each result is a median plus an uncertainty range from 12 runs that vary floor height, window position and the neighbors' heights by 10%, and it names the building that blocks the sun most. Without a floor, it sweeps every floor on the street side for Dec 21 and today: the panel draws that as a bar chart by band of floors, and the model only gets a summary (the range, where it changes most, and the lowest floor with an hour of winter sun). A floor the building doesn't have is refused with the real number of floors.

**`fact_check_listing`** separates what building records can show from billing terms ("heat included"), claims about the unit itself ("pristine") and brightness (which includes reflected light), and states its rule in every evidence line. The model has to call it whenever someone quotes a listing, and gives each claim its own verdict. Claims that rest on the same finding, like "bright" and "sunny", are merged into one verdict. The maintenance evidence includes the area median and the building's complaint and rat-inspection counts, taken from the same functions the other tools use so the numbers match. Any text in the listing that's addressed to an AI ("ignore prior instructions", "[SYSTEM NOTE: …]") is removed before anything is judged, and the answer says it was found and ignored.

**`review_lease`** takes a PDF, a Word .docx or plain text (upload up to 20 MB, or paste it), and asks you to paste the text if a PDF is a scan with no text layer.
- It extracts rent, deposit, dates and fees with deterministic rules. There's no model call inside the tool.
- It flags internal inconsistencies, terms that are likely not allowed under NY law, and missing disclosures, quoting the clause and linking the official source for each.
- It cross-checks the lease with city records: the registered owner against the landlord named, the apartment floor against the building's floors, bedbug filings, and violations in the unit.
- It first checks that the document is actually a lease, using a simple rule: landlord, tenant, a rent amount, a dated term, and at least 6 of 8 lease markers. A research paper gets a "doesn't look like a residential lease" error with no flags. A rider or extract is reviewed as part of a lease, without disclosure findings.
- It labels the review with the address in the document, not the building from the conversation, and points out when they differ. Instructions addressed to an AI are removed and noted, as in the fact-check.

**`night_walk_check`** counts reported robberies, felony assaults, sex crimes, thefts from a person and shootings within about 60 m of the straight line from the station to the door, at night. With no line named it checks every station within a 15-minute walk (up to 6); with a line, the nearest station on it. Each result is compared with about 4,000 walks of the same length from every NYC station. Each walk comes with a ready-made `incidents_phrase` that includes the time window, e.g. "2 reported incidents (9pm–5am, in the 12 months to 2026-06-30)". The model copies it, and if an answer still states a count without its window, the app adds it. The tool never selects or mentions victim or suspect details.

Tools are plain functions with JSON schemas, following the class's harness. [`app.py`](app.py) is the starter's `run_agent` loop (LiteLLM calling Gemini, up to 10 tool rounds). I extended it so the calls in one round run in parallel, with calls that choose a building running first, identical calls in a turn only run once, and long map-only arrays are left out of what the model reads.

## The app

The chat is on the left. The start screen has the nine example cards and "What I can check" (the 13 tools). Both stay above the conversation once it starts, and **Examples** in the header scrolls back to them; clicking a card always sends into the current session. Each tool call shows up as a row with its name, which building it was for (when a turn covers several) and a one-line summary. Opening a row shows the arguments and result as colored JSON with Copy buttons. While the agent works, a progress record lists each tool as it runs, using a read-only `GET /progress` endpoint; if that fails, the answer still arrives.

To review a lease, attach a PDF, .docx or .txt with 📎. The chip shows "Attaching… N%", then "Reading…" while the server extracts the text, then "Lease attached". If you send a message during the upload, it waits and goes out once the file is ready. A failed upload says so on the chip, keeps what you typed and offers Retry.

The building file is on the right. The map shows the building, nearby rentals colored by open violations per apartment, the landlord's other buildings, and night walks (Okabe-Ito colorblind-safe colors for routes, with incidents in a separate red). Below it, each scorecard leads with its number and the comparison together, for example 9 open violations at 0.31 per apartment next to an area median of 0.07. Violations are always per apartment and complaints always per 100 apartments.
- Cards get a chip saying "better than area", "about average", "worse than area" or "no record", but only when a tool returned an area comparison. Within 25% of the area figure counts as about average. Evictions, the landlord portfolio and sunlight have no area comparison, so they have no chip, and the card says so.
- A summary strip under the heading shows up to three of the comparisons that differ most from the area. It only appears once some tool has made an area comparison.
- The portfolio shows each building's rate as a bar with this building marked. The floor sweep and the per-side sun are bar charts too.
- Each card keeps its caveat, in smaller type.

The design uses black and one yellow, with yellow only for things you can act on. Grey is for inactive surfaces, and red, amber and green only appear on data verdicts. Every text color passes 4.5:1 contrast (`python3 palettes/check_unified.py` checks this), the layout works down to 390 px wide, and every control has a visible focus ring.

## Guardrails

These are rules the agent follows. [`tests/test_conversation.py`](tests/test_conversation.py) checks all of them except one, that zero out of zero is no evidence, which is only a prompt rule.
- Every number comes from a tool result in the conversation. No derived ratios, no mixing per apartment with per 100 apartments, and no overall score or "Verdict:" opener. Reports lead with the most important finding and its comparison.
- A red flag means worse than the area, and states the comparison in the same sentence. A mixed result, like several night walks that vary, is never a green flag. The landlord portfolio goes wherever the tool's `flag_placement` says. A listing claim the records contradict is never a green flag, and zero out of zero is no evidence.
- The landlord portfolio is described as buildings where the person is a registered contact, never as what they own, and always with the name-matching caveat.
- Legal figures (temperatures, deadlines, caps) only come from `tenant_rules`, with their source links, and those answers say they're general guidance rather than a records lookup.
- Questions outside NYC renting get a short redirect with a suggestion of what to ask instead. Questions about tenant rights or how to read a listing are answered. Any address goes to the lookup instead of the model deciding whether it's real.
- If someone names a floor the building doesn't have, the agent says so and gives the real number of floors.
- Repair letters only describe conditions the tenant mentioned, in formal wording.
- Instructions addressed to the assistant inside a listing or lease are ignored, and the answer says so.

## How it works

```
app.py              harness, session store, FastAPI: /chat, /upload, /sample-lease, /session, /clear
tools.py            the 13 tools, their JSON schemas, run_tool() (never raises)
nyc.py              data layer: Socrata client with retries and cache, checked geocoding, resolve_building()
sun.py              sun position, footprint ray-casting, street-side detection, uncertainty ensemble
lease.py            lease text extraction and NY/NYC rules (each with the official URL it was verified on)
build_snapshot.py   offline citywide aggregates -> data/snapshot.json.gz
index.html          the frontend (vanilla JS, Leaflet map, marked + DOMPurify)
palettes/           the color palettes I tried and the contrast checkers (check_unified.py measures the live one)
tests/              tool, session, lease, browser and end-to-end conversation tests
```

Sessions are `uuid4` IDs issued by the server, and only issued IDs are accepted. Resuming with a forged or unknown ID (through the start-screen box, `?session=<id>` or `GET /session`) is rejected with "No session found with that ID." A chat message carrying an unknown ID isn't attached to it either; the server answers in a new session of its own, so two clients can never share one. A cookie brings your session back after a page refresh, and you can paste a session ID into the start screen (or open `?session=<id>`) to continue in another browser, with the messages, tool calls and building panel restored. Resuming only works for whoever started the session: the same browser (by cookie), or, when IAP is on, the same Columbia account (from IAP's `X-Goog-Authenticated-User-Email` header). **New search** starts a fresh session and drops the old session's lease. Sessions live in memory, so they're lost when Cloud Run restarts the instance.

The snapshot (`data/snapshot.json.gz`, built 2026-10-05) holds citywide counts that are too slow to query live, for neighborhood comparisons and baselines:
- open violations, complaints, heat complaints and rodent inspections per lot;
- subway stations;
- 12 months of night-time street incidents and shootings (2025-06-30 to 2026-06-30, the latest NYPD data);
- OpenStreetMap groceries and gyms.

Each tool says how old the data it used is.

## Sunlight validation

I checked my own building, 4th floor, against [ShadeMap](https://shademap.app) and what I see from my windows:
- The street side faces northwest (about 299°). The model gives about 1 h of direct sun on Oct 5 (range 0.9 to 1.8 h), roughly 2:20 to 3:20pm in the typical run. ShadeMap shows the street in sun at 2pm and the facade in shadow by 4pm, so they agree within about 10 to 20 minutes.
- The windows on the side walls face gaps of about 3.7 m to the neighbors. The model gives them little or no direct sun, which matches my experience: it's right next to the neighbouring building.
- The rear wall (8 m wide, facing southeast) gets about 4.2 h today in the model, because the buildings behind sit on lower ground. I haven't been able to check that one.

## Data sources

| Source | Used for |
|---|---|
| [NYC GeoSearch](https://geosearch.planninglabs.nyc/) | Turning an address into BBL, BIN and coordinates |
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
| NY/NYC law pages ([nysenate.gov](https://www.nysenate.gov/legislation), [nyc.gov](https://www.nyc.gov/site/hpd/index.page), [hcr.ny.gov](https://hcr.ny.gov/leases), [DCWP](https://www.nyc.gov/site/dca/about/FAQ-Broker-Fees.page)) | Lease rules, tenant rules, violation deadlines, repair escalation steps |

Some data quirks I ran into and handle:
- The shootings file has latitude and longitude swapped in every row through 2025, and uses two time formats.
- GeoSearch quietly returns its nearest guess, even for addresses that don't exist.
- NYPD data is published about 3 months behind.

## Credits

- [JustFix's Who Owns What](https://whoownswhat.justfix.org/) inspired the landlord-portfolio idea.
- [ShadeMap](https://shademap.app) was used to validate the sunlight model.
- Map data and the grocery and gym locations are © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.

## Known limits

- Full records only exist for HPD-registered rentals with 3+ apartments. Houses and many co-ops and condos have little or no HPD data. Sunlight and the night walk work anywhere.
- An "open" violation isn't necessarily unfixed: the owner may have fixed it without getting it certified.
- Landlord matching uses the person's name on HPD registrations, so it can include a namesake and it misses LLCs run by other officers.
- Sunlight is direct sun only. It doesn't account for reflected light, trees, fire escapes, window recesses or buildings newer than the footprint data.
- Crime figures are reported incidents only. NYPD offsets locations to intersections, and the walk is a straight-line corridor. The counts describe places and times, not people.
- It doesn't plan commutes. I cut that feature, and the agent says so instead of guessing.
- The lease review and tenant rules are a checklist, not legal advice. They only state rules I could verify on an official page.
- City records update daily at best, NYPD data lags about 3 months, and neighborhood comparisons use the snapshot (date above).
- Sessions live in memory on one Cloud Run instance, so they're lost if the instance restarts, including for resuming by ID.

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

It runs on Cloud Run (us-east1) with continuous deploy from this repo's `main` branch. It's built with Google Cloud's buildpacks, with the entrypoint `uvicorn app:app --host 0.0.0.0 --port $PORT`. For grading it allows unauthenticated access, so no Columbia account is needed. During development it sat behind Identity-Aware Proxy for columbia.edu, which can be turned back on after grades (`gcloud run services update apartment-411 --region us-east1 --iap`, then remove the `allUsers` invoker binding). `NYC_OPEN_DATA_APP_TOKEN` is read from Secret Manager and never committed. The service has 1 vCPU and 512 MiB (the server peaked at about 330 MB with 3 sessions running at once and a PDF lease review), min and max instances set to 1, and CPU always allocated.

There's exactly one instance because sessions are in memory. CPU stays allocated so the cache-warming thread keeps running and the first queries are fast. It doesn't go below 1 vCPU because Cloud Run only allows that with request-based billing and a concurrency of 1, which would make a second visitor wait for the first.

It costs about **$44/month** (roughly $1.47/day) at Cloud Run's instance-based rates ($0.000018/vCPU-s, $0.000002/GiB-s, after the monthly free tier), plus a little for Gemini calls.

## Submission

Authors: aup2005
