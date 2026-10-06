"""Apartment 411's tools, and the JSON that describes them to the model.

Every tool returns a JSON string: interpreted facts with context (per apartment,
vs the area, over what period), plus a `note` where a caveat applies. Failures
return {"error": ..., "next_step": ...} so the model can tell the user what to do.

Tools that look at a building take an optional `address`. When it is omitted they
use the building already being discussed, kept in the session's `state` by the
harness. The model never sees or passes BBLs or registration IDs.

Run `uv run python -m tools "155 East 92nd Street, Manhattan"` to try every tool.
"""

import json
import math
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import lease
import numpy as np

import nyc
import sun
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
                                        "Ask the user for the house number, street and borough (e.g. '155 East 92nd Street, "
                                        "Manhattan'). Intersections and neighborhood names can't be looked up."))
        state.setdefault("buildings", {})[building.bbl] = building
        state["current_bbl"] = building.bbl
        return building
    bbl = state.get("current_bbl")
    if not bbl:
        raise NO_BUILDING
    return state["buildings"][bbl]


def not_registered(b: nyc.Building) -> str:
    """Why HPD records don't cover this building, in a sentence the model can pass on."""
    if "HOUSING AUTHORITY" in (b.owner_name or "").upper():
        return (f"{b.label} is NYCHA public housing, which isn't HPD-registered: NYCHA handles its own repairs and "
                "complaints, so HPD violation, complaint and court records don't cover it.")
    return (f"{b.label} isn't HPD-registered (registration covers rentals with 3+ apartments; houses and many "
            "co-ops/condos are exempt), so HPD has no records of this kind for it.")


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
        "owner_on_tax_records": b.owner_name or None,  # PLUTO: who the city taxes, can lag a sale
        "hpd_registered": b.is_registered,
        "lat": round(b.lat, 6),
        "lon": round(b.lon, 6),
    }
    if b.is_registered:
        result["registration"] = {"status": b.registration_status, "period_ends": b.registration_ends}
        if b.registration_note:
            result["registration"]["note"] = b.registration_note
        result["registered_contacts"] = b.contacts_by_role()
        result["note"] = ("Contacts are as registered with HPD; the head officer can be an employee of the "
                          "management company, not the owner. Report roles as registered.")
        registered_owners = [c["name"] for role in ("owner_company", "owner_person")
                             for c in result["registered_contacts"].get(role, [])]
        if b.owner_name and registered_owners and not same_party(b.owner_name, registered_owners):
            result["owner_names_differ"] = (
                f"The tax records name '{b.owner_name}' as owner, but the HPD registration names "
                f"'{registered_owners[0]}'. That can be an affiliated LLC or records that lag a sale; the HPD "
                "registration is who answers for repairs.")
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
    # "0 open violations" for a building HPD doesn't oversee would read as spotless. An unregistered
    # building that does have HPD violations (an owner who never registered) still gets them shown.
    if not b.is_registered and not open_rows and not fixed:
        raise ToolError(not_registered(b) + " There are no HPD violation records for it.",
                        "Tell the user HPD maintenance records don't cover this building; offer the sunlight, "
                        "night-walk and rat-inspection checks, which still apply.")

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

    if not b.is_registered and not rows:
        raise ToolError(not_registered(b) + " There are no HPD tenant complaints on record for it.",
                        "Tell the user HPD complaint records don't cover this building; offer the checks that do apply.")

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
        "bedbug_filings": filings or ("Not HPD-registered: bedbug filings are only required of registered rentals, so "
                                      "there is no bedbug record." if not b.is_registered else
                                      "No bedbug filings on record (owners file annually; small buildings may not)."),
        "note": ("Rat inspections are by the Health Department and cover the whole lot, often after a complaint. "
                 "Bedbug filings are self-reported by the owner each year."),
    }
    if any(f["infested_units"] for f in filings):
        result["bedbug_disclosure"] = BEDBUG_RULE
    if not b.is_registered:
        result["hpd_registered"] = False
        result["note"] += " " + not_registered(b) + " Rat inspections are Health Department records and still apply."
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

    result = {
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
    if not b.is_registered:
        result["note"] += " " + not_registered(b) + " Marshal evictions are court records and still apply."
    return result



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
        "address": b.label,
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
    """Plain-English position of this building among its neighbors, ties handled, always said
    from the side it's on: "more than X%" when worse than the median, "fewer than X%" when better.
    ("Fewer than 34%" for a building worse than two-thirds of its block read like a compliment.)"""
    n = len(rates)
    better = sum(r < mine for r in rates)   # nearby rentals with fewer violations per apartment
    worse = sum(r > mine for r in rates)
    tied = sum(r == mine for r in rates)
    area_median = statistics.median(rates)
    median_text = f"area median {area_median:.2f}"
    tie_text = f"; tied with {round(100 * tied / n)}%" if tied > 1 else ""
    if mine == 0:
        return f"tied for cleanest: {round(100 * tied / n)}% of nearby rentals also have no open violations ({median_text})"
    if mine == area_median:
        return f"about average for the area ({median_text})"
    if mine > area_median:
        return (f"more open violations per apartment than {round(100 * better / n)}% of nearby rentals "
                f"({median_text}{tie_text})")
    return (f"fewer open violations per apartment than {round(100 * worse / n)}% of nearby rentals "
            f"({median_text}{tie_text})")


def heat_comparison(count: int, units: int, area_count: int, area_units: int) -> dict:
    """Heat complaints as a rate on both sides, with a sentence to quote: comparing this building's
    raw count with the area's rate per 100 apartments (which the model once did) is meaningless."""
    here = round(100 * count / units, 1) if units else None
    area = round(100 * area_count / area_units, 1) if area_units else None
    if here and area:
        ratio = f" ({here / area:.1f}x the area rate)" if here >= area else f" (the area's rate is {area / here:.1f}x this)"
    else:
        ratio = ""
    plural = "complaint" if count == 1 else "complaints"
    return {"this_building": here, "area": area, "this_building_count": count,
            "compare_as": (f"{here} heat/hot-water complaints per 100 apartments here vs {area} for nearby rentals"
                           f"{ratio}, since 2023 ({count} {plural} across {units} apartments here)")}


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
    # A building HPD doesn't oversee has no HPD records, so a "0 per apartment" would rank it cleanest:
    # describe the area, but don't place this building in it.
    comparable = bool(b.units) and b.is_registered
    if not comparable:
        rentals.pop(b.bbl, None)
    if not rentals:
        raise ToolError(f"There are no HPD-registered rentals within {radius} miles of {b.label} to describe.",
                        "Try a larger radius (up to 0.5 miles), or offer the sunlight and night-walk checks.")

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
            "this_building": round(rate[b.bbl], 2) if comparable else None,
            "area_median": round(statistics.median(others), 2),
            "this_building_vs_area": describe_rank(rate[b.bbl], others) if comparable else
            "not compared: " + not_registered(b),
        },
        "heat_complaints_per_100_apartments_since_2023": (
            heat_comparison(heat.get(b.bbl, 0), b.units, area_heat, total_units) if comparable else
            {"this_building": None, "area": round(100 * area_heat / total_units, 1) if total_units else None}),
        "rats": {
            "share_of_nearby_rental_lots_with_a_failed_rat_inspection_since_2023": f"{round(100 * len(rat_lots) / len(rentals))}%",
            "this_building_failed_one": (rats.get(b.bbl, [0, 0])[1] > 0),
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


def matching_violations(open_rows: list[dict], issues: list[str], apartment: str | None) -> tuple[list, list]:
    """(violations to cite, matching violations in apartments we can't confirm are the tenant's)."""
    apt = (apartment or "").upper().replace("APT", "").strip()
    cite, elsewhere = [], []
    for v in open_rows:
        text = (v.get("novdescription") or "").upper()
        hits = [i for i in issues if any(k in text for k in ISSUE_KEYWORDS.get(i, []))]
        if not hits:
            continue
        v_apt = (v.get("apartment") or "").upper()
        # Cite public-area violations, and the tenant's own apartment's. Never another tenant's
        # apartment, and never guess which apartment is theirs.
        if not v_apt or (apt and v_apt == apt):
            cite.append({**v, "issues": hits})
        else:
            elsewhere.append({**v, "issues": hits})
    return cite, elsewhere


def draft_repair_request(state: dict, issues: list[str], details: str | None = None, apartment: str | None = None,
                         tenant_name: str | None = None) -> dict:
    b = current_building(None, state)
    if not issues:
        raise ToolError("No issues given.", f"Pass one or more of: {', '.join(ISSUE_KEYWORDS)}.")
    unknown = [i for i in issues if i not in ISSUE_KEYWORDS]
    if unknown:
        raise ToolError(f"Unknown issue type(s): {unknown}.", f"Use only: {', '.join(ISSUE_KEYWORDS)}.")

    cited, elsewhere = matching_violations(fetch_open_violations(b), issues, apartment)
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
        **({"matching_violations_in_other_apartments": sorted({v.get("apartment") for v in elsewhere}),
            "next_step": ("Open violations matching these issues exist in the apartment(s) listed. If the user lives "
                          "in one of them, ask them to confirm, then call again with apartment set to cite them. "
                          "Don't assume.")} if elsewhere and not apartment else {}),
        "escalation_steps": ESCALATION_STEPS,
        "escalation_source": ESCALATION_SOURCE,
        "note": (("No matching open violations in public areas" + (" or the tenant's apartment" if apartment else "")
                  + ": the letter uses the tenant's description. Filing a 311 complaint creates an official record.")
                 if not cited else
                 "Cited violations are open in HPD records for this building" + (" and apartment." if apartment else ".")
                 ) + " This is a template, not legal advice.",
    }


