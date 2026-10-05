"""Apartment 411's tools, and the JSON that describes them to the model.

Every tool returns a JSON string: interpreted facts with context (per apartment,
vs the area, over what period), plus a `note` where a caveat applies. Failures
return {"error": ..., "next_step": ...} so the model can tell the user what to do.

Tools that look at a building take an optional `address`. When it is omitted they
use the building already being discussed, kept in the session's `state` by the
harness. The model never sees or passes BBLs or registration IDs.

Run `uv run python -m tools "184 Claremont Ave, Manhattan"` to try every tool.
"""

import json
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import nyc
from nyc import AddressError, DataSourceError

# --- Shared plumbing ---


class ToolError(Exception):
    """A failure the model can act on: what went wrong, and what to do next."""

    def __init__(self, error: str, next_step: str):
        super().__init__(error)
        self.error = error
        self.next_step = next_step


NO_BUILDING = ToolError("No building selected yet.",
                        "Ask the user for an NYC street address with borough, then call look_up_building.")

CERTIFICATION_NOTE = ("'Open' means HPD has no record of the repair being certified; some open violations "
                      "are fixed but never certified by the owner.")


def current_building(address: str | None, state: dict) -> nyc.Building:
    """The building a tool should look at: the one named, else the one being discussed."""
    if address:
        try:
            building = nyc.resolve_building(address)
        except AddressError as e:
            raise ToolError(e.message, (f"Ask the user to confirm: did they mean {e.suggestion}?" if e.suggestion else
                                        "Ask the user for the house number, street and borough (e.g. '184 Claremont Ave, "
                                        "Manhattan'). Intersections and neighborhood names can't be looked up."))
        state.setdefault("buildings", {})[building.bbl] = building
        state["current_bbl"] = building.bbl
        return building
    bbl = state.get("current_bbl")
    if not bbl:
        raise NO_BUILDING
    return state["buildings"][bbl]


def lot_filter(b: nyc.Building, boro: str = "boroid") -> dict:
    """Most HPD datasets are keyed by borough/block/lot rather than BBL."""
    boro_id, block, lot = nyc.split_bbl(b.bbl)
    return {boro: boro_id, "block": block, "lot": lot}


def per_apartment(count: int, units: int) -> float | None:
    return round(count / units, 2) if units else None


def short_date(value: str | None) -> str | None:
    return value[:10] if value else None


def month_label(value: str) -> str:
    return datetime.fromisoformat(value[:10]).strftime("%b %Y")


# --- Violation text: "§ 27-2026, 2027 HMC: PROPERLY REPAIR ... LOCATED AT APT 2N, ..." ---

# The first action verb ends the legal citation and starts the plain-English order.
ACTION_WORDS = (r"PROPERLY|REPAIR|REPLACE|ABATE|PROVIDE|REMOVE|PAINT|POST|TRACE|REFIT|CORRECT|FILE|MAINTAIN|"
                r"INSTALL|RESTORE|ARRANGE|DISCONTINUE|OBTAIN|SECURE|CLEAN|ELIMINATE|OWNER|SUBMIT|KEEP|"
                r"CERTIFY|LEGALIZE|EXTERMINATE|REPOINT|SUPPLY|FURNISH|AFFIX|PERFORM")


def split_violation_text(text: str) -> dict:
    """Split an HPD order into its code citation, what to fix, and where."""
    text = (text or "").strip()
    m = re.search(rf"\b({ACTION_WORDS})\b", text)
    citation, order = (text[:m.start()], text[m.start():]) if m else ("", text)
    order, _, where = order.partition(" LOCATED AT ")
    def sentence(t: str) -> str:  # "APT 2N, 2nd STORY" -> "Apt 2N, 2nd story": keep apartment codes upper case
        return re.sub(r"\bApt (\w+)", lambda m: "Apt " + m.group(1).upper(), t.strip().capitalize())

    return {
        "code_section": citation.strip(" :;,") or None,
        "what": sentence(order),
        "where": sentence(where) or None,
    }


# Correction deadlines from https://www.nyc.gov/site/hpd/services-and-information/clear-violations.page
VIOLATION_CLASSES = {
    "A": "non-hazardous (90 days to correct)",
    "B": "hazardous (30 days to correct)",
    "C": "immediately hazardous (24 hours to correct; 14-21 days for self-closing doors, lead, mold and pests)",
}
VIOLATION_CLASSES_SOURCE = "https://www.nyc.gov/site/hpd/services-and-information/clear-violations.page"


def fetch_open_violations(b: nyc.Building) -> list[dict]:
    return nyc.soda(nyc.VIOLATIONS, violationstatus="Open", **lot_filter(b), **{
        "$select": "violationid, class, inspectiondate, novdescription, apartment, story, rentimpairing, currentstatus",
        "$order": "inspectiondate", "$limit": 2000})


# --- Tool 1 ---


