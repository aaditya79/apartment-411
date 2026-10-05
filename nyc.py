"""The data layer: everything that talks to NYC Open Data and the city geocoder.

One place that knows how to turn a street address into the IDs the city uses
(BBL, BIN, HPD registration IDs), so no tool repeats that work and the model
never has to see or pass an ID.

Datasets are Socrata (SoQL). No API key is required; NYC_OPEN_DATA_APP_TOKEN is
sent when present only to avoid anonymous throttling.

Run `uv run nyc.py` to resolve the three baseline buildings.
"""

import gzip
import json
import math
import os
import re
import statistics
from difflib import SequenceMatcher
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from cachetools import TTLCache

# --- Endpoints ---

GEOSEARCH = "https://geosearch.planninglabs.nyc/v2/search"
SODA = "https://data.cityofnewyork.us/resource/{}.json"
NY_STATE_SODA = "https://data.ny.gov/resource/{}.json"

PLUTO = "64uk-42ks"            # lot data: apartments, year built, owner of record
REGISTRATIONS = "tesw-yqqr"    # who is registered with HPD for each building
CONTACTS = "feu5-w2e2"         # the people and companies on each registration
VIOLATIONS = "wvxf-dwi5"       # HPD housing-code violations
COMPLAINTS = "ygpa-z7cr"       # tenant complaints to HPD (one row per problem)
EVICTIONS = "6z8x-wfk4"        # marshal-executed evictions
LITIGATION = "59kj-x8nc"       # HPD cases in housing court
BEDBUGS = "wz6d-d3jb"          # annual bedbug filings
RODENTS = "p937-wjvj"          # health department rodent inspections
FOOTPRINTS = "5zhs-2jue"       # building outlines with roof heights
CRIME_YTD = "5uac-w243"        # NYPD complaints, current year (lags ~3 months)
CRIME_HISTORIC = "qgea-i56i"   # NYPD complaints, previous years
SHOOTINGS = "5ucz-vwe8"        # NYPD shootings; most rows have latitude/longitude swapped
PRECINCTS = "y76i-bdw7"        # police precinct polygons with shape_area
STATIONS = "39hk-dx4f"         # MTA subway stations (data.ny.gov)

SINCE = "2023-01-01"  # the window every "recent" count in this app uses

# --- Filters shared by the live tools and build_snapshot.py, so both count the same thing ---
# (values verified with $group queries on the live datasets, 2026-10-05)

# Rodent rows that are real inspections; "Bait applied", "Stoppage done" etc. are follow-up visits.
RAT_INSPECTIONS = ("result in ('Passed', 'Failed for Rat Activity', 'Failed for Rat Activity and Other Reason', "
                   "'Failed for Other Reason')")
# Inspections that found rats ("Failed for Other Reason" is e.g. garbage, not rats).
RAT_FAILURES = "result like 'Failed for Rat Activity%'"

# Offenses a pedestrian could face on the walk home (robbery, assault, sex crimes, snatching).
STREET_OFFENSES = (
    "(ofns_desc in ('ROBBERY', 'FELONY ASSAULT', 'SEX CRIMES', 'RAPE') "
    "OR pd_desc like 'LARCENY,GRAND FROM PERSON%')"
)
# Outdoor premises only: an assault inside an apartment says nothing about the walk.
STREET_PREMISES = "prem_typ_desc in ('STREET', 'PARK/PLAYGROUND', 'BUS STOP')"
# The walk-home window: 9pm to 5am, by the hour the incident started.
NIGHT_START_HOUR, NIGHT_END_HOUR = 21, 5
NIGHT_COMPLAINTS = f"(cmplnt_fr_tm >= '{NIGHT_START_HOUR}:00:00' OR cmplnt_fr_tm < '0{NIGHT_END_HOUR}:00:00')"


def is_night(hour: int) -> bool:
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def shooting_hour(occur_time: str) -> int:
    """The shootings file mixes two time formats: rows through 2025 say '23:25:00',
    rows added in 2026 say '1899-12-31T23:25:00.000' (a spreadsheet date artifact)."""
    return int(occur_time.split("T")[-1][:2])