# --- Tool 9 ---

SUN_NOTE = ("Direct sun only. Rooms can still be bright from light reflected off buildings across the street. "
            "Not modeled: trees, fire escapes, window recesses, buildings newer than the footprint data. Ranges "
            "come from 12 runs varying floor height, window position along the wall and neighbors' heights "
            "(a sensitivity analysis, not a confidence interval).")
COMPASS_BEARINGS = {"north": 0, "northeast": 45, "east": 90, "southeast": 135, "south": 180, "southwest": 225,
                    "west": 270, "northwest": 315}
_sites: dict[str, sun.Site] = {}


def site_for(b: nyc.Building) -> sun.Site:
    if b.bbl not in _sites:  # footprints don't change during a session; keep the parsed geometry
        _sites[b.bbl] = sun.Site(b)
    return _sites[b.bbl]


def street_name(b: nyc.Building) -> str:
    """'155 East 92 Street' -> 'East 92 Street'."""
    first = b.label.split(",")[0]
    return first.split(" ", 1)[1] if first[:1].isdigit() and " " in first else first


def building_floors(b: nyc.Building, site: sun.Site) -> int:
    return b.floors or max(1, round(site.target["height_ft"] / 10.5))


def blocker_info(site: sun.Site, index: int, side: dict) -> dict:
    """The building that most often blocks the sun: where it is and how tall."""
    blk = site.buildings[index]
    points = [p for ring in blk["rings"] for p in ring]
    cx, cy = sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)
    a, c = side["start"], side["end"]
    wx, wy = (a[0] + c[0]) / 2, (a[1] + c[1]) / 2
    bearing = math.degrees(math.atan2(cx - wx, cy - wy)) % 360
    address = None
    if blk.get("bbl"):
        try:
            rows = nyc.soda(nyc.PLUTO, **{"$select": "address", "$where": f"bbl={int(float(blk['bbl']))}", "$limit": 1})
            address = (rows[0].get("address") or "").title() or None if rows else None
        except DataSourceError:
            pass  # the address is a nice-to-have; the height and direction still tell the story
    lon, lat = site.to_lonlat(cx, cy)
    return {"address": address, "height_ft": round(blk["height_ft"]), "distance_m": round(math.dist((wx, wy), (cx, cy))),
            "direction": sun.compass(bearing), "lat": round(lat, 6), "lon": round(lon, 6)}


def side_geometry(site: sun.Site, side: dict) -> list[list[float]]:
    return [[round(v, 6) for v in site.to_lonlat(*p)[::-1]] for p in (side["start"], side["end"])]


def pick_sides(sides: list[dict], wanted: str) -> list[dict]:
    windows = [s for s in sides if s["kind"] != "shared_wall"]
    if wanted == "all":
        return windows
    if wanted in COMPASS_BEARINGS:
        target = COMPASS_BEARINGS[wanted]
        best = min(windows, key=lambda s: abs((s["bearing"] - target + 180) % 360 - 180), default=None)
        return [best] if best and abs((best["bearing"] - target + 180) % 360 - 180) <= 45 else []
    return [s for s in windows if s["kind"] == wanted]


def sweep_summary(rows: list[tuple[int, float]]) -> str:
    """[(2, 0.0), (3, 0.0), (5, 1.0)] -> '0h floors 2-3, ~1h floor 5'."""
    parts, i = [], 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][1] == rows[i][1]:
            j += 1
        floors = f"floor {rows[i][0]}" if i == j else f"floors {rows[i][0]}-{rows[j][0]}"
        hours = "0h" if rows[i][1] == 0 else f"~{rows[i][1]:g}h"
        parts.append(f"{hours} {floors}")
        i = j + 1
    return ", ".join(parts)


def estimate_sunlight(state: dict, floor="all", side: str = "all", address: str | None = None) -> dict:
    b = current_building(address, state)
    site = site_for(b)
    sides = site.facades(street_name(b))
    top_floor = building_floors(b, site)
    dates = sun.season_dates(date.today())

    if str(floor).strip().lower() == "all":
        street = next((s for s in sides if s["kind"] == "street"), None)
        if street is None:
            raise ToolError("Couldn't identify the street side of this building's footprint.",
                            "Ask the user which direction their windows face (e.g. west), then call with that side and a floor number.")
        winter, today = [], []
        for f in range(1, top_floor + 1):
            winter.append((f, round(2 * sun.ensemble(site, street, f, dates["winter (Dec 21)"])["hours"]) / 2))
            today.append((f, round(2 * sun.ensemble(site, street, f, dates["today"])["hours"]) / 2))
        return {
            "address": b.label, "side": street["label"], "floors": top_floor,
            "winter_sun_by_floor": sweep_summary(winter), "today_sun_by_floor": sweep_summary(today),
            "lowest_floor_with_1h_winter_sun": next((f for f, h in winter if h >= 1), None),
            "note": "Hours rounded to the nearest half hour (median of 12 runs). " + SUN_NOTE,
        }

    try:
        floor = int(floor)
    except (TypeError, ValueError):
        raise ToolError(f"Floor '{floor}' isn't a number.", "Pass the floor as a number like '4', or 'all' for every floor.")
    if not 1 <= floor <= top_floor:
        raise ToolError(f"{b.label} has {top_floor} floors in city records, so floor {floor} doesn't exist.",
                        f"Ask the user which floor (1-{top_floor}) their apartment is on.")

    chosen = pick_sides(sides, side)
    if not chosen:
        available = sorted({s["kind"] for s in sides if s["kind"] != "shared_wall"} | {s["direction"] for s in sides})
        raise ToolError(f"This building has no '{side}' side with windows in the footprint data.",
                        f"Use one of: all, {', '.join(available)}.")

    results = []
    for s in chosen:
        seasons = {name: sun.ensemble(site, s, floor, day) for name, day in dates.items()}
        entry = {"side": s["label"], "facing_degrees": round(s["bearing"]),
                 "sun": {name: sun.describe(r) for name, r in seasons.items()},
                 "hours_median_min_max": {name: [r["hours"], *r["range"]] for name, r in seasons.items()},
                 "wall_lat_lon": side_geometry(site, s)}
        today_blocker = seasons["today"].get("main_blocker")
        if today_blocker and today_blocker["hours_blocked"] >= 0.5:
            entry["main_blocker_today"] = {**blocker_info(site, today_blocker["index"], s),
                                           "hours_blocked": today_blocker["hours_blocked"]}
        results.append(entry)
    results.sort(key=lambda e: -e["hours_median_min_max"]["today"][0])
    return {
        "address": b.label, "floor": floor, "floors_in_building": top_floor,
        "dates": {name: d.isoformat() for name, d in dates.items()},
        "sides": results,
        "note": SUN_NOTE,
    }