def look_up_building(address: str, state: dict) -> dict:
    b = current_building(address, state)
    result = {
        "address": b.label,
        "borough": b.borough,
        "neighborhood": b.neighborhood,
        "apartments": b.units,
        "year_built": b.year_built,
        "floors": b.floors,
        "building_type": b.bldgclass_label,
        "owner_of_record": b.owner_name or None,
        "hpd_registered": b.is_registered,
    }
    if b.is_registered:
        result["registration"] = {"status": b.registration_status, "period_ends": b.registration_ends}
        if b.registration_note:
            result["registration"]["note"] = b.registration_note
        result["registered_contacts"] = b.contacts_by_role()
        result["note"] = ("Contacts are as registered with HPD; the head officer can be an employee of the "
                          "management company, not the owner. Report roles as registered.")
    else:
        result["note"] = (
            "This lot has no HPD registration. Registration covers rental buildings with 3+ apartments, so "
            "houses, many co-ops/condos and non-residential lots won't appear. Maintenance, complaint and "
            "landlord records may be empty or incomplete here. Sunlight, the night walk and daily-life checks "
            "still work for any address.")
    return result


# --- Tool 2 ---


def check_maintenance_record(state: dict, address: str | None = None) -> dict:
    b = current_building(address, state)
    open_rows = fetch_open_violations(b)
    fixed = nyc.soda(nyc.VIOLATIONS, violationstatus="Close", **lot_filter(b), **{
        "$select": "inspectiondate, currentstatusdate, currentstatus",
        "$where": f"inspectiondate >= '{nyc.SINCE}' AND currentstatus != 'VIOLATION DISMISSED'",
        "$limit": 2000})

    by_class = Counter(v.get("class") for v in open_rows)
    ages = [d for d in (nyc.days_since(v.get("inspectiondate")) for v in open_rows) if d is not None]
    fix_days = [d for d in (nyc.days_between(v.get("inspectiondate"), v.get("currentstatusdate")) for v in fixed)
                if d is not None and d >= 0]

    oldest = []
    for v in open_rows[:3]:  # already ordered oldest first
        text = split_violation_text(v.get("novdescription"))
        oldest.append({"open_since": short_date(v.get("inspectiondate")), "class": v.get("class"),
                       "what": text["what"][:160], "apartment": v.get("apartment")})

    return {
        "address": b.label,
        "apartments": b.units,
        "open_violations": len(open_rows),
        "open_per_apartment": per_apartment(len(open_rows), b.units),
        "open_by_class": {cls: {"count": by_class.get(cls, 0), "meaning": meaning}
                          for cls, meaning in VIOLATION_CLASSES.items()},
        "class_deadlines_source": VIOLATION_CLASSES_SOURCE,
        "rent_impairing_open": sum(v.get("rentimpairing") == "Y" for v in open_rows),
        "days_open": {"median": nyc.median(ages), "longest": max(ages) if ages else None},
        "oldest_open": oldest,
        "fixed_since_2023": {"count": len(fixed), "median_days_to_close": nyc.median(fix_days)},
        "period": f"open = as of today; fixed = violations issued since {nyc.SINCE}",
        "note": CERTIFICATION_NOTE + " Long-open hazardous (B/C) violations are the clearest slow-repair signal.",
    }


# --- Tool 3 ---

# Our enum -> HPD major_category values (verified with a $group on ygpa-z7cr since 2023).
COMPLAINT_CATEGORIES = {
    "heat_hot_water": "HEAT/HOT WATER",
    "plumbing": "PLUMBING",
    "pests_unsanitary": "UNSANITARY CONDITION",
    "paint_plaster": "PAINT/PLASTER",
    "electric": "ELECTRIC",
    "appliance": "APPLIANCE",
    "door_window": "DOOR/WINDOW",
    "water_leak": "WATER LEAK",
    "general": "GENERAL",
    "flooring_stairs": "FLOORING/STAIRS",
    "safety": "SAFETY",
    "elevator": "ELEVATOR",
}
CATEGORY_BY_HPD = {v: k for k, v in COMPLAINT_CATEGORIES.items()}


def get_tenant_complaints(state: dict, address: str | None = None, category: str = "all") -> dict:
    b = current_building(address, state)
    if category != "all" and category not in COMPLAINT_CATEGORIES:
        raise ToolError(f"Unknown category '{category}'.", f"Use one of: all, {', '.join(COMPLAINT_CATEGORIES)}.")

    try:
        rows = nyc.soda(nyc.COMPLAINTS, bbl=b.bbl, **{
            "$select": "complaint_id, received_date, major_category, minor_category, complaint_status",
            "$where": f"received_date >= '{nyc.SINCE}'", "$order": "received_date DESC", "$limit": 5000})
    except DataSourceError:
        # Fall back to the snapshot's counts rather than nothing.
        snap = nyc.load_snapshot()
        if not snap:
            raise
        return {
            "address": b.label,
            "complaints_since_2023": snap["complaints_by_bbl"].get(b.bbl, 0),
            "heat_hot_water_complaints_since_2023": snap["heat_by_bbl"].get(b.bbl, 0),
            "source": f"snapshot as of {snap['as_of'].get('complaints_by_bbl')} (live data didn't respond)",
            "note": "Only totals are available from the snapshot; ask again later for details.",
        }

    # One complaint can list several problems (one row each): count complaints, not rows.
    complaints: dict[str, dict] = {}
    categories_by_complaint: dict[str, set] = {}
    for r in rows:
        cid = r.get("complaint_id")
        complaints.setdefault(cid, r)
        categories_by_complaint.setdefault(cid, set()).add(r.get("major_category"))

    by_category = Counter(cat for cats in categories_by_complaint.values() for cat in cats)
    friendly = {CATEGORY_BY_HPD.get(k, k.lower()): n for k, n in by_category.most_common()}

    focus = COMPLAINT_CATEGORIES.get(category) or (by_category.most_common(1)[0][0] if by_category else None)
    chosen = [complaints[cid] for cid, cats in categories_by_complaint.items()
              if category == "all" or COMPLAINT_CATEGORIES[category] in cats]
    focus_rows = [complaints[cid] for cid, cats in categories_by_complaint.items() if focus in cats]
    by_month = Counter(r["received_date"][:7] for r in focus_rows if r.get("received_date"))
    by_month = {month_label(m + "-01"): n for m, n in sorted(by_month.items())}

    recent = [{"date": short_date(r.get("received_date")), "category": CATEGORY_BY_HPD.get(r.get("major_category"), r.get("major_category")),
               "detail": (r.get("minor_category") or "").lower(), "status": (r.get("complaint_status") or "").lower()}
              for r in sorted(chosen, key=lambda r: r.get("received_date") or "", reverse=True)[:5]]

    return {
        "address": b.label,
        "period": f"since {nyc.SINCE}",
        "complaints": len(complaints),
        "complaints_per_apartment": per_apartment(len(complaints), b.units),
        "by_category": friendly,
        "category_requested": category,
        "matching_complaints": len(chosen),
        "by_month": {"category": CATEGORY_BY_HPD.get(focus, focus), "counts": by_month} if focus else None,
        "most_recent": recent,
        "note": ("Complaints are tenant calls to 311/HPD, counted once even when they list several problems. "
                 "'Closed' means the city closed the case, not necessarily that it was fixed."),
    }