def shooting_point(row: dict) -> tuple[float, float] | None:
    """(lat, lon) for a shootings row. In 5ucz-vwe8 every row through 2025 has the
    latitude and longitude columns swapped (verified per year: 2022-2025 100% swapped,
    2026 0%), so pick the value in NYC's latitude range rather than trusting the label."""
    try:
        a, b = float(row["latitude"]), float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    lat, lon = (a, b) if 40 < a < 41.5 else (b, a)
    return (lat, lon) if 40 < lat < 41.5 and -75 < lon < -73 else None
OFFENSE_LABELS = {
    "ROBBERY": "robbery", "FELONY ASSAULT": "felony assault", "SEX CRIMES": "sex crime",
    "RAPE": "sex crime", "GRAND LARCENY": "theft from a person",
}

SNAPSHOT_PATH = Path(__file__).parent / "data" / "snapshot.json.gz"

BOROUGH_BY_ID = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}

# PLUTO building classes we are likely to meet, in words a renter would use.
# Only the common ones: unknown codes are reported as the raw code.
BUILDING_CLASSES = {
    "A1": "one-family house", "A2": "one-family house", "A3": "one-family house",
    "A4": "one-family house", "A5": "one-family attached house", "A9": "one-family house",
    "B1": "two-family house", "B2": "two-family house", "B3": "two-family house",
    "B9": "two-family house",
    "C0": "three-family house", "C1": "walk-up apartment building",
    "C2": "walk-up apartment building", "C3": "walk-up apartment building",
    "C4": "walk-up apartment building", "C5": "walk-up apartment building",
    "C6": "walk-up co-op", "C7": "walk-up apartment building with stores",
    "C8": "walk-up co-op", "C9": "garden apartment complex",
    "D0": "elevator co-op", "D1": "elevator apartment building",
    "D2": "elevator apartment building", "D3": "elevator apartment building",
    "D4": "elevator co-op", "D5": "elevator apartment building",
    "D6": "elevator apartment building with stores", "D7": "elevator apartment building with stores",
    "D8": "elevator apartment building", "D9": "elevator apartment building",
    "R1": "condominium unit", "R2": "condominium unit", "R3": "condominium unit",
    "R4": "condominium unit", "R6": "condominium in a 1-3 family building",
    "R9": "condominium unit", "RR": "condominium rentals",
    "S1": "mixed residential with one store", "S2": "mixed residential with stores",
    "S3": "mixed residential with stores", "S4": "mixed residential with stores",
    "S5": "mixed residential with stores", "S9": "mixed residential with stores",
}

# How the registration's contact types read to a renter. Order = display order.
CONTACT_ROLES = {
    "CorporateOwner": "owner_company",
    "IndividualOwner": "owner_person",
    "JointOwner": "owner_person",
    "HeadOfficer": "head_officer",
    "Officer": "officer",
    "Shareholder": "officer",
    "Agent": "managing_agent",
    "SiteManager": "site_manager",
    "Lessee": "lessee",
}


