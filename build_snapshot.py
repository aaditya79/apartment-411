"""Build data/snapshot.json.gz: citywide aggregates that are too slow to query live.

Run offline (`uv run build_snapshot.py`, ~minutes) and commit the output. The app
only reads the file. Why a snapshot: comparing one building to its neighbors needs
counts for hundreds of buildings, and live queries for that took 47-63s in testing.

What it holds (BBL = 10-digit borough-block-lot string):
  open_violations_by_bbl  {bbl: [A, B, C]} open HPD violations right now, by class
  complaints_by_bbl       {bbl: n} distinct HPD complaints since SINCE
  heat_by_bbl             {bbl: n} distinct heat/hot-water complaints since SINCE
  rodents_by_lot          {bbl: [inspections, failed for rat activity]} since SINCE
  stations                MTA subway stations (static; the live list is slow cold)
  street_incidents        night-time street incidents for the last 12 months of
                          NYPD data: [lat, lon, "YYYY-MM-DD", hour, type]
  shootings               same window, night only: [lat, lon, "YYYY-MM-DD", hour]
  places                  groceries and gyms from OpenStreetMap: [lat, lon, kind, name]
  as_of                   {section: date built}, shown to the user by the area tools

`uv run build_snapshot.py` rebuilds everything; `uv run build_snapshot.py shootings places`
rebuilds only those sections and keeps the rest of the existing file.
"""

import gzip
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from nyc import (COMPLAINTS, CRIME_HISTORIC, CRIME_YTD, NIGHT_COMPLAINTS, NY_STATE_SODA, RAT_FAILURES,
                 RAT_INSPECTIONS, RODENTS, SHOOTINGS, SINCE, SNAPSHOT_PATH, SODA, STATIONS, STREET_OFFENSES,
                 STREET_PREMISES, VIOLATIONS, is_night, make_bbl, shooting_hour, shooting_point)

PAGE = 50000
BOROUGHS = ["1", "2", "3", "4", "5"]
OVERPASS = "https://overpass-api.de/api/interpreter"
NYC_BBOX = "40.49,-74.26,40.92,-73.70"  # south, west, north, east


def fetch_all(dataset: str, base: str = SODA, label: str = "", **params) -> list[dict]:
    """Every row of a query, 50k at a time, retrying each page 3x with backoff.

    Paging needs a stable $order, so callers always pass one.
    """
    rows, offset = [], 0
    while True:
        for attempt in range(3):
            try:
                started = time.time()
                r = requests.get(base.format(dataset), params={**params, "$limit": PAGE, "$offset": offset},
                                 timeout=300)
                r.raise_for_status()
                page = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                wait = 5 * 2 ** attempt
                print(f"    {label} page {offset // PAGE}: {type(e).__name__}, retrying in {wait}s")
                time.sleep(wait)
        else:
            raise SystemExit(f"Giving up on {label} after 3 attempts.")
        rows += page
        print(f"    {label}: {len(rows):,} rows ({time.time() - started:.1f}s for this page)")
        if len(page) < PAGE:
            return rows
        offset += PAGE


def fetch_many(dataset: str, jobs: dict[str, dict]) -> dict[str, list[dict]]:
    """Run several fetch_all queries at once. Socrata answers each small grouped
    query in seconds but one big one can take minutes, so split and parallelize."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {label: pool.submit(fetch_all, dataset, label=label, **params) for label, params in jobs.items()}
        return {label: f.result() for label, f in futures.items()}


def half_years(start: str) -> list[tuple[str, str]]:
    """[start, end) windows of ~6 months from start to today. Small windows keep
    each grouped complaint query under Socrata's timeout (one big one timed out at 120s)."""
    windows, d = [], date.fromisoformat(start)
    while d <= date.today():
        nxt = date(d.year + (d.month + 6 > 12), (d.month + 5) % 12 + 1, 1)
        windows.append((d.isoformat(), nxt.isoformat()))
        d = nxt
    return windows


def open_violations() -> dict:
    # One query per borough and class: the whole-borough query took 38s to over
    # 200s for Brooklyn in testing, the split ones finish in seconds.
    jobs = {f"violations boro {boro} class {cls}": {
        "$select": "block, lot, count(*) as n",
        "$where": f"violationstatus='Open' AND boroid='{boro}' AND class='{cls}'",
        "$group": "block, lot", "$order": "block, lot"} for boro in BOROUGHS for cls in "ABC"}
    counts = defaultdict(lambda: [0, 0, 0])
    for label, rows in fetch_many(VIOLATIONS, jobs).items():
        boro, cls = label.split()[2], label.split()[-1]
        for r in rows:
            try:
                counts[make_bbl(boro, r["block"], r["lot"])]["ABC".index(cls)] += int(r["n"])
            except (KeyError, ValueError):
                continue  # rows with a blank or non-numeric block/lot
    return dict(counts)