# --- Tool 4 ---

BEDBUG_RULE = ("NYC requires owners to give tenants signing a new (vacancy) lease the building's bedbug "
               "history for the previous year. Ask for it. Source: "
               "https://www.nyc.gov/site/hpd/services-and-information/bedbugs.page")


def check_pests(state: dict, address: str | None = None) -> dict:
    b = current_building(address, state)
    rats = nyc.soda(nyc.RODENTS, **lot_filter(b, boro="boro_code"), **{
        "$select": "inspection_date, result",
        "$where": f"inspection_date >= '{nyc.SINCE}' AND {nyc.RAT_INSPECTIONS}",
        "$order": "inspection_date DESC", "$limit": 500})
    failed = [r for r in rats if (r.get("result") or "").startswith("Failed for Rat Activity")]
    bugs = nyc.soda_bbl_in(nyc.BEDBUGS, [b.bbl], **{
        "$select": "filing_date, of_dwelling_units, infested_dwelling_unit_count, eradicated_unit_count, re_infested_dwelling_unit",
        "$order": "filing_date DESC", "$limit": 5})

    filings = [{"filed": short_date(r.get("filing_date")),
                "infested_units": int(r.get("infested_dwelling_unit_count") or 0),
                "eradicated_units": int(r.get("eradicated_unit_count") or 0),
                "reinfested_units": int(r.get("re_infested_dwelling_unit") or 0)} for r in bugs]
    result = {
        "address": b.label,
        "rodent_inspections_since_2023": {
            "inspections": len(rats),
            "failed_for_rats": len(failed),
            "passed": sum(r.get("result") == "Passed" for r in rats),
            "last_failed": short_date(failed[0]["inspection_date"]) if failed else None,
        },
        "bedbug_filings": filings or "No bedbug filings on record (owners file annually; small buildings may not).",
        "note": ("Rat inspections are by the Health Department and cover the whole lot, often after a complaint. "
                 "Bedbug filings are self-reported by the owner each year."),
    }
    if any(f["infested_units"] for f in filings):
        result["bedbug_disclosure"] = BEDBUG_RULE
    return result


# --- Tool 5 ---

CASE_TYPES = {
    "Tenant Action": "tenants sued the owner to force repairs (HP action)",
    "Tenant Action/Harrassment": "tenants sued the owner over repairs and harassment",
    "Heat and Hot Water": "HPD sued the owner over heat or hot water",
    "Comprehensive": "HPD sued the owner over many violations at once",
    "Access Warrant - Non-Lead": "HPD asked the court for access to inspect or repair",
    "Access Warrant - lead": "HPD asked the court for access to fix lead paint",
    "False Certification Non-Lead": "HPD sued over repairs certified as done that were not",
    "Lead False Certification": "HPD sued over lead repairs certified as done that were not",
    "CONH": "Certification of No Harassment case (needed for some building permits)",
    "Failure to Register Only": "HPD sued because the owner didn't register the building",
    "7A": "court-appointed administrator case (owner removed from management)",
}


def check_evictions_and_court(state: dict, address: str | None = None) -> dict:
    b = current_building(address, state)
    evictions = nyc.soda_bbl_in(nyc.EVICTIONS, [b.bbl], **{
        "$select": "executed_date, residential_commercial_ind",
        "$where": f"executed_date >= '{nyc.SINCE}' AND residential_commercial_ind = 'Residential'",
        "$order": "executed_date DESC", "$limit": 500})
    cases = nyc.soda_bbl_in(nyc.LITIGATION, [b.bbl], **{
        "$select": "casetype, caseopendate, casestatus, casejudgement",
        "$order": "caseopendate DESC", "$limit": 200})

    return {
        "address": b.label,
        "residential_evictions_since_2023": {"count": len(evictions),
                                             "dates": [short_date(e.get("executed_date")) for e in evictions[:10]]},
        "hpd_court_cases": {
            "total_on_record": len(cases),
            "since_2023": sum((c.get("caseopendate") or "") >= nyc.SINCE for c in cases),
            "recent": [{"type": c.get("casetype"), "meaning": CASE_TYPES.get(c.get("casetype"), "other HPD case"),
                        "opened": short_date(c.get("caseopendate")), "status": (c.get("casestatus") or "").lower(),
                        "judgement": (c.get("casejudgement") or "").lower() or None} for c in cases[:8]],
        },
        "note": ("Evictions are those carried out by a city marshal, not cases filed. HPD cases are housing-court "
                 "cases about conditions, not tenants' rent cases."),
    }