class DataSourceError(Exception):
    """A city data source failed. Carries a message a tool can hand to the model."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AddressError(DataSourceError):
    """The address could not be matched to one NYC building. `suggestion` is the
    geocoder's closest guess, when it had one, so the model can ask "did you mean"."""

    def __init__(self, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.suggestion = suggestion


# --- HTTP with a one-hour cache ---

# Keyed by (url, sorted params). The city's data updates daily at best, so an
# hour-old answer is still accurate, and follow-up questions in a chat are instant.
_cache: TTLCache = TTLCache(maxsize=4096, ttl=3600)
_cache_lock = threading.Lock()  # FastAPI runs sync endpoints in a thread pool


def _app_token_headers() -> dict:
    token = os.environ.get("NYC_OPEN_DATA_APP_TOKEN")
    return {"X-App-Token": token} if token else {}


def soda(dataset: str, timeout: int = 25, attempts: int = 3, base: str = SODA, **params) -> list[dict]:
    """Query one Socrata dataset. Raises DataSourceError instead of leaking requests errors.

    Measured: the same query is usually under 1s but 5-13s when Socrata has not
    cached it, and bare 503s happen. So retry timeouts, connection errors and 5xx
    with a short backoff; never retry a 400, which means the query itself is wrong.
    """
    url = base.format(dataset)
    key = (url, tuple(sorted((k, str(v)) for k, v in params.items())))
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    last_error = DataSourceError("NYC Open Data did not respond.")
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.5 * 2 ** (attempt - 1))
        try:
            r = requests.get(url, params=params, headers=_app_token_headers(), timeout=timeout)
        except requests.Timeout:
            last_error = DataSourceError(f"NYC Open Data did not respond within {timeout}s.")
            continue
        except requests.RequestException as e:
            last_error = DataSourceError(f"Could not reach NYC Open Data: {type(e).__name__}.")
            continue

        if r.status_code >= 500 or r.status_code == 429:
            last_error = DataSourceError(f"NYC Open Data is busy (HTTP {r.status_code}).",
                                         status_code=r.status_code)
            continue
        if not r.ok:
            # 400 means the query is wrong (e.g. text vs numeric column); callers
            # handle that themselves, so hand back the code rather than retrying.
            raise DataSourceError(f"NYC Open Data rejected the query (HTTP {r.status_code}).",
                                  status_code=r.status_code)
        try:
            rows = r.json()
        except ValueError:
            last_error = DataSourceError("NYC Open Data returned something that was not JSON.")
            continue

        with _cache_lock:
            _cache[key] = rows
        return rows

    raise last_error


def soda_bbl_in(dataset: str, bbls: list[str], **params) -> list[dict]:
    """Filter a dataset by a list of BBLs, whichever way that dataset stores them.

    Some datasets type bbl as text and some as a number, and the wrong one is a
    400, not an empty result. Try quoted first, then bare.
    """
    base_where = params.pop("$where", "")
    for values in (",".join(f"'{b}'" for b in bbls), ",".join(bbls)):
        where = f"bbl in ({values})" + (f" AND {base_where}" if base_where else "")
        try:
            return soda(dataset, **{**params, "$where": where})
        except DataSourceError as e:
            if e.status_code != 400:
                raise
    return []


# --- Small helpers ---


def normalize_bbl(value) -> str:
    """PLUTO returns '1019930107.00000000'; everything else returns '1019930107'."""
    return str(int(float(value)))


def split_bbl(bbl: str) -> tuple[str, str, str]:
    """10-digit BBL -> (borough id, block, lot), unpadded as the city's filters want."""
    return bbl[0], str(int(bbl[1:6])), str(int(bbl[6:10]))


def make_bbl(boro, block, lot) -> str:
    return f"{boro}{int(block):05d}{int(lot):04d}"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def days_since(value: str | None) -> int | None:
    d = parse_date(value)
    return (datetime.now() - d).days if d else None


def days_between(start: str | None, end: str | None) -> int | None:
    a, b = parse_date(start), parse_date(end)
    return (b - a).days if a and b else None


def median(values: list) -> float | None:
    return round(statistics.median(values), 2) if values else None