# --- Tool 10 ---

# Listing phrases -> the claim they make and how we check it. No LLM inside the tool:
# a keyword map keeps the check reproducible and the evidence traceable.
LISTING_CLAIMS = [
    ("sunny", r"sun[- ]?(drenched|filled|lit|soaked)|\bsunny\b|\bsunlight\b|southern exposure"),
    ("bright", r"\bbright\b|light[- ]filled|natural light|tons of light|flooded with light|\bairy\b"),
    ("well_maintained", r"well[- ]maintained|well[- ]kept"),
    ("unit_condition", r"\brenovated\b|pristine|immaculate|meticulous|mint condition|newly updated|brand[- ]new"),
    ("quiet", r"\bquiet\b|peaceful|tranquil|serene"),
    ("pest_free", r"pest[- ]free|no pests|vermin[- ]free|no (bed ?bugs|roaches|mice)"),
    ("responsive_management", r"responsive (management|landlord|super)|great landlord|attentive (management|landlord)|professionally managed"),
    ("heat_included", r"heat (and hot water )?included|heat & hot water included|utilities included"),
    ("warm_in_winter", r"\bwarm\b|toasty|great heat|plenty of heat"),
    ("safe", r"\bsafe\b|great block|safe block"),
    ("near_subway", r"near (the )?(subway|train)|steps (from|to) (the )?(subway|train)|close to (the )?(subway|train)|blocks? (from|to) (the )?(subway|train)"),
]

ADDRESS_IN_TEXT = re.compile(
    r"\b(\d{1,5}(?:-\d{1,4})?\s+(?:(?:east|west|e\.?|w\.?|north|south)\s+)?[a-z0-9.' ]{2,40}?\s"
    r"(?:avenue|ave|street|st|boulevard|blvd|place|pl|road|rd|drive|dr|parkway|pkwy|terrace|ter|lane|ln|court|ct)\b\.?"
    r"(?:,?\s*(?:apt\.?|apartment|unit|#)\s*\w+)?(?:,?\s*(?:manhattan|brooklyn|queens|bronx|the bronx|staten island|new york|ny))*(?:,?\s*\d{5})?)",
    re.IGNORECASE)
FLOOR_IN_TEXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)[- ]floor\b|\bfloor\s+(\d{1,2})\b|\b(first|second|third|fourth|fifth|"
                           r"sixth|seventh|eighth|ninth|tenth)[- ]floor\b", re.IGNORECASE)
WORD_NUMBERS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
                "ninth": 9, "tenth": 10}
SUPPORTED, PARTLY, NOT_SUPPORTED, CANT_VERIFY = ("supported", "partly supported", "not supported by city records",
                                                 "can't verify")
VERDICTS = (SUPPORTED, PARTLY, NOT_SUPPORTED, CANT_VERIFY)


def floor_from_text(text: str) -> int | None:
    m = FLOOR_IN_TEXT.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2)) if (m.group(1) or m.group(2)) else WORD_NUMBERS[m.group(3).lower()]


def noise_complaints(b: nyc.Building) -> dict:
    """311 noise complaints geocoded to this lot in the last 12 months."""
    since = date.today().replace(year=date.today().year - 1).isoformat()
    rows = nyc.soda(nyc.NOISE_311, **{"$select": "complaint_type, count(*) as n",
                                      "$where": f"bbl='{b.bbl}' AND created_date >= '{since}' AND complaint_type like 'Noise%'",
                                      "$group": "complaint_type"})
    return {r["complaint_type"]: int(r["n"]) for r in rows}