# --- Tool 6 ---


def owner_person(b: nyc.Building) -> dict | None:
    """The human behind the LLC: head officer or individual owner on the latest registration."""
    for wanted in ("HeadOfficer", "IndividualOwner", "JointOwner", "Officer"):
        for c in b.contacts:
            if c.get("type") == wanted and c.get("lastname") and c.get("firstname"):
                return c
    return None


def get_landlord_portfolio(state: dict, address: str | None = None) -> dict:
    b = current_building(address, state)
    if not b.is_registered:
        raise ToolError("This building has no HPD registration, so there is no registered owner to search for.",
                        "Tell the user portfolio lookups only work for registered rentals (3+ apartments).")
    person = owner_person(b)
    if not person:
        raise ToolError("The registration names only companies, no person, so other buildings can't be matched.",
                        "Report the registered owner company from look_up_building instead.")

    first, last = person["firstname"].upper().replace("'", "''"), person["lastname"].upper().replace("'", "''")
    same_name = nyc.soda(nyc.CONTACTS, **{
        "$select": "registrationid, type, businesshousenumber, businessstreetname, businesszip",
        "$where": (f"upper(firstname)='{first}' AND upper(lastname)='{last}' "
                   "AND type in ('HeadOfficer', 'IndividualOwner', 'JointOwner', 'Officer')"),
        "$limit": 3000})
    home_address = (person.get("businesshousenumber"), person.get("businesszip"))
    confirmed = {c["registrationid"] for c in same_name
                 if (c.get("businesshousenumber"), c.get("businesszip")) == home_address and home_address[1]}
    reg_ids = sorted({c["registrationid"] for c in same_name})[:300]

    # Keep registrations that are current (or a few months into renewal): old ones
    # are buildings the person no longer runs.
    regs = []
    for part in nyc.chunks(reg_ids):
        regs += nyc.soda(nyc.REGISTRATIONS, **{
            "$select": "registrationid, boroid, block, lot, housenumber, streetname, registrationenddate",
            "$where": f"registrationid in ({','.join(repr(r) for r in part)})", "$limit": 1000})
    cutoff = (date.today().replace(year=date.today().year - 1)).isoformat()
    buildings = {}
    for r in regs:
        if (r.get("registrationenddate") or "") < cutoff or not r.get("block"):
            continue
        bbl = nyc.make_bbl(r["boroid"], r["block"], r["lot"])
        buildings.setdefault(bbl, {"address": f"{r.get('housenumber', '')} {r.get('streetname', '')}".strip().title(),
                                   "borough": nyc.BOROUGH_BY_ID.get(r["boroid"], ""),
                                   "confirmed_by_address": r["registrationid"] in confirmed})
    buildings.setdefault(b.bbl, {"address": b.label, "borough": b.borough, "confirmed_by_address": True})

    bbls = sorted(buildings)

    def units_and_location(part: list[str]) -> list[dict]:
        return nyc.soda(nyc.PLUTO, **{"$select": "bbl, unitsres, latitude, longitude",
                                      "$where": f"bbl in ({','.join(part)})", "$limit": 1000})

    def counts_since_2023(dataset: str, date_field: str, extra: str, part: list[str]) -> list[dict]:
        return nyc.soda_bbl_in(dataset, part, **{"$select": "bbl, count(*) as n",
                                                 "$where": f"{date_field} >= '{nyc.SINCE}'{extra}",
                                                 "$group": "bbl", "$limit": 1000})

    # Three independent lookups per chunk of buildings; run them at once (cold
    # Socrata queries take seconds each, and this tool's target is under 10s).
    with ThreadPoolExecutor(max_workers=6) as pool:
        lot_jobs = [pool.submit(units_and_location, part) for part in nyc.chunks(bbls)]
        eviction_jobs = [pool.submit(counts_since_2023, nyc.EVICTIONS, "executed_date",
                                     " AND residential_commercial_ind = 'Residential'", part) for part in nyc.chunks(bbls)]
        case_jobs = [pool.submit(counts_since_2023, nyc.LITIGATION, "caseopendate", "", part) for part in nyc.chunks(bbls)]
        lots = [row for job in lot_jobs for row in job.result()]
        evictions, cases = Counter(), Counter()
        for jobs, counter in ((eviction_jobs, evictions), (case_jobs, cases)):
            for job in jobs:
                for r in job.result():
                    if r.get("bbl"):
                        counter[nyc.normalize_bbl(r["bbl"])] += int(r["n"])

    for row in lots:
        info = buildings.get(nyc.normalize_bbl(row["bbl"]))
        if info is not None:
            info["units"] = int(float(row.get("unitsres") or 0))
            info["lat"], info["lon"] = float(row.get("latitude") or 0) or None, float(row.get("longitude") or 0) or None

    # Open violations from the snapshot: one dict lookup per building instead of a
    # live query per lot. This building's count is live (it's what the user sees elsewhere).
    snap = nyc.load_snapshot()
    open_by_bbl = snap.get("open_violations_by_bbl", {})

    table = []
    for bbl, info in buildings.items():
        units = info.get("units", 0)
        live_open = len(fetch_open_violations(b)) if bbl == b.bbl else None
        n_open = live_open if live_open is not None else sum(open_by_bbl.get(bbl, [0, 0, 0]))
        table.append({"address": info["address"], "borough": info["borough"], "apartments": units,
                      "open_violations": n_open, "per_apartment": per_apartment(n_open, units),
                      "evictions_since_2023": evictions[bbl], "hpd_cases_since_2023": cases[bbl],
                      "lat": info.get("lat"), "lon": info.get("lon"),
                      "this_building": bbl == b.bbl, "confirmed_by_address": info["confirmed_by_address"]})

    total_units = sum(t["apartments"] for t in table)
    total_open = sum(t["open_violations"] for t in table)
    rankable = sorted([t for t in table if t["apartments"] >= 6], key=lambda t: -(t["per_apartment"] or 0))
    this = next(t for t in table if t["this_building"])
    rank = next((i + 1 for i, t in enumerate(rankable) if t["this_building"]), None)

    return {
        "owner": {"name": nyc.contact_name(person), "registered_role": person.get("type"),
                  "on_this_building_as": "registered " + person.get("type", "contact")},
        "buildings": len(table),
        "apartments": total_units,
        "open_violations": total_open,
        "portfolio_per_apartment": per_apartment(total_open, total_units),
        "evictions_since_2023": sum(evictions.values()),
        "hpd_cases_since_2023": sum(cases.values()),
        "this_building": {"per_apartment": this["per_apartment"], "open_violations": this["open_violations"],
                          "rank_worst_first": f"{rank} of {len(rankable)} buildings with 6+ apartments" if rank else None},
        "worst_buildings": [{k: t[k] for k in ("address", "borough", "apartments", "open_violations", "per_apartment",
                                               "evictions_since_2023", "hpd_cases_since_2023")} for t in rankable[:8]],
        "all_buildings": [[t["address"], t["apartments"], t["open_violations"], t["per_apartment"], t["lat"], t["lon"]]
                          for t in table],
        "all_buildings_columns": ["address", "apartments", "open_violations", "per_apartment", "lat", "lon"],
        "match_method": (f"same registered person name (case-insensitive); {len(confirmed)} of {len(reg_ids)} matching "
                         "registrations also share the business address"),
        "source": (f"open violations from the snapshot as of {snap.get('as_of', {}).get('open_violations_by_bbl')} "
                   "(this building: live); evictions and HPD cases live"),
        "note": ("Matched by the person's name on HPD registrations, so a different person with the same name "
                 "could be included, and related LLCs run by other officers are missed: the portfolio may be "
                 "incomplete. Records only; they say nothing about intent."),
    }