def complaints(heat_only: bool) -> dict:
    extra = " AND major_category='HEAT/HOT WATER'" if heat_only else ""
    jobs = {f"{'heat' if heat_only else 'all'} complaints boro {boro} {start}": {
        "$select": "bbl, count(distinct complaint_id) as n",
        "$where": (f"bbl between '{boro}000000000' and '{boro}999999999' "
                   f"AND received_date >= '{start}' AND received_date < '{end}'{extra}"),
        "$group": "bbl", "$order": "bbl"} for boro in BOROUGHS for start, end in half_years(SINCE)}
    counts = defaultdict(int)
    for rows in fetch_many(COMPLAINTS, jobs).values():
        for r in rows:
            if r.get("bbl"):
                counts[str(int(float(r["bbl"])))] += int(r["n"])  # a complaint has one date, so summing windows is exact
    return dict(counts)


def rodents() -> dict:
    jobs = {f"rodents boro {boro} {kind}": {
        "$select": "block, lot, count(*) as n",
        "$where": f"boro_code='{boro}' AND inspection_date >= '{SINCE}' AND {where}",
        "$group": "block, lot", "$order": "block, lot"}
        for boro in BOROUGHS
        for kind, where in (("all", RAT_INSPECTIONS), ("failed", RAT_FAILURES))}
    counts = defaultdict(lambda: [0, 0])
    for label, rows in fetch_many(RODENTS, jobs).items():
        boro, column = label.split()[2], int(label.endswith("failed"))
        for r in rows:
            try:
                counts[make_bbl(boro, r["block"], r["lot"])][column] += int(r["n"])
            except (KeyError, ValueError):
                continue
    return dict(counts)


def stations() -> list[dict]:
    rows = fetch_all(STATIONS, base=NY_STATE_SODA, label="subway stations", **{
        "$select": "stop_name, daytime_routes, gtfs_latitude, gtfs_longitude, complex_id, ada, borough",
        "$order": "complex_id"})
    return [{"name": r["stop_name"], "routes": r.get("daytime_routes", "").split(),
             "lat": round(float(r["gtfs_latitude"]), 6), "lon": round(float(r["gtfs_longitude"]), 6),
             "complex_id": r.get("complex_id"), "ada": r.get("ada") not in (None, "0")}
            for r in rows if r.get("gtfs_latitude")]


def crime_window() -> tuple[str, str]:
    """The 12 months ending at the latest date NYPD has published (it lags ~3 months)."""
    latest = requests.get(SODA.format(CRIME_YTD), params={"$select": "max(rpt_dt) as d"}, timeout=60).json()[0]["d"]
    end = datetime.fromisoformat(latest[:10]).date()
    return (end - timedelta(days=365)).isoformat(), end.isoformat()


def street_incidents(start: str, end: str) -> list[list]:
    points = {}
    for dataset in (CRIME_HISTORIC, CRIME_YTD):
        rows = fetch_all(dataset, label=f"street incidents {dataset}", **{
            "$select": "cmplnt_num, cmplnt_fr_dt, cmplnt_fr_tm, ofns_desc, latitude, longitude",
            "$where": (f"cmplnt_fr_dt > '{start}' AND cmplnt_fr_dt <= '{end}T23:59:59' AND {NIGHT_COMPLAINTS} "
                       f"AND {STREET_OFFENSES} AND {STREET_PREMISES} AND latitude IS NOT NULL"),
            "$order": "cmplnt_num"})
        for r in rows:
            # Keyed by complaint number: a late report can sit in both files, but two
            # incidents at the same corner and hour are still two incidents.
            points[r["cmplnt_num"]] = [round(float(r["latitude"]), 5), round(float(r["longitude"]), 5),
                                       r["cmplnt_fr_dt"][:10], int(r["cmplnt_fr_tm"][:2]), r["ofns_desc"]]
    return list(points.values())