def judge_claim(claim: str, b: nyc.Building, state: dict, floor: int | None, cache: dict) -> dict:
    """One listing claim -> verdict, evidence, and which tool the evidence came from."""
    def run(name: str, fn, **kwargs):
        if name not in cache:
            cache[name] = fn(state=state, **kwargs)
        return cache[name]

    if claim in ("sunny", "bright"):
        if floor is None:
            return {"verdict": CANT_VERIFY, "evidence": "The listing doesn't say which floor; sunlight depends on it.",
                    "source_tool": "estimate_sunlight"}
        r = run("estimate_sunlight", estimate_sunlight, floor=floor)
        today = {s["side"]: s["hours_median_min_max"]["today"][0] for s in r["sides"]}
        street = next((s for s in r["sides"] if s["side"].startswith("street")), None)
        shown = ([street] if street else []) + [s for s in r["sides"] if s is not street][:2]
        per_side = "; ".join(f"{s['side']}: {s['sun']['today']}" for s in shown)
        # We don't know which way the apartment faces: only call it when every
        # window side agrees, or when the street side alone is clearly sunny.
        if street and street["hours_median_min_max"]["today"][0] >= 3 or min(today.values()) >= 3:
            verdict = SUPPORTED
        elif max(today.values()) < 1.5:
            verdict = NOT_SUPPORTED
        else:
            verdict = CANT_VERIFY
        caveat = "It depends on which way the apartment's windows face: ask the broker. " if verdict == CANT_VERIFY else ""
        if claim == "bright":
            # Brightness includes reflected and diffuse light, which we don't model: direct
            # sun can support the claim but never refute it.
            if verdict == SUPPORTED:
                verdict = PARTLY
            elif verdict == NOT_SUPPORTED:
                verdict = CANT_VERIFY
            caveat += ("'Bright' includes reflected light, which isn't modeled, so direct sun is only part of "
                       "the answer. ")
        return {"verdict": verdict, "source_tool": "estimate_sunlight",
                "evidence": (f"Floor {floor}, direct sun today by side: {per_side}. " + caveat +
                             "(Direct-sun rule: street side or every side 3+ h today = supported; every side "
                             "under 1.5 h = not supported.)")}

    if claim in ("well_maintained", "unit_condition"):
        m = run("check_maintenance_record", check_maintenance_record)
        hazardous = m["open_by_class"]["B"]["count"] + m["open_by_class"]["C"]["count"]
        longest = m["days_open"]["longest"] or 0
        classes = m["open_by_class"]
        evidence = (f"{m['open_violations']} open HPD violations in the building ({m['open_per_apartment']} per "
                    f"apartment): {classes['C']['count']} class C (immediately hazardous), {classes['B']['count']} "
                    f"class B (hazardous), {classes['A']['count']} class A"
                    + (f"; the oldest has been open {longest} days." if longest else "."))
        bad = m["open_by_class"]["C"]["count"] or longest > 365
        if claim == "unit_condition":
            # 'Pristine' or 'renovated' describes the unit; building records can only partly support it.
            evidence += (" City records describe the building, not this unit's finishes, so a clean record is "
                         "partial support at best. Ask to see the unit and when it was renovated.")
            verdict = PARTLY if hazardous == 0 else CANT_VERIFY
            return {"verdict": verdict, "evidence": evidence, "source_tool": "check_maintenance_record"}
        if bad:
            return {"verdict": NOT_SUPPORTED, "evidence": evidence, "source_tool": "check_maintenance_record"}
        return {"verdict": SUPPORTED if hazardous == 0 else CANT_VERIFY, "evidence": evidence,
                "source_tool": "check_maintenance_record"}

    if claim == "quiet":
        noise = cache.setdefault("noise", noise_complaints(b))
        total = sum(noise.values())
        evidence = (f"{total} 311 noise complaints at this address in the last 12 months"
                    + (f" ({', '.join(f'{k}: {v}' for k, v in noise.items())})" if noise else "")
                    + ". Few complaints means few people called 311, not that it's quiet, so that's partial "
                      "support at best. (Under 3 = partly supported, 12+ = not supported.)")
        verdict = PARTLY if total < 3 else NOT_SUPPORTED if total >= 12 else CANT_VERIFY
        return {"verdict": verdict, "evidence": evidence, "source_tool": "311 noise complaints"}

    if claim == "pest_free":
        p = run("check_pests", check_pests)
        rats = p["rodent_inspections_since_2023"]
        filings = p["bedbug_filings"] if isinstance(p["bedbug_filings"], list) else []
        infested = filings[0]["infested_units"] if filings else 0
        evidence = (f"{rats['failed_for_rats']} of {rats['inspections']} rat inspections since 2023 failed"
                    + (f" (last {rats['last_failed']})" if rats["last_failed"] else "")
                    + (f"; latest bedbug filing ({filings[0]['filed']}): {infested} infested units, "
                       f"{filings[0]['eradicated_units']} eradicated." if filings else "; no bedbug filings on record."))
        verdict = NOT_SUPPORTED if rats["failed_for_rats"] or infested else SUPPORTED if rats["inspections"] else CANT_VERIFY
        return {"verdict": verdict, "evidence": evidence, "source_tool": "check_pests"}

    if claim == "responsive_management":
        m = run("check_maintenance_record", check_maintenance_record)
        median_open = m["days_open"]["median"]
        fixed = m["fixed_since_2023"]
        if not m["open_violations"] and not fixed["count"]:
            return {"verdict": CANT_VERIFY, "source_tool": "check_maintenance_record",
                    "evidence": "No open violations and none issued since 2023, so there's no repair record to "
                                "measure responsiveness by (a good sign in itself)."}
        evidence = (f"{m['open_violations']} open violations" +
                    (f", open a median of {median_open:g} days" if median_open is not None else "") +
                    (f"; {fixed['count']} violations since 2023 took a median of {fixed['median_days_to_close']:g} "
                     "days to close" if fixed["count"] else "; none closed since 2023") +
                    ". (Median open over 180 days = not supported; 60 or less = supported.)")
        if median_open is not None and median_open > 180:
            verdict = NOT_SUPPORTED
        elif (median_open is None or median_open <= 60) and (fixed["median_days_to_close"] or 0) <= 60:
            verdict = SUPPORTED
        else:
            verdict = CANT_VERIFY
        return {"verdict": verdict, "evidence": evidence, "source_tool": "check_maintenance_record"}

    if claim in ("heat_included", "warm_in_winter"):
        n = run("get_neighborhood_context", get_neighborhood_context)
        heat = n["heat_complaints_per_100_apartments_since_2023"]
        record = (f"Heat/hot-water complaints since 2023: {heat['this_building']} per 100 apartments here vs "
                  f"{heat['area']} for nearby rentals.")
        if claim == "heat_included":
            # Who pays for heat is a billing term, not how well the heat works.
            return {"verdict": CANT_VERIFY, "source_tool": None,
                    "evidence": "That's a lease term (who pays for heat); check that it's written into the lease.",
                    "context": record + " That's about whether the heat works, not who pays for it.",
                    "context_source_tool": "get_neighborhood_context"}
        verdict = NOT_SUPPORTED if heat["this_building"] > (heat["area"] or 0) else PARTLY
        return {"verdict": verdict, "source_tool": "get_neighborhood_context",
                "evidence": record + (" Fewer complaints than the area is partial support: not every cold "
                                      "apartment gets reported." if verdict == PARTLY else "")}

    if claim == "safe":
        if "night_walk_check" in TOOL_MAP:
            r = run("night_walk_check", TOOL_MAP["night_walk_check"])
            return {"verdict": CANT_VERIFY, "evidence": r.get("summary", ""), "source_tool": "night_walk_check"}
        return {"verdict": CANT_VERIFY, "evidence": "Safety claims aren't checked yet.", "source_tool": None}

    if claim == "near_subway":
        snap = nyc.load_snapshot()
        nearest = min(snap.get("stations", []), default=None,
                      key=lambda st: nyc.miles_between(b.lat, b.lon, st["lat"], st["lon"]))
        if not nearest:
            return {"verdict": CANT_VERIFY, "evidence": "Station data unavailable.", "source_tool": None}
        miles = nyc.miles_between(b.lat, b.lon, nearest["lat"], nearest["lon"])
        minutes = round(miles * 1609 * 1.3 / 80)
        verdict = SUPPORTED if minutes <= 10 else NOT_SUPPORTED if minutes > 15 else CANT_VERIFY
        return {"verdict": verdict, "source_tool": "subway stations",
                "evidence": (f"Nearest station: {nearest['name']} ({' '.join(nearest['routes'])}), about {minutes} min "
                             "walk (straight line x 1.3 at 80 m/min). (10 min or less = supported.)")}

    return {"verdict": CANT_VERIFY, "evidence": "No city record covers this claim.", "source_tool": None}


def fact_check_listing(state: dict, listing_text: str, floor=None) -> dict:
    text = (listing_text or "").strip()
    if len(text) < 10:
        raise ToolError("The listing text is empty or too short.", "Ask the user to paste the listing description.")

    m = ADDRESS_IN_TEXT.search(text)
    found_address = m.group(1).strip(" ,.") if m else None
    if found_address:
        try:
            b = current_building(found_address, state)
        except ToolError:
            if not state.get("current_bbl"):
                raise ToolError(f"Couldn't look up the address in the listing ('{found_address}').",
                                "Ask the user for the building's full address with borough.")
            b = current_building(None, state)
            found_address = None
    else:
        b = current_building(None, state)  # raises NO_BUILDING if nothing is selected yet

    floor = floor if floor not in (None, "") else floor_from_text(text)
    floor = int(floor) if floor is not None and str(floor).isdigit() else None

    found = []
    for claim, pattern in LISTING_CLAIMS:
        hit = re.search(pattern, text, re.IGNORECASE)
        if hit:
            start = max(0, hit.start() - 40)
            found.append((claim, ("..." if start else "") + text[start:hit.end() + 40].replace("\n", " ").strip() + "..."))

    def judge(claim: str) -> dict:
        try:
            return judge_claim(claim, b, state, floor, cache)
        except ToolError as e:
            return {"verdict": CANT_VERIFY, "evidence": e.error, "source_tool": None}
        except DataSourceError:
            return {"verdict": CANT_VERIFY, "evidence": "City data didn't respond for this check.", "source_tool": None}

    # Each claim needs different city data; fetch them at once (cold queries take seconds each).
    cache: dict = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        verdicts = list(pool.map(judge, [claim for claim, _ in found]))
    claims = [{"claim": claim, "listing_says": quote, **v} for (claim, quote), v in zip(found, verdicts)]

    if not claims:
        return {"address": b.label, "claims": [],
                "note": "No checkable claims found (looked for sun/light, maintenance and condition, quiet, pests, "
                        "management, heat, safety and subway phrases)."}
    return {
        "address": b.label,
        "address_source": "found in the listing" if found_address else "the building already being discussed",
        "floor_checked": floor,
        "claims": claims,
        "summary": {v: sum(c["verdict"] == v for c in claims) for v in VERDICTS},
        "note": ("Verdicts compare listing language with city records using the thresholds stated in each evidence "
                 "line. 'Not supported by city records' means the records point the other way, not that anyone lied."),
    }