# --- Tool 7 ---


def describe_rank(mine: float, rates: list[float]) -> str:
    """Plain-English position of this building among its neighbors, with ties handled."""
    n = len(rates)
    worse = sum(r > mine for r in rates)
    tied = sum(r == mine for r in rates)
    area_median = statistics.median(rates)
    if mine == 0:
        return f"tied for cleanest: {round(100 * tied / n)}% of nearby rentals also have no open violations"
    if mine == area_median:
        return "about average for the area"
    return (f"fewer open violations per apartment than {round(100 * worse / n)}% of nearby rentals"
            + (f" (tied with {round(100 * tied / n)}%)" if tied > 1 else ""))


def get_neighborhood_context(state: dict, address: str | None = None, radius_miles: float = 0.25) -> dict:
    b = current_building(address, state)
    radius = min(max(float(radius_miles), 0.1), 0.5)
    snap = nyc.load_snapshot()
    if not snap:
        raise ToolError("The neighborhood comparison data file is missing.",
                        "Tell the user the area comparison is unavailable right now.")

    # Residential lots in a bounding box (fast on PLUTO), trimmed to the circle.
    dlat = radius / 69.0
    dlon = radius / (69.0 * 0.7570)  # cos(40.7 deg): NYC's longitude scale
    lots = nyc.soda(nyc.PLUTO, **{
        "$select": "bbl, address, unitsres, latitude, longitude",
        "$where": (f"latitude between {b.lat - dlat} and {b.lat + dlat} AND "
                   f"longitude between {b.lon - dlon} and {b.lon + dlon} AND unitsres >= 3"),
        "$limit": 50000})
    nearby = {}
    for r in lots:
        if not r.get("latitude"):
            continue
        lat, lon = float(r["latitude"]), float(r["longitude"])
        if nyc.miles_between(b.lat, b.lon, lat, lon) <= radius:
            nearby[nyc.normalize_bbl(r["bbl"])] = {"address": (r.get("address") or "").title() or None,
                                                   "units": int(float(r.get("unitsres") or 0)), "lat": lat, "lon": lon}

    # Keep HPD-registered rentals: the same population this building is part of.
    boro = b.bbl[0]
    blocks = sorted({str(int(x[1:6])) for x in nearby if x[0] == boro})
    registered = set()
    for part in nyc.chunks(blocks):
        rows = nyc.soda(nyc.REGISTRATIONS, **{"$select": "block, lot",
                                              "$where": f"boroid='{boro}' AND block in ({','.join(repr(x) for x in part)})",
                                              "$limit": 50000})
        registered |= {nyc.make_bbl(boro, r["block"], r["lot"]) for r in rows}
    rentals = {k: v for k, v in nearby.items() if k in registered and v["units"]}
    rentals.setdefault(b.bbl, {"address": b.label, "units": b.units, "lat": b.lat, "lon": b.lon})
    if not b.units or not b.is_registered:
        raise ToolError(f"{b.label} is not an HPD-registered rental, so it can't be compared with nearby rentals.",
                        "Tell the user the area comparison covers rental buildings with 3+ apartments; offer the "
                        "sunlight, night-walk or daily-life checks instead.")

    open_v, heat, rats = snap["open_violations_by_bbl"], snap["heat_by_bbl"], snap["rodents_by_lot"]
    rate = {k: sum(open_v.get(k, [0, 0, 0])) / v["units"] for k, v in rentals.items()}
    others = [r for k, r in rate.items() if k != b.bbl] or [0.0]
    total_units = sum(v["units"] for v in rentals.values())
    area_heat = sum(heat.get(k, 0) for k in rentals)
    rat_lots = [k for k in rentals if rats.get(k, [0, 0])[1] > 0]

    worst = sorted((k for k in rentals if rentals[k]["units"] >= 6 and k != b.bbl), key=lambda k: -rate[k])[:5]
    return {
        "address": b.label,
        "radius_miles": radius,
        "compared": {"rental_buildings": len(rentals), "apartments": total_units},
        "open_violations_per_apartment": {
            "this_building": round(rate[b.bbl], 2),
            "area_median": round(statistics.median(others), 2),
            "this_building_vs_area": describe_rank(rate[b.bbl], others),
        },
        "heat_complaints_per_100_apartments_since_2023": {
            "this_building": round(100 * heat.get(b.bbl, 0) / b.units, 1),
            "area": round(100 * area_heat / total_units, 1) if total_units else None,
        },
        "rats": {
            "share_of_nearby_rental_lots_with_a_failed_rat_inspection_since_2023": f"{round(100 * len(rat_lots) / len(rentals))}%",
            "this_building_failed_one": b.bbl in rat_lots,
        },
        "worst_nearby": [{"address": rentals[k]["address"], "apartments": rentals[k]["units"],
                          "open_violations": sum(open_v.get(k, [0, 0, 0])), "per_apartment": round(rate[k], 2)}
                         for k in worst],
        "points": [[round(v["lat"], 5), round(v["lon"], 5), round(rate[k], 2)] for k, v in rentals.items()],
        "points_columns": ["lat", "lon", "open_violations_per_apartment"],
        "as_of": {"open_violations": snap["as_of"].get("open_violations_by_bbl"),
                  "heat_complaints": snap["as_of"].get("heat_by_bbl"), "rats": snap["as_of"].get("rodents_by_lot")},
        "note": ("Compares HPD-registered rentals with 3+ apartments within the radius, using a citywide snapshot "
                 "(dates in as_of), so this building's figure here can differ slightly from today's live count. "
                 + CERTIFICATION_NOTE),
    }