def chunks(items: list, size: int = 150):
    """Split long IN (...) lists so the request URL stays a sane length."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def contact_name(c: dict) -> str:
    """A registration contact is either a company or a person."""
    person = f"{c.get('firstname', '')} {c.get('lastname', '')}".strip()
    return c.get("corporationname") or person.title() or "(unnamed)"


def business_address(c: dict) -> str:
    parts = [c.get("businesshousenumber"), c.get("businessstreetname"), c.get("businesszip")]
    text = " ".join(p for p in parts if p).strip()
    return text.title() if text.isupper() else text


# --- Geocoding ---

# GeoSearch never says "no match" for a plausible-looking address: it silently
# returns its nearest guess with the same confidence as a real hit. Measured:
# "123 Fake Street" -> "123 WEST 123 STREET", "116th & Broadway" -> "116 B'WAY,
# Brooklyn". So we check the answer against what was asked before trusting it.

_STREET_WORDS = {
    "ave": "avenue", "av": "avenue", "avenue": "avenue", "st": "street", "str": "street",
    "street": "street", "blvd": "boulevard", "boulevard": "boulevard", "pl": "place",
    "place": "place", "rd": "road", "road": "road", "dr": "drive", "drive": "drive",
    "pkwy": "parkway", "parkway": "parkway", "ln": "lane", "lane": "lane", "ct": "court",
    "court": "court", "ter": "terrace", "terrace": "terrace", "sq": "square", "hwy": "highway",
    "e": "east", "east": "east", "w": "west", "west": "west", "n": "north", "north": "north",
    "s": "south", "south": "south", "b'way": "broadway", "bway": "broadway",
}
_GENERIC = {"avenue", "street", "boulevard", "place", "road", "drive", "parkway", "lane", "court",
            "terrace", "square", "highway", "the", "of", "at"}
_STREET_TYPES = _GENERIC - {"the", "of", "at"}
_PLACE_WORDS = {"new", "york", "ny", "nyc", "usa", "us", "manhattan", "brooklyn", "queens",
                "bronx", "staten", "island", "si", "bk", "bx", "mn", "qn", "city"}
_BOROUGH_HINTS = {"manhattan": "Manhattan", "brooklyn": "Brooklyn", "queens": "Queens",
                  "bronx": "Bronx", "staten": "Staten Island"}


def _address_tokens(text: str) -> list[str]:
    """Lowercase words with abbreviations expanded and ordinals stripped (30th -> 30)."""
    text = re.sub(r"\b(apt|apartment|unit|fl|floor|#)\s*[\w-]+", " ", text.lower())
    words = re.findall(r"[a-z0-9']+", text)
    words = [re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w) for w in words]
    return [_STREET_WORDS.get(w, w) for w in words]


def _check_match(asked: str, label: str, borough: str) -> None:
    """Raise AddressError if the geocoder's label is not the place the user asked for."""
    if re.search(r"\s(&|and|at)\s|/", f" {asked.lower()} "):
        raise AddressError(f"'{asked}' looks like an intersection, which the city geocoder can't place.")

    asked_words = [w for w in _address_tokens(asked) if not re.fullmatch(r"\d{5}", w)]  # drop zip codes
    label_words = set(_address_tokens(label.split(",")[0]))

    # A borough named in the question must be the borough we found.
    for word, name in _BOROUGH_HINTS.items():
        if word in asked_words and name != borough:
            raise AddressError(f"'{asked}' matched '{label}' in {borough}, not {name}.", suggestion=f"{label} ({borough})")

    house = asked_words[0] if asked_words and asked_words[0][0].isdigit() else None
    street = [w for w in asked_words[1 if house else 0:] if w not in _PLACE_WORDS and w not in _GENERIC]
    label_house = label.split()[0] if label[:1].isdigit() else None

    house_ok = house is None or house.split("-")[0] == (label_house or "").split("-")[0] or house == label_house
    # Claremont Avenue and Clermont Place are different streets: if both name a
    # street type, the types must agree.
    asked_types, label_types = set(asked_words) & _STREET_TYPES, label_words & _STREET_TYPES
    type_ok = not (asked_types and label_types) or bool(asked_types & label_types)
    # Fuzzy per word so spelling variants pass (Centre/Center, Douglas/Douglass)
    # while different streets (Fake vs West 123) still fail.
    street_ok = all(w in label_words or any(SequenceMatcher(None, w, lw).ratio() >= 0.8 for lw in label_words)
                    for w in street)
    if not (house_ok and street_ok and type_ok):
        raise AddressError(f"No exact NYC match for '{asked}'. The closest the city geocoder found was '{label}'.",
                           suggestion=f"{label} ({borough})")