# --- Tool 13 ---

LEASE_NOTE = ("This is a checklist, not legal advice. Raise flags as questions with the landlord. For disputes: "
              "311, Met Council on Housing (https://www.metcouncilonhousing.org/), or a tenant attorney.")
CORPORATE_WORDS = {"llc", "inc", "corp", "corporation", "co", "company", "the", "lp", "ltd", "realty", "management",
                   "mgmt", "associates", "assoc", "group", "holdings", "by", "its", "managing", "member"}


def name_tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in CORPORATE_WORDS}


def same_party(lease_name: str, registered: list[str]) -> bool:
    """Does the lease's landlord name match any registered owner/officer/agent?"""
    mine = name_tokens(lease_name)
    for other in registered:
        theirs = name_tokens(other)
        # "92nd Street 4 LLC" and "92nd Street 6 LLC" are different companies: numbers must agree.
        numbers_mine = {w for w in mine if w.isdigit()}
        numbers_theirs = {w for w in theirs if w.isdigit()}
        if numbers_mine and numbers_theirs and numbers_mine != numbers_theirs:
            continue
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.5:
            return True
    return False


def unit_floor(unit: str | None) -> int | None:
    """'4B' -> 4, '12F' -> 12, '1203' -> 12; None when the unit isn't numbered by floor."""
    if not unit:
        return None
    m = re.match(r"^(\d{1,4})[A-Z]{0,2}$", unit)
    if not m:
        return None
    digits = m.group(1)
    return int(digits) if len(digits) <= 2 else int(digits[:-2])


def check_against_records(facts: dict, b: nyc.Building, state: dict) -> tuple[list[dict], dict]:
    flags, context = [], {}
    contacts = b.contacts_by_role()
    registered = [c["name"] for people in contacts.values() for c in people] + ([b.owner_name] if b.owner_name else [])
    if facts["landlord_name"] and registered and not same_party(facts["landlord_name"], registered):
        owner = (contacts.get("owner_company") or contacts.get("owner_person") or [{"name": b.owner_name}])[0]["name"]
        agent = (contacts.get("managing_agent") or [{}])[0].get("name")
        flags.append(lease.flag(lease.CHECK, f"Landlord: {facts['landlord_name']}",
                                f"The lease names '{facts['landlord_name']}' as landlord, but HPD's registration lists "
                                f"the owner as '{owner}'" + (f" and the managing agent as '{agent}'" if agent else "") +
                                ". It could be a legitimate agent or affiliate: ask how they're related to the "
                                "registered owner, and who to contact for repairs.", category="city_records"))
        context["registered_owner"] = owner

    floor = unit_floor(facts["unit"])
    if floor and b.floors and floor > b.floors:
        flags.append(lease.flag(lease.INCONSISTENT, f"Apartment {facts['unit']}",
                                f"Apartment {facts['unit']} suggests floor {floor}, but city records show "
                                f"{b.floors} floors. Confirm the apartment number and floor.", category="city_records"))

    pests = check_pests(state=state)
    filings = pests["bedbug_filings"] if isinstance(pests["bedbug_filings"], list) else []
    if not facts["disclosures_present"]["bedbug_history"] and any(f["infested_units"] for f in filings):
        latest = next(f for f in filings if f["infested_units"])
        flags.append(lease.flag(lease.CHECK, None,
                                f"No bedbug disclosure in the lease, and the owner's {latest['filed']} filing reported "
                                f"{latest['infested_units']} infested apartment(s). For a new lease, ask for the "
                                "bedbug history form.", "bedbug_disclosure"))
    context["bedbug_filings"] = filings[:2]

    if facts["heat_included"]:
        heat = get_tenant_complaints(state=state, category="heat_hot_water")
        context["heat_complaints_since_2023"] = {
            "count": heat["matching_complaints"], "by_month": heat["by_month"]["counts"] if heat["by_month"] else {},
            "meaning": "The lease covers heat; these complaints show whether tenants reported going without it."}

    if facts["unit"]:
        in_unit = [v for v in fetch_open_violations(b) if (v.get("apartment") or "").upper() == facts["unit"]]
        hazardous = [v for v in in_unit if v.get("class") == "C"]
        if hazardous:
            flags.append(lease.flag(lease.CHECK, f"Apartment {facts['unit']}",
                                    f"Apartment {facts['unit']} has {len(hazardous)} open class C (immediately hazardous) "
                                    "violation(s) in HPD records: " + "; ".join(
                                        split_violation_text(v.get("novdescription"))["what"][:90] for v in hazardous[:3])
                                    + ". Ask for them to be fixed before you move in.", category="city_records"))
        context["open_violations_in_unit"] = len(in_unit)
    return flags, context


LEASE_FOCUS = {"all", "money", "terms", "disclosures", "city_records"}