# --- Tool 8 ---

# Words in HPD violation orders that belong to each complaint category.
ISSUE_KEYWORDS = {
    "heat_hot_water": ["HEAT", "HOT WATER", "BOILER"],
    "plumbing": ["PLUMBING", "WATER SUPPLY", "DRAIN", "TOILET", "WATER CLOSET", "SINK", "BASIN", "BATHTUB", "FAUCET",
                 "PIPE"],
    "pests_unsanitary": ["MICE", "RATS", "ROACH", "PEST", "VERMIN", "BED BUG", "BEDBUG", "MOLD", "REFUSE", "RUBBISH",
                         "GARBAGE", "INFESTATION"],
    "paint_plaster": ["PLASTER", "PAINT"],
    "electric": ["ELECTRIC", "OUTLET", "WIRING", "LIGHT FIXTURE", "SWITCH"],
    "appliance": ["REFRIGERATOR", "RANGE", "STOVE", "OVEN", "APPLIANCE", "GAS"],
    "door_window": ["DOOR", "WINDOW", "LOCK"],
    "water_leak": ["LEAK"],
    "flooring_stairs": ["FLOOR", "STAIR", "STEP", "TREAD"],
    "safety": ["SMOKE DETECT", "CARBON MONOXIDE", "FIRE", "SPRINKLER", "EGRESS", "SELF-CLOSING"],
    "elevator": ["ELEVATOR"],
    "general": [],
}

ESCALATION_STEPS = [
    "Keep a copy of this letter and photos of each problem, with dates.",
    "If the landlord doesn't respond, file a complaint with 311 (call 311 or use 311 Online). HPD may inspect and "
    "issue a violation, which creates an official record.",
    "If a violation stays open, tenants can start a case against the owner in Housing Court (an HP action).",
    "Free help: HPD's tenant resources (https://www.nyc.gov/site/hpd/services-and-information/tenants-rights.page) "
    "and Met Council on Housing (https://www.metcouncilonhousing.org/).",
]
ESCALATION_SOURCE = "https://www.nyc.gov/site/hpd/services-and-information/report-a-maintenance-issue.page"