def geocode(address: str, need_lot: bool = True) -> dict:
    """Street address -> {label, bbl, bin, lat, lon, borough, neighborhood}.

    Uses the city's own geocoder (GeoSearch, built on the city's PAD file).
    need_lot=False accepts landmarks without a single tax lot (e.g. for a commute destination).
    """
    data = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.5 * attempt)
        try:
            r = requests.get(GEOSEARCH, params={"text": address, "size": 5}, timeout=15)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError):
            continue
    if data is None:
        raise DataSourceError("The NYC address geocoder is not responding.")

    features = data.get("features") or []
    if not features:
        raise AddressError(f"No NYC address matched '{address}'.")

    # The top hit ignores the borough you typed ("184 Clermont Ave, Brooklyn" ->
    # Staten Island first), so check all 5 candidates against what was asked.
    matches, first_error = [], None
    for feature in features:
        props = feature["properties"]
        label = props.get("label", address).replace(", New York, NY, USA", "").replace(", NY, USA", "")
        street_part, _, rest = label.partition(",")
        label = (street_part.title() if street_part.isupper() else street_part) + (f",{rest}" if rest else "")
        borough = props.get("borough") or ""
        try:
            _check_match(address, label, borough)
            matches.append((feature, label, borough))
        except AddressError as e:
            first_error = first_error or e
    if not matches:
        raise first_error

    # "100 Broadway" exists in Manhattan and Brooklyn. Without a borough or zip to
    # decide, ask rather than guess.
    words = _address_tokens(address)
    has_hint = any(w in _BOROUGH_HINTS for w in words) or any(re.fullmatch(r"\d{5}", w) for w in words)
    boroughs = {b: lab for _, lab, b in matches}
    if not has_hint and len(boroughs) > 1:
        options = [f"{lab} ({b})" for b, lab in boroughs.items()]
        raise AddressError(f"'{address}' exists in more than one borough: {'; '.join(options)}.",
                           suggestion=" or ".join(options))
    feature, label, borough = matches[0]
    props = feature["properties"]

    pad = props.get("addendum", {}).get("pad", {})
    bbl = pad.get("bbl")
    if need_lot and not bbl:
        raise AddressError(f"'{address}' matched '{label}', which is not a single building lot.")

    lon, lat = feature["geometry"]["coordinates"]
    return {
        "label": label,
        "bbl": normalize_bbl(bbl) if bbl else None,
        "bin": pad.get("bin"),
        "lat": lat,
        "lon": lon,
        "borough": borough or (BOROUGH_BY_ID.get(str(bbl)[0], "") if bbl else ""),
        "neighborhood": props.get("neighbourhood") or "",
    }


# --- The building record every tool starts from ---


@dataclass
class Building:
    bbl: str
    bin: str | None
    label: str
    borough: str
    neighborhood: str
    lat: float
    lon: float
    units: int                      # residential units (PLUTO unitsres)
    units_total: int
    year_built: int | None
    floors: int | None
    bldgclass: str
    bldgclass_label: str
    owner_name: str                 # PLUTO owner of record, not the HPD registration
    registration_ids: list[str] = field(default_factory=list)   # every registration this lot has had
    latest_registration: dict | None = None
    registration_status: str = "none"       # current | lapsed | none
    registration_ends: str | None = None
    registration_note: str | None = None
    contacts: list[dict] = field(default_factory=list)

    @property
    def is_registered(self) -> bool:
        return bool(self.registration_ids)

    def contacts_by_role(self) -> dict[str, list[dict]]:
        """{'owner_company': [{'name', 'business_address'}], 'head_officer': [...], ...}, deduped."""
        grouped: dict[str, list[dict]] = {}
        for role in dict.fromkeys(CONTACT_ROLES.values()):
            seen = set()
            for c in self.contacts:
                if CONTACT_ROLES.get(c.get("type")) != role:
                    continue
                name = contact_name(c)
                if name.upper() in seen:
                    continue
                seen.add(name.upper())
                grouped.setdefault(role, []).append({"name": name, "business_address": business_address(c) or None})
        return grouped


def registration_status(end_date: str | None) -> tuple[str, str | None]:
    """HPD registrations expire every Sep 1 and renewals post late, so a few weeks
    past the end date is normal. Only call it lapsed after 90 days. Never 'illegal'."""
    if not end_date:
        return "none", None
    overdue = days_since(end_date)
    if overdue is None or overdue <= 0:
        return "current", None
    if overdue <= 90:
        return "current", (f"The registration period ended {end_date[:10]} ({overdue} days ago). "
                           "Registrations renew on an annual Sep 1 cycle, so a renewal is probably in progress.")
    return "lapsed", f"The registration ended {end_date[:10]} and has not been renewed in the {overdue} days since."


_building_cache: TTLCache = TTLCache(maxsize=512, ttl=3600)