def review_lease(state: dict, focus: str = "all") -> dict:
    text = state.get("lease_text")
    if not text:
        raise ToolError("No lease has been shared in this conversation.",
                        "Ask the user to upload the lease (PDF, .docx or .txt, paperclip button) or paste its text, or to "
                        "try the sample lease.")
    if focus not in LEASE_FOCUS:
        raise ToolError(f"Unknown focus '{focus}'.", "Use one of: all, money, terms, disclosures, city_records.")

    facts = lease.extract(text)
    flags = lease.check_consistency(facts) + lease.check_rules(facts)

    # The building: the lease's premises address (best-scoring candidate that the city geocoder
    # accepts), else the building being discussed. A landlord's office address scores low.
    b, context, building_note, tried = None, {}, None, []
    for candidate in facts["address_candidates"]:
        if candidate["why"].endswith("zip outside NYC"):
            tried.append(f"{candidate['address']} (outside NYC)")
            continue
        for variant in lease.address_variants(candidate["address"]):
            try:
                b = current_building(variant, state)
                building_note = f"Used the premises address '{variant}' ({candidate['why']})."
                break
            except ToolError:
                tried.append(variant)
        if b:
            break
    if b:
        used = building_note.split("'")[1]
        others = [c for c in facts["address_candidates"] if not any(v == used for v in lease.address_variants(c["address"]))]
        # Name other NYC addresses; only count the rest. Leases list people's mailing and previous home
        # addresses, which aren't needed here and shouldn't be repeated back.
        nyc_others = [f"{c['address']} ({c['why']})" for c in others if "outside NYC" not in c["why"]]
        outside = sum("outside NYC" in c["why"] for c in others)
        if nyc_others:
            building_note += f" Other addresses in the lease, not used: {'; '.join(nyc_others)}."
        if outside:
            building_note += (f" {outside} address{'es' if outside > 1 else ''} outside NYC (such as the landlord's "
                              f"office or a tenant's mailing address) {'were' if outside > 1 else 'was'} ignored.")
    if not b:
        try:
            b = current_building(None, state)
            nyc_tried = [t for t in dict.fromkeys(tried) if "(outside NYC)" not in t]
            building_note = ("No address in the lease matched an NYC building"
                             + (f" (tried: {'; '.join(nyc_tried)})" if nyc_tried else "")
                             + f"; used the building being discussed, {b.label}.")
        except ToolError as e:
            nyc_tried = [t for t in dict.fromkeys(tried) if "(outside NYC)" not in t]
            building_note = (f"City-record checks skipped: no NYC building address found in the lease"
                             + (f" (tried: {'; '.join(nyc_tried)})" if nyc_tried else "") + f". {e.next_step}")
    if b:
        try:
            record_flags, context = check_against_records(facts, b, state)
            flags += record_flags
        except DataSourceError:
            building_note = "City-record checks skipped: NYC Open Data didn't respond."

    if focus != "all":
        flags = [f for f in flags if f["category"] == focus]
    order = {lease.LIKELY_NOT_ALLOWED: 0, lease.INCONSISTENT: 1, lease.CHECK: 2}
    flags.sort(key=lambda f: order[f["severity"]])

    extracted = {k: ({kk: vv for kk, vv in v.items() if not kk.startswith("_")} if isinstance(v, dict) else v)
                 for k, v in facts.items() if k not in ("heat_clauses", "rent_mentions", "disclosures_present")}
    extracted["rents_stated"] = [m["amount"] for m in facts["rent_mentions"]]
    extracted["address_candidates"] = [c for c in facts["address_candidates"] if "outside NYC" not in c["why"]]
    return {
        "address": b.label if b else None,
        "is_sample": lease.is_sample(text),
        "flag_counts": {sev: sum(f["severity"] == sev for f in flags) for sev in order},
        "flags": flags,
        "missing_disclosures": lease.missing_disclosures(facts, b.units if b else None, b.year_built if b else None),
        "extracted": extracted,
        "city_record_context": context,
        "building_note": building_note,
        "note": LEASE_NOTE,
    }


# --- Tool 11 ---

CORRIDOR_M = 60            # half-width of the walk corridor (NYPD points are offset to intersections)
WALK_M_PER_MIN = 80        # an easy walking pace
DETOUR = 1.3               # street grid vs straight line
NIGHT_WALK_NOTE = ("Reported incidents only, from NYPD complaint and shooting data. NYPD locations are approximate "
                   "(offset to the nearest intersection), and the route is a straight-line corridor about 60 m wide, "
                   "not your exact path. Counts describe places and times, not people.")
_comparison_cache: dict = {}


def station_label(st: dict) -> str:
    return f"{st['name']} ({' '.join(st['routes'])})"


MAX_WALK_M = 2000  # farther than this isn't "the walk home from the subway"


def station_key(st: dict) -> str:
    return f"{st['name']}|{' '.join(st['routes'])}|{st['lat']:.5f},{st['lon']:.5f}"


def normalize_station(text: str) -> set[str]:
    t = text.lower().replace("-", " ")
    t = re.sub(r"\b(street|st)\b", "st", t)
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"\b(avenue|av|ave)\b", "av", t)
    t = re.sub(r"\b(parkway|pkwy)\b", "pkwy", t)
    return set(re.sub(r"[^a-z0-9 ]", " ", t).split()) - {"station", "stop", "the", "subway"}


def nearby_list(b: nyc.Building, stations: list[dict], limit: int = 5) -> str:
    return "; ".join(f"{station_label(st)} {st['_m']:.0f} m" for st in stations[:limit])


def find_station(name: str | None, line: str | None, b: nyc.Building, stations: list[dict]) -> dict:
    """The station to walk from: the nearest one, the nearest on a line, or the best name match nearby.

    Never guesses: a weak or ambiguous name match returns an error listing the candidates, because
    'Central Park North (110 St)' once matched the 6 train's '110 St', 1,355 m away on Lexington.
    """
    def with_distance(st: dict) -> dict:
        return {**st, "_m": nyc.miles_between(b.lat, b.lon, st["lat"], st["lon"]) * 1609.34}

    by_distance = sorted((with_distance(st) for st in stations), key=lambda st: st["_m"])
    near = [st for st in by_distance if st["_m"] <= MAX_WALK_M]
    if not near:
        raise ToolError(f"No subway station is within {MAX_WALK_M / 1609:.1f} miles of {b.label}.",
                        f"Tell the user the nearest is {station_label(by_distance[0])}, {by_distance[0]['_m'] / 1609:.1f} miles away.")

    if line:
        wanted = line.strip().upper().replace("TRAIN", "").replace("LINE", "").strip()
        on_line = [st for st in near if wanted in [r.upper() for r in st["routes"]]]
        if not on_line:
            anywhere = [st for st in by_distance if wanted in [r.upper() for r in st["routes"]]][:2]
            raise ToolError(f"No {wanted} train station within walking distance of {b.label}.",
                            ("The nearest " + wanted + " stations are " + nearby_list(b, anywhere) + ". " if anywhere else "")
                            + f"Stations within walking distance: {nearby_list(b, near)}.")
        near = on_line
        if not name:
            return near[0]
    if not name:
        return near[0]

    asked = normalize_station(name)
    # Every nearby station whose name contains all the words asked ("110 St" is in three names
    # near 111th St). One clear winner (the nearest, at under half the next one's distance) is
    # fine; otherwise list them and let the user choose.
    containing = [st for st in near if asked and asked <= normalize_station(st["name"])]
    if containing:
        if len(containing) == 1 or containing[0]["_m"] < 0.5 * containing[1]["_m"]:
            return containing[0]
        raise ToolError(f"'{name}' could mean more than one station near {b.label}.",
                        f"Candidates: {nearby_list(b, containing)}. Ask the user which one, or pass line "
                        "(e.g. '2') for the nearest station on that line.")
    scored = []
    for st in near:
        tokens = normalize_station(st["name"])
        overlap = len(asked & tokens) / len(asked | tokens) if asked | tokens else 0
        scored.append((overlap, st))
    scored.sort(key=lambda x: (-x[0], x[1]["_m"]))
    best_score, best = scored[0]
    # Different stations with the same name (several "116 St"s) aren't ambiguous: take the nearest.
    rivals = [st for score, st in scored[1:] if score >= best_score - 0.1 and st["name"] != best["name"]]
    if best_score < 0.5:
        raise ToolError(f"No station within walking distance of {b.label} clearly matches '{name}'.",
                        f"Stations within walking distance: {nearby_list(b, near)}. Ask the user which one, "
                        "or pass line (e.g. '2') for the nearest station on a line.")
    if rivals:
        raise ToolError(f"'{name}' could mean more than one nearby station.",
                        f"Candidates: {nearby_list(b, [best] + rivals)}. Ask the user which one, or pass line.")
    return best


def corridor_mask(points: np.ndarray, start: tuple, end: tuple, origin_lat: float) -> np.ndarray:
    """Which [lat, lon] points lie within CORRIDOR_M of the start-end segment."""
    kx, ky = 111320 * math.cos(math.radians(origin_lat)), 110540
    px, py = (points[:, 1] - start[1]) * kx, (points[:, 0] - start[0]) * ky
    ex, ey = (end[1] - start[1]) * kx, (end[0] - start[0]) * ky
    length2 = ex * ex + ey * ey or 1e-9
    t = np.clip((px * ex + py * ey) / length2, 0, 1)
    return np.hypot(px - t * ex, py - t * ey) <= CORRIDOR_M


