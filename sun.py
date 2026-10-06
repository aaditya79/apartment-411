"""How much direct sun does an NYC apartment window get?

NYC's building footprints give every building's outline and roof height. For a
window on one side of a building, every 5 minutes we cast a ray from the window
toward the sun (NOAA sun-position formula) and check whether any taller building
is above that ray where the ray crosses its outline.

Unknowns (exact floor height, where along the facade the window is, footprint
height errors) are handled by running 12 versions and reporting the spread: an
uncertainty range from a sensitivity analysis, not a confidence interval.

Not modeled: trees, fire escapes, window recesses, buildings newer than the
footprint data, weather. Direct sun only: rooms can still be bright from light
reflected off buildings across the street.
"""

import math
from datetime import date, datetime, timedelta, timezone
from itertools import product
from zoneinfo import ZoneInfo

import numpy as np

import nyc

NY = ZoneInfo("America/New_York")
FT = 0.3048                       # feet -> meters
SEARCH_RADIUS_M = 350             # farther buildings almost never block a window
STEP_MIN = 5
FLOOR_HEIGHTS_FT = (10.0, 11.5)   # typical NYC floor-to-floor; 11.5 for prewar
WINDOW_POSITIONS = (0.2, 0.5, 0.8)  # left / center / right along the facade
HEIGHT_FACTORS = (0.9, 1.1)       # footprint roof heights can be off by ~10%
SHARED_WALL_M = 3                 # a neighbor this close means a party wall: no windows
LIGHT_COURT_M = 12                # this close means a court or shaft: little direct sun
STREET_MIN_OPEN_M = 15            # a street facade looks across at least this much open space


# --- Sun position (NOAA solar calculator equations; verified: NYC noon elevation
#     25.8 deg on Dec 21 and 72.6 deg on Jun 21) ---