def matching_violations(open_rows: list[dict], issues: list[str], apartment: str | None) -> list[dict]:
    apt = (apartment or "").upper().replace("APT", "").strip()
    out = []
    for v in open_rows:
        text = (v.get("novdescription") or "").upper()
        v_apt = (v.get("apartment") or "").upper()
        if apt and v_apt and v_apt != apt:
            continue  # another tenant's apartment; public-area violations (no apartment) still count
        hits = [i for i in issues if any(k in text for k in ISSUE_KEYWORDS.get(i, []))]
        if hits:
            out.append({**v, "issues": hits})
    return out


def draft_repair_request(state: dict, issues: list[str], details: str | None = None, apartment: str | None = None,
                         tenant_name: str | None = None) -> dict:
    b = current_building(None, state)
    if not issues:
        raise ToolError("No issues given.", f"Pass one or more of: {', '.join(ISSUE_KEYWORDS)}.")
    unknown = [i for i in issues if i not in ISSUE_KEYWORDS]
    if unknown:
        raise ToolError(f"Unknown issue type(s): {unknown}.", f"Use only: {', '.join(ISSUE_KEYWORDS)}.")

    cited = matching_violations(fetch_open_violations(b), issues, apartment)
    contacts = b.contacts_by_role()
    owner = (contacts.get("owner_company") or contacts.get("owner_person") or [{"name": b.owner_name or "Building Owner"}])[0]
    agent = (contacts.get("managing_agent") or [None])[0]
    unit = f", Apt {apartment.upper().replace('APT', '').strip()}" if apartment else ""
    today = date.today().strftime("%B %-d, %Y")

    lines = [today, "", f"To: {owner['name']}" + (f", {owner['business_address']}" if owner.get("business_address") else "")]
    if agent:
        lines.append(f"Cc: {agent['name']} (managing agent)" + (f", {agent['business_address']}" if agent.get("business_address") else ""))
    lines += ["", f"Re: Repairs needed at {b.label}{unit}, {b.borough}", "", "Dear Owner/Managing Agent,", ""]
    if details:
        lines += [f"I am writing about the following conditions in my home: {details.strip().rstrip('.')}.", ""]
    if cited:
        lines.append("HPD has already issued violations for these conditions, and they remain open in city records:")
        for v in cited:
            t = split_violation_text(v.get("novdescription"))
            lines.append(f"  - Violation {v.get('violationid')} (class {v.get('class')}, inspected "
                         f"{short_date(v.get('inspectiondate'))}): {t['what'][:180]}"
                         + (f" [{t['code_section']}]" if t["code_section"] else ""))
        lines.append("")
    else:
        lines += ["I could not find an open HPD violation for these conditions, so I am reporting them to you directly.", ""]
    lines += [
        "Please arrange the repairs and let me know in writing, within 14 days of this letter, when the work "
        "will be done. I will provide access at reasonable times with advance notice.",
        "",
        "If the conditions are not addressed, I will file a complaint with 311 so that HPD can inspect.",
        "",
        "Sincerely,",
        tenant_name or "[Your name]",
        f"{b.label}{unit}",
    ]

    return {
        "letter_text": "\n".join(lines),
        "addressed_to": {"owner": owner["name"], "managing_agent": agent["name"] if agent else None,
                         "source": "HPD registration" if contacts else "PLUTO owner of record"},
        "cited_violations": [{"violation_id": v.get("violationid"), "class": v.get("class"),
                              "inspected": short_date(v.get("inspectiondate")), "apartment": v.get("apartment"),
                              "issues": v["issues"], **split_violation_text(v.get("novdescription"))} for v in cited],
        "escalation_steps": ESCALATION_STEPS,
        "escalation_source": ESCALATION_SOURCE,
        "note": ("No matching open violations: the letter uses the tenant's description. Filing a 311 complaint "
                 "creates an official record." if not cited else
                 "Cited violations are open in HPD records for this building" + (" and apartment." if apartment else ".")
                 ) + " This is a template, not legal advice.",
    }


# --- What the model sees ---