def in_window(hour: int, after_hour: int) -> bool:
    return hour >= after_hour or hour < nyc.NIGHT_END_HOUR


def comparable_walks(length_m: float, after_hour: int, near: tuple | None = None) -> list[int]:
    """Reported night incidents along walks of the same length from every station, in 8 directions:
    the baseline this walk is compared with (snapshot data, so it's the same for every user)."""
    key = (round(length_m / 25), after_hour, near)
    if key in _comparison_cache:
        return _comparison_cache[key]
    snap = nyc.load_snapshot()
    rows = [p for p in snap["street_incidents"] if in_window(p[3], after_hour)]
    rows += [p for p in snap["shootings"] if in_window(p[3], after_hour)]
    pts = np.array([[p[0], p[1]] for p in rows])
    stations = snap["stations"]
    if near:
        stations = [st for st in stations if nyc.miles_between(near[0], near[1], st["lat"], st["lon"]) <= 2]
    counts = []
    for st in stations:
        dlat = length_m / 110540
        dlon = length_m / (111320 * math.cos(math.radians(st["lat"])))
        close = pts[(np.abs(pts[:, 0] - st["lat"]) < dlat + 0.001) & (np.abs(pts[:, 1] - st["lon"]) < dlon + 0.001)]
        for bearing in range(0, 360, 45):
            end = (st["lat"] + dlat * math.cos(math.radians(bearing)), st["lon"] + dlon * math.sin(math.radians(bearing)))
            counts.append(int(corridor_mask(close, (st["lat"], st["lon"]), end, st["lat"]).sum()) if len(close) else 0)
    _comparison_cache[key] = counts
    return counts


def live_incidents(start: tuple, end: tuple, window: list[str], after_hour: int) -> list[list]:
    """Street incidents and shootings in the corridor's bounding box, live from NYPD data."""
    pad_lat, pad_lon = CORRIDOR_M / 110540, CORRIDOR_M / (111320 * math.cos(math.radians(start[0])))
    s_, n_ = min(start[0], end[0]) - pad_lat, max(start[0], end[0]) + pad_lat
    w_, e_ = min(start[1], end[1]) - pad_lon, max(start[1], end[1]) + pad_lon
    box = f"latitude between {s_} and {n_} AND longitude between {w_} and {e_}"
    # Only date, time, offense and location are selected: no victim or suspect details ever reach the model.
    crime_query = {"$select": "cmplnt_num, cmplnt_fr_dt, cmplnt_fr_tm, ofns_desc, latitude, longitude",
                   "$where": (f"{box} AND cmplnt_fr_dt > '{window[0]}' AND cmplnt_fr_dt <= '{window[1]}T23:59:59' "
                              f"AND {nyc.NIGHT_COMPLAINTS} AND {nyc.STREET_OFFENSES} AND {nyc.STREET_PREMISES}"),
                   "$limit": 5000}
    swapped_box = f"latitude between {w_} and {e_} AND longitude between {s_} and {n_}"  # see nyc.shooting_point
    shot_query = {"$select": "incident_key, occur_date, occur_time, latitude, longitude",
                  "$where": (f"(({box}) OR ({swapped_box})) AND occur_date > '{window[0]}' "
                             f"AND occur_date <= '{window[1]}T23:59:59'"), "$limit": 1000}
    with ThreadPoolExecutor(max_workers=3) as pool:
        historic = pool.submit(nyc.soda, nyc.CRIME_HISTORIC, timeout=15, attempts=2, **crime_query)
        ytd = pool.submit(nyc.soda, nyc.CRIME_YTD, timeout=15, attempts=2, **crime_query)
        shots = pool.submit(nyc.soda, nyc.SHOOTINGS, timeout=15, attempts=2, **shot_query)
        crime_rows = {r["cmplnt_num"]: r for r in historic.result() + ytd.result()}.values()  # late reports sit in both
        shot_rows = shots.result()
    points = [[float(r["latitude"]), float(r["longitude"]), r["cmplnt_fr_dt"][:10], int(r["cmplnt_fr_tm"][:2]),
               r["ofns_desc"]] for r in crime_rows if r.get("latitude")]
    for r in shot_rows:
        where = nyc.shooting_point(r)
        if where:
            points.append([where[0], where[1], r["occur_date"][:10], nyc.shooting_hour(r["occur_time"]), "SHOOTING"])
    return [p for p in points if in_window(p[3], after_hour)]


def night_walk_check(state: dict, address: str | None = None, station: str | None = None, line: str | None = None,
                     after_hour: int = 21) -> dict:
    b = current_building(address, state)
    snap = nyc.load_snapshot()
    if not snap.get("stations"):
        raise ToolError("Subway station data is missing.", "Tell the user the night-walk check is unavailable.")
    try:
        after_hour = int(after_hour)
    except (TypeError, ValueError):
        raise ToolError(f"after_hour '{after_hour}' isn't an hour.", "Pass an hour from 21 to 23 (24-hour clock).")
    if not nyc.NIGHT_START_HOUR <= after_hour <= 23:
        raise ToolError(f"after_hour must be 21-23 (got {after_hour}).",
                        "The comparison data covers 9pm-5am; use 21, 22 or 23.")

    st = find_station(station, line, b, snap["stations"])
    # Several spellings ("2 train", "110 St", "Malcolm X Plaza") can resolve to the same station:
    # answer from the first check instead of running it again.
    key = (b.bbl, station_key(st), after_hour)
    done = state.setdefault("night_walks", {})
    if key in done:
        return {**done[key], "duplicate": "Same station as an earlier check in this conversation; reuse that result."}
    start, end = (st["lat"], st["lon"]), (b.lat, b.lon)
    straight_m = st["_m"]
    window = snap["crime_window"]

    try:
        points, source = live_incidents(start, end, window, after_hour), "live NYPD data"
    except DataSourceError:
        points = [p for p in snap["street_incidents"] if in_window(p[3], after_hour)]
        points += [[*p, "SHOOTING"] for p in snap["shootings"] if in_window(p[3], after_hour)]
        source = f"snapshot as of {snap['as_of'].get('street_incidents')} (live NYPD data didn't respond)"
    on_route = []
    if points:
        mask = corridor_mask(np.array([[p[0], p[1]] for p in points]), start, end, b.lat)
        on_route = [p for p, keep in zip(points, mask) if keep]

    def label(offense: str) -> str:
        return "shooting" if offense == "SHOOTING" else nyc.OFFENSE_LABELS.get(offense, offense.lower())

    # Where along the walk: split the route into thirds by distance from the station.
    thirds = Counter()
    for p in on_route:
        d_station = nyc.miles_between(*start, p[0], p[1])
        d_home = nyc.miles_between(*end, p[0], p[1])
        position = d_station / (d_station + d_home or 1)
        thirds["near the station" if position < 1 / 3 else "near the building" if position > 2 / 3 else "mid-walk"] += 1

    baseline = comparable_walks(straight_m, after_hour)
    nearby = comparable_walks(straight_m, after_hour, near=(round(b.lat, 2), round(b.lon, 2)))
    n = len(on_route)

    def share_with_more(counts: list[int]) -> int:
        return round(100 * sum(c > n for c in counts) / len(counts)) if counts else None

    def share_with_same(counts: list[int]) -> int:
        return round(100 * sum(c == n for c in counts) / len(counts)) if counts else None

    minutes = round(straight_m * DETOUR / WALK_M_PER_MIN)
    hours = f"{after_hour - 12}pm-5am"
    by_type = dict(Counter(label(p[4]) for p in on_route).most_common())
    summary = (f"{n} reported street incident(s) between {hours} along the ~{minutes}-min walk from "
               f"{station_label(st)} in the 12 months to {window[1]}; "
               f"of similar-length walks from NYC stations, {share_with_more(baseline)}% had more and "
               f"{share_with_same(baseline)}% had the same number.")
    result = {
        "address": b.label,
        "station": {"name": st["name"], "lines": st["routes"], "accessible": st["ada"]},
        "walk": {"minutes": minutes, "straight_line_m": round(straight_m),
                 "route_lat_lon": [[round(start[0], 6), round(start[1], 6)], [round(end[0], 6), round(end[1], 6)]]},
        "hours_checked": hours,
        "period": {"from": window[0], "to": window[1]},
        "incidents_on_route": n,
        "by_type": by_type,
        "where_on_route": dict(thirds),
        "comparison": {
            "citywide": {"walks_compared": len(baseline), "median_incidents": float(np.median(baseline)) if baseline else None,
                         "share_with_more_incidents_pct": share_with_more(baseline),
                         "share_with_same_count_pct": share_with_same(baseline)},
            "within_2_miles": {"walks_compared": len(nearby), "median_incidents": float(np.median(nearby)) if nearby else None,
                               "share_with_more_incidents_pct": share_with_more(nearby),
                               "share_with_same_count_pct": share_with_same(nearby)},
            "method": ("Same-length straight walks from every subway station (and those within 2 miles), in 8 "
                       "directions, counting the same offenses and hours from the snapshot."),
        },
        "points": [[round(p[0], 5), round(p[1], 5), label(p[4]), p[2], p[3]] for p in on_route],
        "points_columns": ["lat", "lon", "type", "date", "hour"],
        "summary": summary,
        "source": source,
        "note": NIGHT_WALK_NOTE + (" The NYPD publishes about 3 months behind, so the period ends "
                                   f"{window[1]}."),
    }
    done[key] = result
    return result