def resolve_building(address: str) -> Building:
    """Address -> everything the tools need to query the city about this building."""
    key = address.strip().lower()
    with _cache_lock:
        if key in _building_cache:
            return _building_cache[key]

    place = geocode(address)
    bbl = place["bbl"]
    boro, block, lot = split_bbl(bbl)

    lot_rows = soda(PLUTO, **{
        "$select": "bbl, address, unitsres, unitstotal, yearbuilt, numfloors, ownername, bldgclass, latitude, longitude",
        "$where": f"bbl={int(bbl)}", "$limit": 1})
    p = lot_rows[0] if lot_rows else {}  # some lots (e.g. new condos) have no PLUTO row

    floors = p.get("numfloors")
    year = p.get("yearbuilt")
    bldgclass = (p.get("bldgclass") or "").strip()

    building = Building(
        bbl=bbl,
        bin=place["bin"],
        label=place["label"],
        borough=place["borough"],
        neighborhood=place["neighborhood"],
        lat=place["lat"],
        lon=place["lon"],
        units=int(float(p.get("unitsres") or 0)),
        units_total=int(float(p.get("unitstotal") or 0)),
        year_built=int(float(year)) if year and float(year) > 0 else None,
        floors=int(float(floors)) if floors and float(floors) > 0 else None,
        bldgclass=bldgclass,
        bldgclass_label=BUILDING_CLASSES.get(bldgclass, f"building class {bldgclass}" if bldgclass else "unknown type"),
        owner_name=(p.get("ownername") or "").strip(),
    )

    # A lot can carry several registrations over the years. Keep all of them for
    # counting violations; use the most recent for who is responsible today.
    regs = soda(REGISTRATIONS, boroid=boro, block=block, lot=lot, **{"$limit": 100})
    if regs:
        regs.sort(key=lambda r: r.get("lastregistrationdate") or "", reverse=True)
        building.registration_ids = sorted({r["registrationid"] for r in regs})
        latest = regs[0]
        building.latest_registration = latest
        ends = latest.get("registrationenddate")
        building.registration_ends = ends[:10] if ends else None
        building.registration_status, building.registration_note = registration_status(ends)
        building.contacts = soda(CONTACTS, registrationid=latest["registrationid"], **{"$limit": 100})

    with _cache_lock:
        _building_cache[key] = building
    return building


# --- The offline snapshot (built by build_snapshot.py, committed to the repo) ---

_snapshot: dict | None = None
_snapshot_lock = threading.Lock()


def load_snapshot() -> dict:
    """Citywide per-building aggregates that are too slow to query live (see build_snapshot.py).

    Loaded once per process. Returns {} if the file is missing so tools can say so.
    """
    global _snapshot
    with _snapshot_lock:
        if _snapshot is None:
            try:
                with gzip.open(SNAPSHOT_PATH, "rt") as f:
                    _snapshot = json.load(f)
            except (OSError, ValueError):
                _snapshot = {}
        return _snapshot


if __name__ == "__main__":
    import sys

    tests = sys.argv[1:] or ["184 Claremont Ave, Manhattan", "2053 Frederick Douglass Blvd, Manhattan",
                             "350 East 30th Street, Manhattan"]
    for addr in tests:
        print(f"\n===== {addr} =====")
        started = time.time()
        try:
            b = resolve_building(addr)
        except AddressError as e:
            print(f"  AddressError: {e.message}  suggestion={e.suggestion}")
            continue
        except DataSourceError as e:
            print(f"  DataSourceError: {e.message}")
            continue
        print(f"  resolved in {time.time() - started:.1f}s")
        print(f"  {b.label}  (BBL {b.bbl}, BIN {b.bin}, {b.neighborhood}, {b.borough})")
        print(f"  {b.units} apartments / {b.units_total} total units, built {b.year_built}, "
              f"{b.floors} floors, {b.bldgclass} = {b.bldgclass_label}")
        print(f"  lat/lon: {b.lat}, {b.lon}")
        print(f"  PLUTO owner of record: {b.owner_name}")
        print(f"  registrations: {b.registration_ids} -> {b.registration_status}, ends {b.registration_ends}")
        if b.registration_note:
            print(f"  note: {b.registration_note}")
        for role, people in b.contacts_by_role().items():
            for c in people:
                print(f"    {role:15s} {c['name']:40s} {c['business_address'] or ''}")