def sun_position(when_utc: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(azimuth degrees clockwise from north, elevation degrees)."""
    jd = when_utc.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t) + math.sin(3 * mr) * 0.000289)
    omega = 125.04 - 1934.136 * t
    app_long = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(math.radians(omega)))
    decl = math.asin(math.sin(eps) * math.sin(math.radians(app_long)))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
                               + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                               - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    minutes = when_utc.hour * 60 + when_utc.minute + when_utc.second / 60
    hour_angle = math.radians(((minutes + eq_time + 4 * lon) % 1440) / 4 - 180)
    latr = math.radians(lat)
    cos_zen = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(hour_angle)
    elevation = 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))
    az = math.degrees(math.atan2(math.sin(hour_angle),
                                 math.cos(hour_angle) * math.sin(latr) - math.tan(decl) * math.cos(latr)))
    return (az + 180) % 360, elevation


def sun_track(day: date, lat: float, lon: float) -> list[tuple[datetime, float, float]]:
    """(local time, azimuth, elevation) every 5 minutes while the sun is up."""
    track = []
    t = datetime(day.year, day.month, day.day, 4, 30, tzinfo=NY)
    for _ in range(17 * 60 // STEP_MIN):
        az, el = sun_position(t.astimezone(timezone.utc), lat, lon)
        if el > 1:
            track.append((t, az, el))
        t += timedelta(minutes=STEP_MIN)
    return track


# --- Geometry in local meters around the building ---


def compass(bearing: float) -> str:
    return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][
        int((bearing + 22.5) % 360 // 45)]


def point_in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def point_along(points: list, fraction: float) -> tuple[float, float]:
    """The point a given fraction of the way along a wall's polyline. A wall with a jog
    isn't straight, and its end-to-end chord can run through the building itself."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    target = sum(lengths) * fraction
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if target <= length or length == lengths[-1]:
            t = min(1.0, target / length) if length else 0.0
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        target -= length
    return points[-1]


def ray_distance(ox: float, oy: float, dx: float, dy: float, edges: np.ndarray) -> np.ndarray:
    """Distance along the ray o + t*d to each edge [x1, y1, x2, y2] it crosses (inf if none)."""
    ex, ey = edges[:, 2] - edges[:, 0], edges[:, 3] - edges[:, 1]
    denom = dx * ey - dy * ex
    with np.errstate(divide="ignore", invalid="ignore"):
        t = ((edges[:, 0] - ox) * ey - (edges[:, 1] - oy) * ex) / denom
        u = ((edges[:, 0] - ox) * dy - (edges[:, 1] - oy) * dx) / denom
    hit = (np.abs(denom) > 1e-12) & (t > 0.3) & (u >= 0) & (u <= 1)
    return np.where(hit, t, np.inf)


class Site:
    """One building and its neighbors' footprints, in local meters."""

    def __init__(self, b: nyc.Building):
        self.building = b
        self.lat, self.lon = b.lat, b.lon
        self.kx, self.ky = 111320 * math.cos(math.radians(b.lat)), 110540
        rows = self._footprints()
        self.buildings = []
        self.target = None
        for r in rows:
            if "the_geom" not in r or not r.get("height_roof"):
                continue
            geom = r["the_geom"]
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            rings = [[self.to_local(lon, lat) for lon, lat in poly[0][:-1]] for poly in polys]
            entry = {"bin": r.get("bin"), "bbl": r.get("base_bbl"), "height_ft": float(r["height_roof"]),
                     "ground_m": float(r.get("ground_elevation") or 0) * FT, "rings": rings}
            self.buildings.append(entry)
            if r.get("bin") == str(b.bin):
                self.target = entry
        if self.target is None:
            # Some lots have several buildings or a stale BIN: fall back to the footprint
            # containing the address point.
            self.target = next((x for x in self.buildings for ring in x["rings"] if point_in_ring(0, 0, ring)), None)
        if self.target is None:
            raise nyc.DataSourceError("No building footprint found at this address.")
        self.edges = self._edges()
        self._street_bearing, self._toward_street = self.find_street()

    def to_local(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon) * self.kx, (lat - self.lat) * self.ky)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon + x / self.kx, self.lat + y / self.ky)

    def _footprints(self) -> list[dict]:
        dlat, dlon = SEARCH_RADIUS_M / self.ky, SEARCH_RADIUS_M / self.kx
        lo, la = self.lon, self.lat
        box = (f"POLYGON(({lo - dlon} {la - dlat}, {lo + dlon} {la - dlat}, {lo + dlon} {la + dlat}, "
               f"{lo - dlon} {la + dlat}, {lo - dlon} {la - dlat}))")
        return nyc.soda(nyc.FOOTPRINTS, timeout=40, **{
            "$select": "bin, base_bbl, height_roof, ground_elevation, the_geom",
            "$where": f"intersects(the_geom, '{box}')", "$limit": 20000})

    def _edges(self) -> dict:
        """All footprint edges as arrays: coordinates, ground and roof height, owner index."""
        coords, ground, height, owner = [], [], [], []
        for i, bld in enumerate(self.buildings):
            for ring in bld["rings"]:
                for a, c in zip(ring, ring[1:] + ring[:1]):
                    coords.append([a[0], a[1], c[0], c[1]])
                    ground.append(bld["ground_m"])
                    height.append(bld["height_ft"] * FT)
                    owner.append(i)
        return {"xy": np.array(coords), "ground": np.array(ground), "height": np.array(height), "owner": np.array(owner)}

    # --- Facades ---

    def find_street(self) -> tuple[float | None, tuple[float, float] | None]:
        """(direction the street runs, unit vector from the building toward the street).

        The address point sits near the middle of the lot, so it can't say which wall
        faces the street. Other addresses can: same-side numbers (same parity) give the
        street's direction, and an opposite-side number (other parity) is across the
        street, which tells us which of the parallel walls is the front.
        """
        first = self.building.label.split(",")[0]
        number, _, street = first.partition(" ")
        prefix, _, last = number.rpartition("-")  # Queens: 84-74 -> vary 74
        if not last.isdigit():
            return None, None

        def point(n: int) -> tuple[float, float] | None:
            if n <= 0:
                return None
            try:
                g = nyc.geocode(f"{prefix + '-' if prefix else ''}{n} {street}, {self.building.borough}", need_lot=False)
            except nyc.DataSourceError:
                return None
            return self.to_local(g["lon"], g["lat"])

        bearing = None
        for delta in (10, -10, 20, -20, 4, -4, 40, -40):
            p = point(int(last) + delta)
            if p and math.dist((0, 0), p) >= 15:
                bearing = math.degrees(math.atan2(p[0], p[1])) % 180
                break
        if bearing is None:
            return None, None

        along = (math.sin(math.radians(bearing)), math.cos(math.radians(bearing)))
        for delta in (1, -1, 3, -3, 5, -5, 11, -11):
            p = point(int(last) + delta)
            if not p:
                continue
            dot = p[0] * along[0] + p[1] * along[1]
            perp = (p[0] - dot * along[0], p[1] - dot * along[1])  # remove the along-street offset
            size = math.hypot(*perp)
            if size >= 8:
                return bearing, (perp[0] / size, perp[1] / size)
        return bearing, None

    def facades(self, street_name: str) -> list[dict]:
        """The building's sides: collinear footprint edges merged, each labeled."""
        ring = max(self.target["rings"], key=len)
        target_index = self.buildings.index(self.target)
        sides = []
        for a, c in zip(ring, ring[1:] + ring[:1]):
            length = math.dist(a, c)
            if length < 0.5:
                continue
            nx, ny = (c[1] - a[1]) / length, -(c[0] - a[0]) / length
            mx, my = (a[0] + c[0]) / 2, (a[1] + c[1]) / 2
            if point_in_ring(mx + nx * 0.5, my + ny * 0.5, ring):
                nx, ny = -nx, -ny
            bearing = math.degrees(math.atan2(nx, ny)) % 360
            last = sides[-1] if sides else None
            # Merge into the previous side if it continues in (nearly) the same direction.
            if last and abs((bearing - last["bearing"] + 180) % 360 - 180) < 12:
                last["points"].append(c)
                last["length"] += length
            else:
                sides.append({"points": [a, c], "length": length, "bearing": bearing})
        # The ring wraps around: the last side may continue the first.
        if len(sides) > 1 and abs((sides[0]["bearing"] - sides[-1]["bearing"] + 180) % 360 - 180) < 12:
            sides[0]["points"] = sides[-1]["points"][:-1] + sides[0]["points"]
            sides[0]["length"] += sides[-1]["length"]
            sides.pop()

        others = self.edges["xy"][self.edges["owner"] != target_index]
        own = self.edges["xy"][self.edges["owner"] == target_index]
        for s in sides:
            a, c = s["points"][0], s["points"][-1]
            s["start"], s["end"] = a, c
            nx, ny = math.sin(math.radians(s["bearing"])), math.cos(math.radians(s["bearing"]))
            # How much open space is in front of this side: probes at 3 points on the wall, each
            # looking straight out and 30/60 degrees to either side. A single straight probe called
            # a notch in an L-shaped building "40 m of open space" while its own wing was 7 m away.
            gaps = []
            for f in WINDOW_POSITIONS:
                wx, wy = point_along(s["points"], f)
                px, py = wx + nx * 0.3, wy + ny * 0.3
                for turn in (-60, -30, 0, 30, 60):
                    dx, dy = math.sin(math.radians(s["bearing"] + turn)), math.cos(math.radians(s["bearing"] + turn))
                    gaps.append(min(ray_distance(px, py, dx, dy, others).min(), ray_distance(px, py, dx, dy, own).min()))
            s["open_m"] = float(np.median(gaps))
            s["direction"] = compass(s["bearing"])
            mid = ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2)
            s["distance_to_address_point"] = math.dist(mid, (0, 0))

        usable = [s for s in sides if s["length"] >= 4]  # shorter pieces are jogs in the outline, not window walls
        streets = [s for s in usable if s["open_m"] >= STREET_MIN_OPEN_M]
        if self._street_bearing is not None:
            # A street-side wall runs parallel to the street, so it faces at right angles to it.
            parallel = [s for s in streets
                        if abs((s["bearing"] - self._street_bearing) % 180 - 90) < 30]
            if self._toward_street:
                tx, ty = self._toward_street
                facing = [s for s in parallel
                          if math.sin(math.radians(s["bearing"])) * tx + math.cos(math.radians(s["bearing"])) * ty > 0]
                parallel = facing or parallel
            streets = parallel or streets
            street = max(streets, key=lambda s: (s["open_m"] >= 18, s["length"])) if streets else None
        else:
            street = min(streets, key=lambda s: s["distance_to_address_point"]) if streets else None
        for s in usable:
            opposite_street = street and abs((s["bearing"] - street["bearing"] + 180) % 360 - 180) > 135
            if s["open_m"] <= SHARED_WALL_M:
                s["kind"], s["label"] = "shared_wall", f"{s['direction']} side: shared wall (likely no windows)"
            elif s is street:
                s["kind"], s["label"] = "street", f"street side ({street_name}), facing {s['direction']}"
            elif opposite_street:
                s["kind"], s["label"] = "rear", f"rear, facing {s['direction']} (~{s['open_m']:.0f} m of open space behind)"
            elif s["open_m"] < LIGHT_COURT_M:
                s["kind"], s["label"] = "court", (f"{s['direction']} side: faces a light court or shaft "
                                                  f"(~{s['open_m']:.0f} m to the next wall)")
            else:
                s["kind"], s["label"] = "side", f"side, facing {s['direction']}"
        return usable

    # --- Sun on one window ---

    def sun_minutes(self, side: dict, floor: int, floor_height_ft: float, position: float, height_factor: float,
                    track: list) -> tuple[list[bool], list[int]]:
        """For each time in the track: is the window in direct sun, and which building blocks it (-1 = none)."""
        dx_n, dy_n = math.sin(math.radians(side["bearing"])), math.cos(math.radians(side["bearing"]))
        wx, wy = point_along(side["points"], position)
        ox, oy = wx + dx_n * 0.6, wy + dy_n * 0.6
        window_z = self.target["ground_m"] + ((floor - 1) * floor_height_ft + floor_height_ft / 2) * FT

        tops = self.edges["ground"] + self.edges["height"] * height_factor
        taller = tops > window_z                    # only taller buildings can block
        xy, tops, owner = self.edges["xy"][taller], tops[taller], self.edges["owner"][taller]
        lit, blockers = [], []
        for _, az, el in track:
            if abs((az - side["bearing"] + 180) % 360 - 180) >= 88:  # sun behind this wall
                lit.append(False)
                blockers.append(-2)
                continue
            dx, dy = math.sin(math.radians(az)), math.cos(math.radians(az))
            dist = ray_distance(ox, oy, dx, dy, xy)
            blocked = window_z + dist * math.tan(math.radians(el)) < tops
            if blocked.any():
                lit.append(False)
                blockers.append(int(owner[blocked][np.argmin(dist[blocked])]))
            else:
                lit.append(True)
                blockers.append(-1)
        return lit, blockers