# --- What the model sees ---

ADDRESS_ARG = {
    "type": "string",
    "description": ("Only for a building other than the one being discussed: NYC street address with borough, e.g. "
                    "'155 East 92nd Street, Manhattan'. Omit for follow-ups about the current building."),
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
                    "description": ("House number, street and borough (or zip), e.g. '155 East 92nd Street, Manhattan' "
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
                    "apartment": {"type": "string",
                                  "description": "The tenant's apartment as THEY stated it, e.g. '2N'. Omit if they haven't said; never infer it."},
                    "tenant_name": {"type": "string",
                                    "description": "Name to sign with, only if the user gave it. Omit to leave a placeholder."},
                },
                "required": ["issues"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_sunlight",
            "description": (
                "How much direct sun does a window get? Ray-traces the sun past every nearby building's real "
                "height for today, Dec 21, Mar 20 and Jun 21, per side of the building (street side, rear, side, "
                "light court), with hours, times and an uncertainty range, plus the building that blocks it most. "
                "floor='all' gives winter and today sun for every floor on the street side ('which floor gets "
                "winter sun?'). Without a floor, it returns every floor on the street side."),
            "parameters": {
                "type": "object",
                "properties": {
                    "floor": {"type": "string",
                              "description": ("Apartment floor as a number, e.g. '4'. Omit (or 'all') when the floor "
                                              "isn't known: returns winter and today sun for every floor on the street side.")},
                    "side": {"type": "string", "enum": ["all", "street", "rear", "side", "court", *COMPASS_BEARINGS],
                             "description": ("Which windows: 'street' (front), 'rear', 'side', 'court', a compass "
                                             "direction the windows face, or 'all' (default) if the user doesn't know.")},
                    "address": ADDRESS_ARG,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fact_check_listing",
            "description": (
                "Check a rental listing's claims against city records. Pass the listing text the user pasted; it "
                "finds the address (or uses the building being discussed) and checks phrases about sun and light, "
                "maintenance and unit condition, quiet, pests, management, heat and the subway, returning "
                "supported / partly supported / not supported by city records / can't verify for each, with the "
                "evidence and the rule used. Use whenever the user pastes listing text or quotes a listing's claims."),
            "parameters": {
                "type": "object",
                "properties": {
                    "listing_text": {"type": "string", "description": "The listing description, verbatim."},
                    "floor": {"type": "string",
                              "description": "Apartment floor if known and not in the text, e.g. '4' (needed for sunlight claims)."},
                },
                "required": ["listing_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_lease",
            "description": (
                "Review the lease the user uploaded or pasted (it's already stored; don't pass the text). Extracts "
                "rent, deposit, dates, fees and clauses; flags internal inconsistencies, terms that are likely not "
                "allowed under NY/NYC law (deposit cap, late and application fees, broker fees, attorney's fees, "
                "heat), missing required disclosures, and mismatches with city records for the building (registered "
                "owner, floors, bedbug filings, violations in the unit). Each flag quotes the clause and links the "
                "official rule. Use when the user mentions their lease or asks to review one."),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "enum": ["all", "money", "terms", "disclosures", "city_records"],
                              "description": ("Limit flags to 'money' (rent, deposit, fees), 'terms', 'disclosures' "
                                              "or 'city_records' (owner, floors, violations). Default 'all'.")},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "night_walk_check",
            "description": (
                "Is the area safe at night / how's the walk home from the subway? Picks a subway station (the "
                "nearest by default, the nearest on a given line, or a named one near the building), estimates the "
                "walk, and counts reported street incidents (robbery, felony assault, sex crimes, theft from a "
                "person, shootings) along that route at night over the latest 12 months of NYPD data, where on the "
                "route they happened, and how that compares with similar walks from other stations. For several "
                "lines, call it once per line in the same turn."),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": ADDRESS_ARG,
                    "line": {"type": "string",
                             "description": "A subway line, e.g. '2', 'C', 'Q': uses the nearest station on that line. Prefer this when the user names a train."},
                    "station": {"type": "string",
                                "description": ("A station name the user gave, e.g. '96 St'. Only stations within ~1.2 miles of "
                                                "the building are considered; an unclear name returns the candidates. Omit for the nearest.")},
                    "after_hour": {"type": "integer", "minimum": 21, "maximum": 23,
                                   "description": "Start of the night window on a 24-hour clock (window ends 5am). Default 21 (9pm)."},
                },
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
    "estimate_sunlight": estimate_sunlight,
    "fact_check_listing": fact_check_listing,
    "review_lease": review_lease,
    "night_walk_check": night_walk_check,
}


def run_tool(name: str, args: dict, state: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    name = (name or "").strip()  # seen in testing: Gemini called " estimate_sunlight"
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

    address = " ".join(sys.argv[1:]) or "155 East 92nd Street, Manhattan"
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
        ("draft_repair_request", {"issues": ["water_leak", "paint_plaster"], "apartment": "18",
                                  "details": "the bathroom ceiling has been leaking for months"}),
        ("estimate_sunlight", {"floor": "4"}),
        ("estimate_sunlight", {"floor": "all"}),
        ("fact_check_listing", {"listing_text": "Sun-drenched 4th floor 2BR in a well-maintained building at "
                                                "155 East 92nd Street, Manhattan. Quiet block, steps to the subway, "
                                                "heat included, responsive management."}),
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