ADDRESS_ARG = {
    "type": "string",
    "description": ("NYC street address with borough, e.g. '184 Claremont Ave, Manhattan'. Omit to use the "
                    "building already being discussed."),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "look_up_building",
            "description": (
                "Find an NYC building by street address and make it the building being discussed. Returns "
                "apartments, year built, floors, building type, HPD registration status and the registered "
                "owner, head officer and managing agent. Call this FIRST whenever the user mentions a new "
                "address. Does not cover repairs, complaints or pests (use the other tools)."),
            "parameters": {
                "type": "object",
                "properties": {"address": {
                    "type": "string",
                    "description": ("House number, street and borough (or zip), e.g. '184 Claremont Ave, Manhattan' "
                                    "or '45-17 21st St, Queens 11101'. Not an intersection or neighborhood name.")}},
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_maintenance_record",
            "description": (
                "Does the landlord fix things? Open HPD housing-code violations now (total, per apartment, by "
                "hazard class, how long they've been open, the oldest ones) and how fast violations issued since "
                "2023 were closed. Use for 'is it well maintained', 'does the landlord make repairs'."),
            "parameters": {"type": "object", "properties": {"address": ADDRESS_ARG}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tenant_complaints",
            "description": (
                "What do tenants complain about? HPD complaints since 2023: count, per apartment, by category, "
                "month-by-month for one category, and the 5 most recent. Use for heat, leaks or 'problems "
                "tenants report'. Not repairs ordered by the city (use check_maintenance_record)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": ADDRESS_ARG,
                    "category": {"type": "string", "enum": ["all", *COMPLAINT_CATEGORIES],
                                 "description": ("Which complaints to list and chart by month. 'all' (default) "
                                                 "charts the most common category.")},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_pests",
            "description": (
                "Rats and bedbugs: Health Department rat inspections since 2023 (passed, failed, last failure) "
                "and the owner's last 5 annual bedbug filings (infested, eradicated, re-infested units)."),
            "parameters": {"type": "object", "properties": {"address": ADDRESS_ARG}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_evictions_and_court",
            "description": (
                "Marshal-executed residential evictions since 2023 and HPD housing-court cases against the owner "
                "(repairs, heat, harassment), with case types in plain English."),
            "parameters": {"type": "object", "properties": {"address": ADDRESS_ARG}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_landlord_portfolio",
            "description": (
                "Who is the person behind the owner LLC, and how do they run their OTHER buildings? Finds every "
                "current HPD registration naming the same head officer/owner, then compares open violations per "
                "apartment, evictions and court cases across them, ranks this building, and lists the worst. Use "
                "for 'who owns this', 'what's the landlord like', 'other buildings'. Takes ~5-10s."),
            "parameters": {"type": "object", "properties": {"address": ADDRESS_ARG}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighborhood_context",
            "description": (
                "Is this building better or worse than its neighbors? Compares open violations per apartment, "
                "heat complaints per 100 apartments and failed rat inspections with every registered rental "
                "within a radius, gives a percentile and the worst nearby buildings. Use for 'is that normal "
                "for the area'. Uses a citywide snapshot (dates in as_of)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": ADDRESS_ARG,
                    "radius_miles": {"type": "number", "minimum": 0.1, "maximum": 0.5,
                                     "description": "Search radius in miles, 0.1-0.5. Default 0.25 (about 5 blocks)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_repair_request",
            "description": (
                "Write a polite, firm repair-request letter from a tenant to their landlord for the building "
                "being discussed, citing any matching OPEN HPD violations (ID, date, code section), plus the "
                "official escalation steps. Call look_up_building first. Use when a current tenant describes a "
                "problem in their home. Not legal advice."),
            "parameters": {
                "type": "object",
                "properties": {
                    "issues": {"type": "array", "minItems": 1,
                               "items": {"type": "string", "enum": list(ISSUE_KEYWORDS)},
                               "description": "The kinds of problem, e.g. ['water_leak', 'paint_plaster'] for a leaking ceiling."},
                    "details": {"type": "string",
                                "description": "The tenant's own description, e.g. 'the bathroom ceiling has leaked since June'."},
                    "apartment": {"type": "string", "description": "The tenant's apartment, e.g. '2N'. Omit if unknown."},
                    "tenant_name": {"type": "string", "description": "Name to sign with. Omit to leave a placeholder."},
                },
                "required": ["issues"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "look_up_building": look_up_building,
    "check_maintenance_record": check_maintenance_record,
    "get_tenant_complaints": get_tenant_complaints,
    "check_pests": check_pests,
    "check_evictions_and_court": check_evictions_and_court,
    "get_landlord_portfolio": get_landlord_portfolio,
    "get_neighborhood_context": get_neighborhood_context,
    "draft_repair_request": draft_repair_request,
}


def run_tool(name: str, args: dict, state: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'.", "next_step": f"Use one of: {', '.join(TOOL_MAP)}."})
    try:
        return json.dumps(TOOL_MAP[name](**args, state=state))
    except ToolError as e:
        return json.dumps({"error": e.error, "next_step": e.next_step})
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}",
                           "next_step": "Check the argument names and types in the tool description and retry."})
    except DataSourceError as e:
        return json.dumps({"error": f"NYC Open Data didn't respond in time ({e.message})",
                           "next_step": "Tell the user and offer to retry or skip this check."})
    except Exception as e:  # last resort: the model must always get JSON, never a stack trace
        return json.dumps({"error": f"{name} failed unexpectedly: {type(e).__name__}.",
                           "next_step": "Tell the user this check failed and continue with the others."})


if __name__ == "__main__":
    import sys
    import time

    address = " ".join(sys.argv[1:]) or "184 Claremont Ave, Manhattan"
    state: dict = {}
    calls = [
        ("look_up_building", {"address": address}),
        ("check_maintenance_record", {}),
        ("get_tenant_complaints", {}),
        ("get_tenant_complaints", {"category": "heat_hot_water"}),
        ("check_pests", {}),
        ("check_evictions_and_court", {}),
        ("get_landlord_portfolio", {}),
        ("get_neighborhood_context", {}),
        ("draft_repair_request", {"issues": ["water_leak", "paint_plaster"], "apartment": "2N",
                                  "details": "the bathroom ceiling has been leaking for months"}),
    ]
    timings = []
    for name, args in calls:
        started = time.time()
        result = run_tool(name, args, state)
        elapsed = time.time() - started
        timings.append((name, args, elapsed, len(result)))
        print(f"\n===== {name}({args})  {elapsed:.1f}s, {len(result):,} chars")
        parsed = json.loads(result)
        for key in ("all_buildings", "points"):
            if key in parsed:
                parsed[key] = f"[{len(parsed[key])} rows]"
        if "letter_text" in parsed:
            print(parsed.pop("letter_text"))
        print(json.dumps(parsed, indent=1)[:3500])
    print("\n===== Timing")
    for name, args, elapsed, size in timings:
        print(f"  {name:28s} {elapsed:5.1f}s  {size:>7,} chars  {args if args else ''}")