def spans(track: list, lit: list[bool]) -> list[tuple[datetime, datetime]]:
    """Contiguous sunny periods."""
    out, start = [], None
    for (t, _, _), on in zip(track, lit):
        if on and start is None:
            start = t
        if not on and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, track[-1][0] + timedelta(minutes=STEP_MIN)))
    return out


def clock(t: datetime) -> str:
    return t.strftime("%-I:%M%p").lower().replace(":00", "")


def season_dates(today: date) -> dict[str, date]:
    """Today plus the next solstices/equinox (the sun's path repeats every year)."""
    def next_one(month: int, day: int) -> date:
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    return {"today": today, "winter (Dec 21)": next_one(12, 21), "spring (Mar 20)": next_one(3, 20),
            "summer (Jun 21)": next_one(6, 21)}


def ensemble(site: Site, side: dict, floor: int, day: date, runs=None) -> dict:
    """Direct-sun hours for one window and day, as a median with an uncertainty range, plus the
    actual sunny periods of the median run (sun can come in patches between buildings)."""
    track = sun_track(day, site.lat, site.lon)
    runs = runs or list(product(FLOOR_HEIGHTS_FT, WINDOW_POSITIONS, HEIGHT_FACTORS))
    hours, periods_by_run, blocked_by = [], [], {}
    for fh, pos, hf in runs:
        lit, blockers = site.sun_minutes(side, floor, fh, pos, hf, track)
        hours.append(sum(lit) * STEP_MIN / 60)
        periods_by_run.append(spans(track, lit))
        for blk in blockers:
            if blk >= 0:
                blocked_by[blk] = blocked_by.get(blk, 0) + STEP_MIN / len(runs)
    median = float(np.median(hours))
    result = {"hours": round(median, 1), "range": [round(min(hours), 1), round(max(hours), 1)]}
    if median > 0:
        # The run whose total is closest to the median stands in for "typical".
        typical = periods_by_run[min(range(len(runs)), key=lambda i: abs(hours[i] - median))]
        result["periods"] = [f"{clock(a)}-{clock(b)}" for a, b in typical]
    if blocked_by:
        main, minutes = max(blocked_by.items(), key=lambda kv: kv[1])
        result["main_blocker"] = {"index": main, "hours_blocked": round(minutes / 60, 1)}
    return result


def describe(day_result: dict) -> str:
    h, (lo, hi) = day_result["hours"], day_result["range"]
    if hi == 0:
        return "no direct sun"
    if h == 0:
        return f"little or no direct sun (at most {hi:g} h)"
    text = f"about {h:g} h ({lo:g}-{hi:g} h)"
    periods = day_result.get("periods") or []
    if len(periods) == 1:
        text += f", roughly {periods[0]}"
    elif periods:
        text += f", in {len(periods)} stretches: roughly {', '.join(periods)}"
    return text