def shootings(start: str, end: str) -> list[list]:
    # No time filter in SoQL: occur_time has two formats (see nyc.shooting_hour), and a
    # string comparison silently dropped every 2026 row in the first build.
    rows = fetch_all(SHOOTINGS, label="shootings", **{
        "$select": "incident_key, occur_date, occur_time, latitude, longitude",
        "$where": f"occur_date > '{start}' AND occur_date <= '{end}T23:59:59'",
        "$order": "incident_key"})
    expected = requests.get(SODA.format(SHOOTINGS), timeout=60, params={
        "$select": "count(distinct incident_key) as n",
        "$where": f"occur_date > '{start}' AND occur_date <= '{end}T23:59:59'"}).json()[0]["n"]
    points, located = {}, 0
    for r in rows:
        point = shooting_point(r)
        if point is None:
            continue
        located += 1
        hour = shooting_hour(r["occur_time"])
        if is_night(hour):
            points[r["incident_key"]] = [round(point[0], 5), round(point[1], 5), r["occur_date"][:10], hour]
    print(f"   shootings sanity check: {len({r['incident_key'] for r in rows})} incidents fetched, "
          f"dataset says {expected}; {located} located; {len(points)} at night")
    return list(points.values())


def places() -> list[list]:
    """Groceries and gyms citywide from OpenStreetMap, once, because the public
    Overpass servers returned 504s or timed out on most live tries in testing."""
    query = f"""[out:json][timeout:180];
(
  nwr["shop"~"^(supermarket|grocery|greengrocer)$"]({NYC_BBOX});
  nwr["leisure"="fitness_centre"]({NYC_BBOX});
);
out center tags;"""
    for attempt in range(4):
        try:
            r = requests.post(OVERPASS, data={"data": query}, timeout=240,
                              headers={"User-Agent": "apartment-411 (Columbia course project)"})
            r.raise_for_status()
            elements = r.json()["elements"]
            break
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"   Overpass attempt {attempt + 1}: {type(e).__name__}, retrying in 20s")
            time.sleep(20)
    else:
        raise SystemExit("Overpass did not answer; try again later.")
    out = []
    for e in elements:
        lat, lon = (e["lat"], e["lon"]) if "lat" in e else (e.get("center", {}).get("lat"), e.get("center", {}).get("lon"))
        tags = e.get("tags", {})
        if lat is None or not tags.get("name"):
            continue  # unnamed places aren't useful to tell a renter about
        kind = "gym" if tags.get("leisure") == "fitness_centre" else "grocery"
        out.append([round(lat, 5), round(lon, 5), kind, tags["name"]])
    return out


SECTIONS = {
    "open_violations_by_bbl": open_violations,
    "complaints_by_bbl": lambda: complaints(heat_only=False),
    "heat_by_bbl": lambda: complaints(heat_only=True),
    "rodents_by_lot": rodents,
    "stations": stations,
    "street_incidents": None,  # these two share a crime window, built below
    "shootings": None,
    "places": places,
}


if __name__ == "__main__":
    import sys

    started = time.time()
    wanted = sys.argv[1:] or list(SECTIONS)
    unknown = set(wanted) - set(SECTIONS)
    if unknown:
        raise SystemExit(f"Unknown sections {unknown}. Choose from {list(SECTIONS)}.")

    snapshot = {}
    if sys.argv[1:] and SNAPSHOT_PATH.exists():
        with gzip.open(SNAPSHOT_PATH, "rt") as f:
            snapshot = json.load(f)
    if not isinstance(snapshot.get("as_of"), dict):
        snapshot["as_of"] = {}
    snapshot["since"] = SINCE
    today = date.today().isoformat()

    for name in wanted:
        t = time.time()
        print(f"\n== {name}")
        if name in ("street_incidents", "shootings"):
            start, end = crime_window()
            snapshot["crime_window"] = [start, end]
            snapshot[name] = street_incidents(start, end) if name == "street_incidents" else shootings(start, end)
        else:
            snapshot[name] = SECTIONS[name]()
        snapshot["as_of"][name] = today
        print(f"   -> {len(snapshot[name]):,} entries in {time.time() - t:.0f}s")

    SNAPSHOT_PATH.parent.mkdir(exist_ok=True)
    with gzip.open(SNAPSHOT_PATH, "wt") as f:
        json.dump(snapshot, f, separators=(",", ":"))
    size = Path(SNAPSHOT_PATH).stat().st_size / 1e6
    print(f"\nWrote {SNAPSHOT_PATH} ({size:.2f} MB) in {(time.time() - started) / 60:.1f} min")
