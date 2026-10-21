"""
Build the access-point dataset: where an angler actually parks and gets on the
water, for every water body we can recommend (TODO-5).

A fishing spot used to be one coordinate per water body, and drive time was
routed to it. For lakes that was one WDFW shore site picked arbitrarily (Lake
Washington has 28); for most rivers there was no point at all, so the river
could never be recommended. This script gathers every public access point
from official sources and ties each one to the water it serves:

  wdfw_was     WDFW Water Access Sites (boat launches and access areas; parking
               lots, ramps, seasonal vehicle closures). Named by place, not by
               water, so they are matched to water by location (NHD).
  wdfw_shore   WDFW Shore Fishing Sites (lake name + county + a description).
  parks_launch Washington State Parks boat launches (matched by location).
  ecology      WA Ecology public beach access with fishing (the coastal spots;
               each is already an access point).
  curated      Hand-placed spot coordinates already in fishing_spots.
  osm          OpenStreetMap campgrounds, boat ramps, named parking and road
               bridges, for rivers with fewer than 3 official points (read from a
               Geofabrik extract, --osm-gpkg).

Matching uses the USGS National Hydrography Dataset (WA Ecology's service):
for each point, the named lake polygon it sits in or beside and the named
river/stream line within reach. A river water body is identified by the NHD
feature (GNIS_ID) its name resolves to, so two rivers with the same name stay
apart, and duplicate water_bodies rows for one river resolve to one feature.

Read-only against the database. No HERE calls. Network: WDFW, State Parks,
Ecology ArcGIS services only; every response is cached under --cache.
OpenStreetMap comes from a local file.

Output: --out (JSON, loaded by scripts/load_access_points.py) and --report
(Markdown summary for review).

Usage (from backend/, with the app env for DATABASE_URL):
  python -m scripts.build_access_points --cache /tmp/ap_cache \
      --osm-gpkg /tmp/osm/washington.gpkg \
      --out data/access_points.json --report /tmp/access_points_report.md
  (washington.gpkg: unzip https://download.geofabrik.de/north-america/us/washington-latest-free.gpkg.zip)
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_UA = "waflyfish.com access-point builder (contact via site)"
_TIMEOUT = httpx.Timeout(connect=15.0, read=90.0, write=10.0, pool=10.0)

_WDFW_WAS = ("https://geodataservices.wdfw.wa.gov/arcgis/rest/services"
             "/WP_RealEstate/WaterAccessSites/MapServer/1")
_WDFW_SHORE = ("https://geodataservices.wdfw.wa.gov/arcgis/rest/services"
               "/FP_FishMaps/ShoreFishingSites/MapServer/0")
_PARKS_LAUNCH = ("https://services5.arcgis.com/4LKAHwqnBooVDUlX/arcgis/rest/services"
                 "/WAStateParks_BoatLaunches/FeatureServer/0")
_NHD = "https://services.arcgis.com/6lCKYNJLvwTXqrmp/arcgis/rest/services/NHD/FeatureServer"
_NHD_FLOWLINE, _NHD_WATERBODY = 3, 6

# How far a point may sit from the water it serves.
_LAKE_REACH_M = 150     # shore sites sit at the water's edge; parking lots a bit back
_RIVER_REACH_M = 200    # river access areas are often set back behind a levee or field

# NHD FTypes that are lakes/ponds/reservoirs (not swamps, estuaries, ice).
_LAKE_FTYPES = {390, 436}
# Stream/river and artificial path (river centreline through a lake/wide reach).
_FLOW_FTYPES = {460, 558}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_ABBREV = {
    "lk": "lake", "lks": "lakes", "pd": "pond", "pds": "ponds", "res": "reservoir",
    "cr": "creek", "c": "creek", "r": "river", "sl": "slough", "mt": "mount",
    "st": "saint", "ft": "fort", "n": "north", "s": "south", "e": "east", "w": "west",
    "nf": "north fork", "sf": "south fork", "ef": "east fork", "wf": "west fork",
    "mf": "middle fork", "4th": "fourth", "1st": "first", "2nd": "second", "3rd": "third",
    "ltl": "little", "lt": "little", "upr": "upper", "lwr": "lower", "lo": "lower",
}
# Words that say what kind of water it is; dropped for lake identity so
# "Lake Desire", "LK DESIRE (KING)" and "Desire Lake" agree.
_LAKE_KIND = {"lake", "lakes", "pond", "ponds", "reservoir", "the", "of"}
_RIVER_KIND = {"river", "creek", "the", "of"}

# Our names that aren't the water's own name.
_NAME_OVERRIDES = {
    "yakima basin watershed": "Yakima River",
    "snoqualmie corridor": "Snoqualmie River",
    "elwha watershed study": "Elwha River",
    "south fork ahtanum": "South Fork Ahtanum Creek",
    "upper clearwater": "Clearwater River",
    "north fork stillaguamish": "North Fork Stillaguamish River",
    "south fork hoh": "South Fork Hoh River",
    "west teanaway river": "West Fork Teanaway River",
}


# WDFW shore-access sites placed wrong in WDFW's own layer (TODO-5 review,
# 2026-10-21): each one checked against OpenStreetMap and the lake's launch.
_SHORE_FIXES = {
    "10639": (45.626002, -121.128247),   # Spearfish Lake: the layer says 46.6 N (Rimrock Lake)
    "10266": (45.661441, -121.906395),   # Icehouse Lake: 46.6 N (Greenwood Lake)
    "10607": (45.709121, -121.375493),   # Rowland, South: 122.4 W (Clark County)
    "10682": (45.717709, -121.617709),   # Tunnel Lake: 45.6 N (Collins Creek)
    "10088": (46.578210, -122.726590),   # Carlisle Lake: 16 km north; this is its boat launch
}
# Points that serve some other water than the one they match.
_DROP_KEYS = {
    "wdfw_shore:10344",     # "Lake Morton" site that sits on Shady Lake, 12 km north
    "osm:374042226",        # a Weaver Creek bridge in Clark County, not Mason's Weaver Creek
}


def _county_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip().lower() for c in re.split(r"[/,]", raw) if c.strip()]


def clean_name(raw: str) -> str:
    """WDFW stocking names → plain names: 'LK DESIRE (KING)' → 'lake desire',
    'LEWIS R -NF  27.0168' → 'north fork lewis river'."""
    s = raw.lower()
    s = re.sub(r"\)\s+\d{1,2}\s*$", ")", s)          # 'SILVER LK (WHAT) 01'
    s = re.sub(r"\d{2}\.\d{4}", " ", s)              # WRIA stream numbers, even glued on ('R24.0460')
    s = re.sub(r"\bt\d+\b", " ", s)                   # township tags (T24, T28)
    s = re.sub(r"\(\d+\)", " ", s)                    # '(57)'
    s = re.sub(r"\((?:[a-z]{3,4})(?:/[a-z]{3,4})*\)", " ", s)  # county tags (KING), (LINC/SPOK), (SAN)
    s = re.sub(r"#\s*(\d)", r"\1", s)                 # 'PD #1' → 'pd 1'
    fork = re.search(r"-\s*(nf|sf|ef|wf|mf)\b", s)
    s = re.sub(r"-\s*(nf|sf|ef|wf|mf)\b", " ", s)
    s = re.sub(r"[^a-z0-9' ]", " ", s).replace("'", "")
    words = [_ABBREV.get(w, w) for w in s.split()]
    out = " ".join(words)
    if fork:
        out = f"{_ABBREV[fork.group(1)]} {out}"
    return re.sub(r"\s+", " ", out).strip()


def name_variants(raw: str) -> list[str]:
    """'HOG (CANYON) LK' → ['hog lake', 'canyon lake']; otherwise one name."""
    m = re.match(r"^(.*?)\(([^)]*)\)(.*)$", raw)
    if m and not re.fullmatch(r"[A-Za-z]{3,4}(/[A-Za-z]{3,4})*|\d+", m.group(2).strip()):
        a = clean_name(m.group(1) + " " + m.group(3))
        b = clean_name(m.group(2) + " " + m.group(3))
        return [a, b]
    return [clean_name(raw)]


def lake_key(name: str) -> str:
    return " ".join(sorted(w for w in clean_name(name).split() if w not in _LAKE_KIND))


def river_key(name: str) -> str:
    return " ".join(w for w in clean_name(name).split() if w not in _RIVER_KIND)


# ---------------------------------------------------------------------------
# Geometry (local equirectangular metres; fine at the few-hundred-metre scale)
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p = math.radians
    h = (math.sin(p(lat2 - lat1) / 2) ** 2
         + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(p(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _xy(lat0, lon0, lat, lon):
    return ((lon - lon0) * math.cos(math.radians(lat0)) * 111320.0, (lat - lat0) * 110540.0)


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_paths(lat, lon, paths) -> float:
    best = math.inf
    for path in paths:
        pts = [_xy(lat, lon, y, x) for x, y in path]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            best = min(best, _seg_dist(0.0, 0.0, ax, ay, bx, by))
    return best


def inside_rings(lat, lon, rings) -> bool:
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Fetching (cached)
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self, cache: str, concurrency: int = 6):
        self.cache = cache
        os.makedirs(cache, exist_ok=True)
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA})
        self.hits = self.misses = 0

    async def close(self):
        await self.client.aclose()

    async def get_json(self, url: str, params: dict, timeout: float | None = None, post: bool = False) -> dict:
        key = hashlib.sha1((("POST " if post else "") + url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        path = os.path.join(self.cache, key + ".json")
        if os.path.exists(path):
            self.hits += 1
            with open(path) as f:
                return json.load(f)
        self.misses += 1
        async with self.sem:
            for attempt in range(4):
                try:
                    extra = {"timeout": timeout} if timeout else {}
                    r = await (self.client.post(url, data=params, **extra) if post
                               else self.client.get(url, params=params, **extra))
                    r.raise_for_status()
                    data = r.json()
                    if "error" in data:
                        raise RuntimeError(f"ArcGIS error: {data['error']}")
                    break
                except (httpx.HTTPError, RuntimeError):
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))
        with open(path, "w") as f:
            json.dump(data, f)
        return data

    async def all_features(self, layer_url: str) -> list[dict]:
        feats, offset = [], 0
        while True:
            data = await self.get_json(layer_url + "/query", {
                "where": "1=1", "outFields": "*", "outSR": "4326", "f": "json",
                "resultOffset": offset, "resultRecordCount": 1000,
            })
            feats.extend(data.get("features", []))
            if not data.get("exceededTransferLimit"):
                return feats
            offset += 1000

    async def nhd_near(self, layer: int, lat: float, lon: float, reach_m: int) -> list[dict]:
        fields = ("GNIS_ID,GNIS_Name,FType,Permanent_Identifier"
                  + (",AreaSqKm" if layer == _NHD_WATERBODY else ""))
        data = await self.get_json(f"{_NHD}/{layer}/query", {
            "geometry": f"{lon:.6f},{lat:.6f}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "distance": str(reach_m), "units": "esriSRUnit_Meter",
            "outFields": fields, "returnGeometry": "true", "outSR": "4326",
            "maxAllowableOffset": "0.00005", "geometryPrecision": "6", "f": "json",
        })
        return data.get("features", [])

    async def nhd_by_name(self, layer: int, gnis_name: str) -> list[dict]:
        data = await self.get_json(f"{_NHD}/{layer}/query", {
            "where": f"GNIS_Name = '{gnis_name.replace(chr(39), chr(39) * 2)}'",
            "outFields": "GNIS_ID,GNIS_Name,FType,LengthKm" if layer == _NHD_FLOWLINE
                         else "GNIS_ID,GNIS_Name,FType,AreaSqKm,Permanent_Identifier",
            "returnGeometry": "true", "outSR": "4326", "maxAllowableOffset": "0.0003",
            "geometryPrecision": "5", "f": "json", "resultRecordCount": 2000,
        })
        return data.get("features", [])


# ---------------------------------------------------------------------------
# Access points
# ---------------------------------------------------------------------------

@dataclass
class AccessPoint:
    source: str
    source_id: str
    name: str
    kind: str                 # boat_ramp | hand_launch | shore | pier | access_area | beach | access
    lat: float
    lon: float
    county: str | None = None
    water_name: str | None = None     # name the source gives the water (shore sites)
    description: str | None = None
    parking: str | None = None
    facilities: list[str] = field(default_factory=list)
    seasonal: str | None = None       # gate / vehicle-access closure text
    pass_required: str | None = None
    managing_entity: str | None = None
    # Where the local roads to this point leave the highway network, and the drive
    # from there (rivers only; see road_gateways).
    gateway_lat: float | None = None
    gateway_lon: float | None = None
    gateway_minutes: float | None = None
    # NHD enrichment
    lake: dict | None = None          # {gnis_id, name, perm_id, dist_m, area_km2}
    streams: list[dict] = field(default_factory=list)  # [{gnis_id, name, dist_m}] nearest first


def _kind_from_text(text: str) -> str:
    t = (text or "").lower()
    if "ramp" in t or "launch" in t:
        return "boat_ramp"
    if "pier" in t or "dock" in t or "platform" in t:
        return "pier"
    return "shore"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def from_wdfw_was(f: dict) -> AccessPoint:
    a, g = f["attributes"], f["geometry"]
    ramps, hand = a.get("BoatRamps") or 0, a.get("HandLaunches") or 0
    kind = "boat_ramp" if ramps else ("hand_launch" if hand else "access_area")
    fac = []
    if ramps:
        surf = a.get("BoatRampSurfaceTypes")
        fac.append(_plural(ramps, "boat ramp") + (f" ({surf.lower()})" if surf and surf != "N/A" else ""))
    if hand:
        fac.append(_plural(hand, "hand launch"))
    if a.get("FishingPlatforms"):
        fac.append(_plural(a["FishingPlatforms"], "fishing platform"))
    if a.get("Restrooms"):
        fac.append("restroom")
    if a.get("CampingAllowed") == "Yes":
        fac.append("camping allowed")
    lots = a.get("ParkingLots")
    seasonal = None
    if a.get("ClosureType") and a["ClosureType"] != "No closure":
        seasonal = a.get("OpenDates") or a["ClosureType"]
    notes = a.get("Notes")
    if notes:
        seasonal = f"{seasonal} {notes}".strip() if seasonal else None
    entity = a.get("ManagingEntity")
    return AccessPoint(
        source="wdfw_was", source_id=str(a["ManagedPlaceOID"]),
        name=a["WaterAccessSiteName"].strip(), kind=kind,
        lat=round(g["y"], 6), lon=round(g["x"], 6),
        county=(a.get("County") or "").strip() or None,
        description=notes if notes and not seasonal else None,
        parking=(_plural(lots, "parking lot") if lots else "no developed parking"),
        facilities=fac, seasonal=seasonal,
        pass_required=("Discover Pass or WDFW Vehicle Access Pass" if entity == "WDFW" else None),
        managing_entity=entity,
    )


def from_wdfw_shore(f: dict) -> AccessPoint:
    a = f["attributes"]
    desc = (a.get("Description") or "").strip() or None
    sid = str(int(a["AccessSiteID"]))
    lat, lon = _SHORE_FIXES.get(sid) or (round(a["Latitude"], 6), round(a["Longitude"], 6))
    return AccessPoint(
        source="wdfw_shore", source_id=sid,
        name=(a.get("LakeName") or "").strip(), kind=_kind_from_text(desc or ""),
        lat=lat, lon=lon,
        county=(a.get("County") or "").strip() or None,
        water_name=(a.get("LakeName") or "").strip() or None,
        description=desc,
        pass_required="Discover Pass or WDFW Vehicle Access Pass",
        managing_entity="WDFW",
    )


def from_parks(f: dict) -> AccessPoint:
    a, g = f["attributes"], f["geometry"]
    ident = (a.get("Identifier") or "").strip()
    return AccessPoint(
        source="parks_launch", source_id=str(a["OBJECTID"]),
        name=re.sub(r"\s*-\s*launch area$", "", ident, flags=re.I) or "State park boat launch",
        kind="boat_ramp", lat=round(g["y"], 6), lon=round(g["x"], 6),
        facilities=["boat ramp"], pass_required="Discover Pass",
        managing_entity="Washington State Parks",
    )


async def enrich(fetch: Fetcher, ap: AccessPoint) -> None:
    lakes, flows = await asyncio.gather(
        fetch.nhd_near(_NHD_WATERBODY, ap.lat, ap.lon, _LAKE_REACH_M),
        fetch.nhd_near(_NHD_FLOWLINE, ap.lat, ap.lon, _RIVER_REACH_M),
    )
    best = None
    for f in lakes:
        a = f["attributes"]
        if a.get("FType") not in _LAKE_FTYPES or not f.get("geometry"):
            continue
        rings = f["geometry"].get("rings") or []
        d = 0.0 if inside_rings(ap.lat, ap.lon, rings) else dist_to_paths(ap.lat, ap.lon, rings)
        cand = {"gnis_id": a.get("GNIS_ID"), "name": a.get("GNIS_Name"),
                "perm_id": a.get("Permanent_Identifier"), "dist_m": round(d),
                "area_km2": a.get("AreaSqKm")}
        # Named lakes first, then nearest.
        rank = (cand["name"] is None, d)
        if best is None or rank < best[0]:
            best = (rank, cand)
    ap.lake = best[1] if best else None
    streams: dict[str, dict] = {}
    for f in flows:
        a = f["attributes"]
        if a.get("FType") not in _FLOW_FTYPES or not a.get("GNIS_Name") or not f.get("geometry"):
            continue
        d = dist_to_paths(ap.lat, ap.lon, f["geometry"].get("paths") or [])
        gid = a.get("GNIS_ID") or a["GNIS_Name"]
        if gid not in streams or d < streams[gid]["dist_m"]:
            streams[gid] = {"gnis_id": a.get("GNIS_ID"), "name": a["GNIS_Name"], "dist_m": round(d)}
    ap.streams = sorted(streams.values(), key=lambda s: s["dist_m"])


# ---------------------------------------------------------------------------
# Water bodies
# ---------------------------------------------------------------------------

def load_water_bodies() -> list[dict]:
    import psycopg2
    import psycopg2.extras

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    with psycopg2.connect(url) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("""
            SELECT w.id::text, w.name, w.type, w.county, w.source, w.aliases,
                   w.latitude::float AS lat, w.longitude::float AS lon,
                   w.fly_fishing_legal, w.last_stocked_date,
                   (SELECT count(*) FROM stocking_events e WHERE e.water_body_id = w.id) AS stocking_events,
                   s.id::text AS spot_id, s.name AS spot_name, s.source AS spot_source,
                   s.latitude::float AS spot_lat, s.longitude::float AS spot_lon
            FROM water_bodies w LEFT JOIN fishing_spots s ON s.water_body_id = w.id
            ORDER BY w.name
        """)
        return [dict(r) for r in cur.fetchall()]



# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class Match:
    wb_id: str
    ap_index: int
    method: str
    confidence: str     # high | medium | low
    note: str | None = None


def is_lakeish(wb: dict) -> bool:
    """Lakes, and stocking rows typed 'river' that are really lakes/ponds."""
    if wb["type"] == "lake":
        return True
    words = set(clean_name(wb["name"]).split())
    return wb["type"] in ("river", "creek") and bool(words & {"lake", "lakes", "pond", "ponds", "reservoir"}) \
        and not words & {"river", "creek"}


def _river_display(wb: dict) -> str:
    return _NAME_OVERRIDES.get(wb["name"].lower().strip()) or clean_name(wb["name"]).title()


async def resolve_river_features(fetch: Fetcher, rivers: list[dict]) -> dict[str, dict]:
    """water body id → the NHD river feature its name resolves to. A name can belong
    to several features (13 Jones Creeks); pick the one passing nearest the water
    body's own point, else the longest, and say which."""
    out = {}
    for wb in rivers:
        target = _river_display(wb)
        feats = await fetch.nhd_by_name(_NHD_FLOWLINE, target)
        by_id: dict[str, dict] = {}
        for f in feats:
            a = f["attributes"]
            if a.get("FType") not in _FLOW_FTYPES:
                continue
            g = by_id.setdefault(a["GNIS_ID"], {"gnis_id": a["GNIS_ID"], "name": a["GNIS_Name"],
                                                "length_km": 0.0, "paths": []})
            g["length_km"] += a.get("LengthKm") or 0.0
            g["paths"].extend((f.get("geometry") or {}).get("paths") or [])
        if not by_id:
            out[wb["id"]] = {"gnis_id": None, "gnis_name": target, "candidates": 0,
                             "how": "no NHD feature by that name", "paths": []}
            continue
        ref = (wb["spot_lat"], wb["spot_lon"]) if wb.get("spot_lat") else (
            (wb["lat"], wb["lon"]) if wb.get("lat") else None)
        if ref:
            best = min(by_id.values(), key=lambda g: dist_to_paths(ref[0], ref[1], g["paths"]))
            how = f"nearest of {len(by_id)} to the spot" if len(by_id) > 1 else "only feature"
            d = dist_to_paths(ref[0], ref[1], best["paths"])
            if d > 3000:
                how += f" (spot is {d / 1000:.1f} km from it)"
        else:
            best = max(by_id.values(), key=lambda g: g["length_km"])
            how = (f"longest of {len(by_id)} ({best['length_km']:.0f} km)" if len(by_id) > 1
                   else "only feature")
        out[wb["id"]] = {"gnis_id": best["gnis_id"], "gnis_name": best["name"], "candidates": len(by_id),
                         "how": how, "length_km": round(best["length_km"]), "paths": best["paths"]}
    return out


def lake_identities(aps: list[AccessPoint], wbs: list[dict]) -> dict[str, str]:
    """wb id → NHD lake Permanent_Identifier, from the access point at its spot."""
    out = {}
    for w in wbs:
        if not w.get("spot_lat") or not is_lakeish(w):
            continue
        best = None
        for ap in aps:
            if ap.lake and ap.lake["perm_id"] and abs(ap.lat - w["spot_lat"]) < 0.0005 \
                    and abs(ap.lon - w["spot_lon"]) < 0.0005:
                d = haversine_m(ap.lat, ap.lon, w["spot_lat"], w["spot_lon"])
                if d < 30 and (best is None or d < best[0]):
                    best = (d, ap.lake["perm_id"])
        if best:
            out[w["id"]] = best[1]
    return out


def match_all(aps: list[AccessPoint], wbs: list[dict], river_feat: dict[str, dict],
              lake_perm: dict[str, str]) -> list[Match]:
    matches: list[Match] = []
    lake_wbs = [w for w in wbs if is_lakeish(w)]
    by_lake_key: dict[tuple, list[dict]] = defaultdict(list)
    strict_names: dict[str, set] = {}
    for w in lake_wbs:
        names = set(name_variants(w["name"]) + [clean_name(a) for a in (w["aliases"] or [])])
        strict_names[w["id"]] = names
        for n in names:
            for c in (_county_list(w["county"]) or [""]):
                by_lake_key[(lake_key(n), c)].append(w)
    river_by_gnis: dict[str, list[dict]] = defaultdict(list)
    for w in wbs:
        rf = river_feat.get(w["id"])
        if rf and rf["gnis_id"] and not is_lakeish(w):
            river_by_gnis[rf["gnis_id"]].append(w)

    def lake_wbs_for(name: str, county: str | None, ap: AccessPoint) -> list[dict]:
        if not county:
            return []
        cands = list({w["id"]: w for w in by_lake_key.get((lake_key(name), county.lower()), [])}.values())
        return _disambiguate(cands, name, ap)

    def _disambiguate(cands: list[dict], name: str, ap: AccessPoint) -> list[dict]:
        known = [w for w in cands if w["id"] in lake_perm]
        spotted = [w for w in cands if w.get("spot_id")]
        if len({lake_perm[w["id"]] for w in known}) <= 1 and len(spotted) <= 1:
            return cands   # one lake (its duplicate rows included)
        # Several distinct lakes share the name key (Conconully Lake / Conconully
        # Reservoir): decide by the NHD polygon, then by the exact name.
        ap_perm = ap.lake["perm_id"] if ap.lake else None
        keep = [w for w in known if lake_perm[w["id"]] == ap_perm]
        target = {clean_name(name)}
        if not keep:
            keep = [w for w in known if strict_names[w["id"]] & target]
        names = target | set().union(*(strict_names[w["id"]] for w in keep)) if keep else target
        keep += [w for w in cands if w["id"] not in lake_perm and strict_names[w["id"]] & names]
        return keep

    for i, ap in enumerate(aps):
        if ap.source in ("ecology", "curated", "osm"):
            continue  # matched when created
        hit = False
        # 1. Shore sites name their lake and county.
        if ap.source == "wdfw_shore":
            for w in lake_wbs_for(ap.water_name or "", ap.county, ap):
                conf, note = "high", None
                if ap.lake and ap.lake["name"] and not any(
                        lake_key(v) == lake_key(ap.lake["name"]) for v in strict_names[w["id"]]):
                    conf, note = "medium", f"NHD names the lake here {ap.lake['name']}"
                matches.append(Match(w["id"], i, "name+county", conf, note))
                hit = True
            if hit:
                continue
        # 2. Point in/beside a named NHD lake.
        if ap.lake and ap.lake["name"] and ap.lake["dist_m"] <= _LAKE_REACH_M:
            if ap.county:
                cands = lake_wbs_for(ap.lake["name"], ap.county, ap)
            else:
                # No county on the point (State Parks): a same-named lake whose spot
                # is on the same NHD polygon, or within 3 km.
                cands = _disambiguate(
                    [w for w in lake_wbs
                     if any(lake_key(v) == lake_key(ap.lake["name"]) for v in strict_names[w["id"]])
                     and ((w["id"] in lake_perm and lake_perm[w["id"]] == ap.lake["perm_id"])
                          or (w.get("spot_lat") and haversine_m(ap.lat, ap.lon, w["spot_lat"], w["spot_lon"]) < 3000))],
                    ap.lake["name"], ap)
            for w in cands:
                matches.append(Match(w["id"], i, "nhd_lake", "high" if ap.lake["dist_m"] <= 50 else "medium",
                                     f"{ap.lake['name']} ({ap.lake['dist_m']} m)"))
                hit = True
            if hit:
                continue
        # 3. Beside a named NHD river/stream that is one of ours.
        for s in ap.streams:
            if s["gnis_id"] in river_by_gnis:
                # A different named stream much closer means the point serves that
                # one (a tributary mouth): low confidence.
                nearer = [o for o in ap.streams if o["dist_m"] < s["dist_m"] - 75]
                conf = "high" if s["dist_m"] <= 100 and not nearer else "medium"
                note = f"{s['name']} ({s['dist_m']} m)"
                if nearer:
                    note += f"; nearer: {nearer[0]['name']} ({nearer[0]['dist_m']} m)"
                    conf = "low"
                for w in river_by_gnis[s["gnis_id"]]:
                    matches.append(Match(w["id"], i, "nhd_stream", conf, note))
                break
    return matches


# ---------------------------------------------------------------------------
# Duplicates, new lakes, activation, names
# ---------------------------------------------------------------------------

def find_merges(wbs, matches, river_feat) -> tuple[dict[str, str], list[list[str]]]:
    """Rows that are the same water: they share an access point (lakes) or an NHD
    river feature (rivers). Returns ({duplicate id → kept id}, conflict groups).
    The kept row is the one with a spot; with none, the one with most stockings."""
    parent = {w["id"]: w["id"] for w in wbs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_ap = defaultdict(set)
    for m in matches:
        by_ap[m.ap_index].add(m.wb_id)
    for ids in by_ap.values():
        ids = list(ids)
        for x in ids[1:]:
            union(ids[0], x)
    by_gnis = defaultdict(list)
    for w in wbs:
        rf = river_feat.get(w["id"])
        if rf and rf.get("gnis_id") and not is_lakeish(w):
            by_gnis[rf["gnis_id"]].append(w["id"])
    for ids in by_gnis.values():
        for x in ids[1:]:
            union(ids[0], x)

    wb_by_id = {w["id"]: w for w in wbs}
    groups = defaultdict(list)
    for w in wbs:
        groups[find(w["id"])].append(w["id"])
    merges, conflicts = {}, []
    spot_rank = {"wdfw_access": 0, "wa_ecology": 0, "curated": 1, None: 2}
    for ids in groups.values():
        if len(ids) < 2:
            continue
        spotted = [x for x in ids if wb_by_id[x]["spot_id"]]
        if len(spotted) > 2 or (len(spotted) == 2 and len({wb_by_id[x]["type"] for x in spotted}) > 1):
            conflicts.append(ids)
            continue
        if spotted:
            # Two rows with spots for one water (McCabe Pond): keep the one whose
            # spot came from an access source; the other's spot is dropped.
            keep = min(spotted, key=lambda x: (spot_rank.get(wb_by_id[x]["spot_source"], 2), x))
        else:
            keep = max(ids, key=lambda x: (wb_by_id[x]["stocking_events"], x))
        for x in ids:
            if x != keep:
                merges[x] = keep
    return merges, conflicts


_STOCKING_STYLE = re.compile(r"^[A-Z0-9 .()#/'&-]+$")


def proper_name(w: dict, matched_aps: list[AccessPoint], river_feat: dict) -> str | None:
    """A better display name, or None to keep the current one. Stocking-style names
    ('LK DESIRE (KING)', 'WYNOOCHEE R  22.0260') and the DNR area names get the
    water's own name; the old name stays as an alias."""
    override = _NAME_OVERRIDES.get(w["name"].lower().strip())
    if override and override != w["name"]:
        return override
    if not _STOCKING_STYLE.match(w["name"]):
        return None
    shore = Counter(ap.water_name for ap in matched_aps if ap.source == "wdfw_shore" and ap.water_name)
    if shore:
        return shore.most_common(1)[0][0]
    rf = river_feat.get(w["id"])
    if rf and rf.get("gnis_id") and not is_lakeish(w):
        return rf["gnis_name"]
    lake = Counter(ap.lake["name"] for ap in matched_aps if ap.lake and ap.lake["name"])
    if lake:
        return lake.most_common(1)[0][0]
    return name_variants(w["name"])[0].title()


def activation(w: dict, n_access: int) -> tuple[bool, str]:
    """Should a water body without a spot become recommendable? Proposed default;
    the reviewed JSON is what the loader applies."""
    if n_access == 0:
        return False, "no access point"
    if w["source"] == "dnr" and re.search(r"state forest|sportsmen|road stream|camp", w["name"], re.I):
        return False, "DNR recreation-area name, not a fishery"
    if w["source"] == "dnr" and w["type"] == "creek":
        return False, "DNR recreation-page creek, review"
    if w["source"] == "wdfw_stocking" and w["type"] in ("river", "creek") and not is_lakeish(w) \
            and "river" not in clean_name(w["name"]).split():
        return False, "stocked creek (usually a juvenile salmon release), review"
    return True, "has public access"


# ---------------------------------------------------------------------------
# OpenStreetMap (rivers with little official access)
# ---------------------------------------------------------------------------
# Read from a Geofabrik extract (washington-latest-free.gpkg), not Overpass: on
# 2026-10-19 one overpass-api.de backend answered every query with zero elements
# and the others timed out or 502'd. The extract is also reproducible.
# Geofabrik's "free" layers drop the access tag, so only kinds that are public by
# nature are taken: campgrounds, boat ramps, bridges on public roads, and parking
# whose name says what it's for.

_OSM_MIN_OFFICIAL = 3        # rivers with fewer official access points get an OSM pass
_OSM_ROAD_BRIDGE = {"primary", "secondary", "tertiary", "unclassified", "residential",
                    "primary_link", "secondary_link", "tertiary_link"}
_OSM_PARKING_NAME = re.compile(r"\b(access|fishing|river|creek|boat|launch|trail|trailhead|park|campground|recreation)\b", re.I)
_OSM_PARKING_NOT = re.compile(r"\b(employee|employees|staff|customer|customers|resident|residents|tenant|permit|private|visitor center)\b", re.I)
_OSM_PRIORITY = {"campground": 0, "boat_ramp": 0, "parking": 1, "bridge": 2}


def _bbox(paths: list) -> tuple | None:
    lats = [y for p in paths for x, y in p]
    lons = [x for p in paths for x, y in p]
    if not lats:
        return None
    return (min(lats) - 0.01, min(lons) - 0.01, max(lats) + 0.01, max(lons) + 0.01)


def _gpkg_coords(blob: bytes) -> list[tuple[float, float]]:
    """(lon, lat) vertices of a GeoPackage geometry: point, line or polygon (any multi)."""
    import struct
    flags = blob[3]
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    wkb = memoryview(blob)[8 + env:]
    out: list[tuple[float, float]] = []

    def read(off: int) -> int:
        bo = "<" if wkb[off] == 1 else ">"
        gtype = struct.unpack_from(bo + "I", wkb, off + 1)[0] % 1000
        off += 5
        if gtype == 1:
            out.append(struct.unpack_from(bo + "dd", wkb, off))
            return off + 16
        if gtype == 2:
            n = struct.unpack_from(bo + "I", wkb, off)[0]
            out.extend(struct.unpack_from(bo + "dd", wkb, off + 4 + 16 * i) for i in range(n))
            return off + 4 + 16 * n
        if gtype == 3:
            rings = struct.unpack_from(bo + "I", wkb, off)[0]
            off += 4
            for _ in range(rings):
                n = struct.unpack_from(bo + "I", wkb, off)[0]
                out.extend(struct.unpack_from(bo + "dd", wkb, off + 4 + 16 * i) for i in range(n))
                off += 4 + 16 * n
            return off
        if gtype in (4, 5, 6, 7):
            parts = struct.unpack_from(bo + "I", wkb, off)[0]
            off += 4
            for _ in range(parts):
                off = read(off)
            return off
        raise ValueError(f"WKB type {gtype}")

    read(0)
    return out


_DRIVABLE = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
             "living_street", "service", "track", "track_grade1", "track_grade2", "track_grade3",
             "track_grade4", "track_grade5", "motorway_link", "trunk_link", "primary_link",
             "secondary_link", "tertiary_link", "unknown")
_GRID = 0.005   # ~500 m cells


def load_drivable_grid(gpkg: str, boxes: list[tuple]) -> set:
    """Grid cells holding a drivable-road vertex, inside the given boxes. A camp
    with no road within ~300 m is backcountry (Enchanted Valley), not a drive."""
    import sqlite3
    db = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    cells = set()
    sql = "SELECT geom FROM gis_osm_roads_free WHERE fclass IN (" + ",".join(f"'{c}'" for c in _DRIVABLE) + ")"
    for (geom,) in db.execute(sql):
        pts = _gpkg_coords(geom)
        lon, lat = pts[0]
        if not any(b[0] - 0.05 <= lat <= b[2] + 0.05 and b[1] - 0.05 <= lon <= b[3] + 0.05 for b in boxes):
            continue
        for lon, lat in pts:
            cells.add((int(lat // _GRID), int(lon // _GRID)))
    db.close()
    return cells


def near_road(cells: set, lat: float, lon: float) -> bool:
    i, j = int(lat // _GRID), int(lon // _GRID)
    return any((i + di, j + dj) in cells for di in (-1, 0, 1) for dj in (-1, 0, 1))


_SPEED_KMH = {"motorway": 100, "trunk": 90, "primary": 80, "secondary": 70, "tertiary": 60,
              "motorway_link": 60, "trunk_link": 50, "primary_link": 50, "secondary_link": 45,
              "tertiary_link": 40, "unclassified": 45, "residential": 35, "living_street": 15,
              "service": 20, "track": 20, "unknown": 25}
_MAJOR = {"motorway", "trunk", "primary", "secondary"}


def road_gateways(gpkg: str, points: list[AccessPoint], margin_deg: float = 0.25) -> int:
    """For each point, the highway junction its local roads lead to and the drive
    time from there, so the context builder can estimate drive time as
    "straight line to the gateway + this" instead of straight line to the point.
    Straight line alone picks the wrong end of a mountain valley: from Seattle the
    westernmost Icicle Creek point is Blackpine Horse Camp, 18 miles up a dead-end
    road from Leavenworth. Multi-source Dijkstra from every primary-or-bigger road
    vertex over the drivable roads around the points. Returns how many got one."""
    import heapq
    import sqlite3

    if not points:
        return 0
    lat0, lat1 = min(p.lat for p in points) - margin_deg, max(p.lat for p in points) + margin_deg
    lon0, lon1 = min(p.lon for p in points) - margin_deg, max(p.lon for p in points) + margin_deg
    cells_needed = {(int(p.lat // 0.25), int(p.lon // 0.25)) for p in points}

    def wanted(lat, lon):
        i, j = int(lat // 0.25), int(lon // 0.25)
        return any((i + di, j + dj) in cells_needed for di in (-1, 0, 1) for dj in (-1, 0, 1))

    db = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    adj: dict[tuple, list] = defaultdict(list)
    major: set = set()
    # No service roads: ~600k driveways and parking aisles; a campground loop is
    # covered by the last stretch (point to nearest road vertex, up to 1 km).
    sql = ("SELECT fclass, geom FROM gis_osm_roads_free WHERE fclass IN ("
           + ",".join(f"'{c}'" for c in _DRIVABLE if c != "service") + ")")
    for fclass, geom in db.execute(sql):
        pts = _gpkg_coords(geom)
        lon, lat = pts[0]
        if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1) or not wanted(lat, lon):
            continue
        kmh = _SPEED_KMH.get(fclass, _SPEED_KMH["track"] if fclass.startswith("track") else 25)
        nodes = [(round(y, 6), round(x, 6)) for x, y in pts]
        for a, b in zip(nodes, nodes[1:]):
            minutes = haversine_m(a[0], a[1], b[0], b[1]) / 1000 / kmh * 60
            adj[a].append((b, minutes))
            adj[b].append((a, minutes))
        if fclass in _MAJOR:
            major.update(nodes)
    db.close()

    best: dict[tuple, tuple] = {}
    heap = [(0.0, n, n) for n in major]
    heapq.heapify(heap)
    while heap:
        d, n, g = heapq.heappop(heap)
        if n in best:
            continue
        best[n] = (d, g)
        for m, w in adj[n]:
            if m not in best:
                heapq.heappush(heap, (d + w, m, g))

    grid: dict[tuple, list] = defaultdict(list)
    for n in best:
        grid[(int(n[0] // _GRID), int(n[1] // _GRID))].append(n)
    done = 0
    for p in points:
        i, j = int(p.lat // _GRID), int(p.lon // _GRID)
        near = [n for di in (-2, -1, 0, 1, 2) for dj in (-2, -1, 0, 1, 2) for n in grid.get((i + di, j + dj), [])]
        if not near:
            continue
        n = min(near, key=lambda n: haversine_m(p.lat, p.lon, n[0], n[1]))
        walk = haversine_m(p.lat, p.lon, n[0], n[1])
        if walk > 1000:
            continue
        d, g = best[n]
        p.gateway_lat, p.gateway_lon = g
        p.gateway_minutes = round(d + walk / 1000 / 20 * 60, 1)   # last stretch at track speed
        done += 1
    return done


def load_osm_candidates(gpkg: str) -> list[dict]:
    """Every OSM feature that could be river access, with its vertices."""
    import sqlite3
    db = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    queries = [
        ("campground", "SELECT osm_id, name, geom FROM gis_osm_pois_free WHERE fclass = 'camp_site'"),
        ("campground", "SELECT osm_id, name, geom FROM gis_osm_pois_a_free WHERE fclass = 'camp_site'"),
        ("boat_ramp", "SELECT osm_id, name, geom FROM gis_osm_traffic_free WHERE fclass = 'slipway'"),
        ("boat_ramp", "SELECT osm_id, name, geom FROM gis_osm_traffic_a_free WHERE fclass = 'slipway'"),
        ("parking", "SELECT osm_id, name, geom FROM gis_osm_traffic_free WHERE fclass = 'parking' AND name IS NOT NULL"),
        ("parking", "SELECT osm_id, name, geom FROM gis_osm_traffic_a_free WHERE fclass = 'parking' AND name IS NOT NULL"),
        ("bridge", "SELECT osm_id, name, geom FROM gis_osm_roads_free WHERE bridge = 'T' AND fclass IN ("
                   + ",".join(f"'{c}'" for c in sorted(_OSM_ROAD_BRIDGE)) + ")"),
    ]
    out = []
    for kind, sql in queries:
        for osm_id, name, geom in db.execute(sql):
            if kind == "parking" and (not _OSM_PARKING_NAME.search(name or "") or _OSM_PARKING_NOT.search(name)):
                continue
            pts = _gpkg_coords(geom)
            if pts:
                out.append({"kind": kind, "osm_id": str(osm_id), "name": name, "pts": pts})
    db.close()
    return out


def osm_points_for_rivers(candidates: list[dict], rivers: dict[str, dict]) -> dict[str, list[AccessPoint]]:
    """{wb id: {"name", "paths"}} → {wb id: OSM access points}. Each feature goes to
    the nearest of our river lines, within 250 m (40 m for a bridge, measured from
    its nearest vertex); a polygon is placed at its vertex nearest the river."""
    boxes = {wid: _bbox(r["paths"]) for wid, r in rivers.items() if r["paths"]}
    found: dict[str, list[AccessPoint]] = defaultdict(list)
    for c in candidates:
        lons = [x for x, y in c["pts"]]
        lats = [y for x, y in c["pts"]]
        best = None
        for wid, b in boxes.items():
            if max(lats) < b[0] or min(lats) > b[2] or max(lons) < b[1] or min(lons) > b[3]:
                continue
            for lon, lat in c["pts"][:: max(1, len(c["pts"]) // 40)]:
                d = dist_to_paths(lat, lon, rivers[wid]["paths"])
                if best is None or d < best[0]:
                    best = (d, wid, lat, lon)
        if best is None or best[0] > (40 if c["kind"] == "bridge" else 250):
            continue
        d, wid, lat, lon = best
        if c["kind"] in ("campground", "parking"):
            # Point at the feature's middle, not the vertex on the bank.
            lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
        label = c["name"] or {"bridge": "Road bridge", "boat_ramp": "Boat ramp"}.get(c["kind"], c["kind"].title())
        if c["kind"] == "bridge" and c["name"]:
            label = f"{c['name']} bridge"
        found[wid].append(AccessPoint(
            source="osm", source_id=c["osm_id"], name=label, kind=c["kind"],
            lat=round(lat, 6), lon=round(lon, 6), description=f"OSM {c['kind'].replace('_', ' ')}",
            streams=[{"gnis_id": None, "name": rivers[wid]["name"], "dist_m": round(d)}]))
    out = {}
    for wid, pts in found.items():
        # One point per ~300 m: the best kind wins.
        pts.sort(key=lambda a: (_OSM_PRIORITY[a.kind], a.streams[0]["dist_m"]))
        kept: list[AccessPoint] = []
        for a in pts:
            if all(haversine_m(a.lat, a.lon, k.lat, k.lon) > 300 for k in kept):
                kept.append(a)
        out[wid] = kept
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def build(args) -> None:
    fetch = Fetcher(args.cache)
    try:
        was, shore, parks = await asyncio.gather(
            fetch.all_features(_WDFW_WAS), fetch.all_features(_WDFW_SHORE),
            fetch.all_features(_PARKS_LAUNCH))
        aps = ([from_wdfw_was(f) for f in was] + [from_wdfw_shore(f) for f in shore]
               + [from_parks(f) for f in parks if (f["attributes"].get("HideFeature") or "").lower() != "yes"])
        print(f"sources: wdfw_was={len(was)} wdfw_shore={len(shore)} parks_launch={len(parks)}", flush=True)

        wbs = load_water_bodies()
        print(f"water bodies: {len(wbs)}", flush=True)

        # Existing spots become access points: coastal spots are Ecology beach
        # access points; hand-placed river/lake spots are kept as 'curated'.
        # WDFW-access lake spots are already one of the shore sites.
        pre: list[Match] = []
        for w in wbs:
            if not w["spot_id"] or w["spot_source"] == "wdfw_access":
                continue
            src = "ecology" if w["spot_source"] == "wa_ecology" else "curated"
            aps.append(AccessPoint(
                source=src, source_id=w["spot_id"], name=(w["spot_name"] or w["name"]),
                kind="beach" if src == "ecology" else "access",
                lat=round(w["spot_lat"], 6), lon=round(w["spot_lon"], 6), county=w["county"]))
            pre.append(Match(w["id"], len(aps) - 1, "existing_spot", "high" if src == "ecology" else "medium",
                             None if src == "ecology" else "hand-placed spot, unverified"))

        todo = [ap for ap in aps if ap.source != "ecology"]
        for k in range(0, len(todo), 50):
            await asyncio.gather(*(enrich(fetch, ap) for ap in todo[k:k + 50]))
        print(f"  nhd enriched {len(todo)} (cache hits {fetch.hits}, fetched {fetch.misses})", flush=True)

        rivers = [w for w in wbs if w["type"] in ("river", "creek") and not is_lakeish(w)]
        river_feat = await resolve_river_features(fetch, rivers)
        lake_perm = lake_identities(aps, wbs)
        matches = pre + match_all(aps, wbs, river_feat, lake_perm)
        merges, conflicts = find_merges(wbs, matches, river_feat)

        # OSM for kept rivers with little official access.
        official = Counter(m.wb_id for m in matches if aps[m.ap_index].source in ("wdfw_was", "parks_launch", "wdfw_shore"))
        targets = {w["id"]: {"name": river_feat[w["id"]]["gnis_name"], "paths": river_feat[w["id"]]["paths"]}
                   for w in rivers
                   if w["id"] not in merges and (river_feat.get(w["id"]) or {}).get("paths")
                   and official[w["id"]] < _OSM_MIN_OFFICIAL}
        osm_n = 0
        osm_found = {}
        if args.osm_gpkg:
            osm_found = osm_points_for_rivers(load_osm_candidates(args.osm_gpkg), targets)
            roads = load_drivable_grid(args.osm_gpkg, [b for b in (_bbox(t["paths"]) for t in targets.values()) if b])
            osm_found = {wid: [a for a in pts if near_road(roads, a.lat, a.lon)] for wid, pts in osm_found.items()}
        else:
            print("  no --osm-gpkg: skipping the OpenStreetMap pass", flush=True)
        # A ramp at a river mouth is the lake's (the Cedar's is Lake Washington's).
        osm_all = [ap for pts in osm_found.values() for ap in pts]
        for k in range(0, len(osm_all), 50):
            lakes = await asyncio.gather(*(fetch.nhd_near(_NHD_WATERBODY, a.lat, a.lon, 60) for a in osm_all[k:k + 50]))
            for a, feats in zip(osm_all[k:k + 50], lakes):
                a.lake = next(({"name": f["attributes"]["GNIS_Name"]} for f in feats
                               if f["attributes"].get("FType") in _LAKE_FTYPES and f["attributes"].get("GNIS_Name")), None)
        for wid, found in list(osm_found.items()):
            found = [a for a in found if not a.lake]
            # Bridges only fill in where a river has fewer than 3 better points.
            better = official[wid] + sum(1 for a in found if a.kind != "bridge") \
                + sum(1 for m in matches if m.wb_id == wid and aps[m.ap_index].source == "curated")
            if better >= _OSM_MIN_OFFICIAL:
                found = [a for a in found if a.kind != "bridge"]
            osm_found[wid] = found
        for wid, found in osm_found.items():
            print(f"  osm {targets[wid]['name']}: {len(found)} candidate points", flush=True)
            for ap in found:
                if any(haversine_m(ap.lat, ap.lon, aps[m.ap_index].lat, aps[m.ap_index].lon) < 300
                       for m in matches if m.wb_id == wid):
                    continue
                aps.append(ap)
                conf = "low" if ap.kind == "bridge" else "medium"
                matches.append(Match(wid, len(aps) - 1, "osm", conf, f"{ap.streams[0]['dist_m']} m from the river"))
                osm_n += 1
        print(f"  osm river points: {osm_n} (fetched {fetch.misses})", flush=True)
        if args.osm_gpkg:
            river_ids = {w["id"] for w in rivers}
            river_aps = list({id(aps[m.ap_index]): aps[m.ap_index] for m in matches if m.wb_id in river_ids}.values())
            n = road_gateways(args.osm_gpkg, river_aps)
            print(f"  road gateways: {n}/{len(river_aps)} river access points", flush=True)
    finally:
        await fetch.close()

    matches = [m for m in matches if f"{aps[m.ap_index].source}:{aps[m.ap_index].source_id}" not in _DROP_KEYS]
    new_lakes = propose_new_lakes(aps, matches, wbs)
    write_outputs(args, aps, wbs, matches, river_feat, merges, conflicts, new_lakes)


def propose_new_lakes(aps, matches, wbs) -> list[dict]:
    """WDFW shore sites on a lake we have no row for (the original seed kept one
    lake per name statewide): one new lake per (name, county, NHD lake)."""
    used = {m.ap_index for m in matches}
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, ap in enumerate(aps):
        if i in used or ap.source != "wdfw_shore" or not ap.water_name or not ap.county:
            continue
        groups[(lake_key(ap.water_name), ap.county.lower(), ap.lake["perm_id"] if ap.lake else None)].append(i)
    # WDFW Water Access Sites on the same NHD lake join the group.
    by_perm = {k[2]: k for k in groups if k[2]}
    for i, ap in enumerate(aps):
        if i in used or ap.source != "wdfw_was" or not ap.lake or ap.lake["perm_id"] not in by_perm:
            continue
        if ap.lake["dist_m"] <= _LAKE_REACH_M:
            groups[by_perm[ap.lake["perm_id"]]].append(i)
    out = []
    for (key, county, perm), idx in sorted(groups.items()):
        names = Counter(aps[i].water_name for i in idx if aps[i].water_name)
        out.append({"name": names.most_common(1)[0][0], "county": aps[idx[0]].county,
                    "type": "lake", "nhd_perm_id": perm, "ap_indexes": idx})
    return out


def write_outputs(args, aps, wbs, matches, river_feat, merges, conflicts, new_lakes) -> None:
    wb_by_id = {w["id"]: w for w in wbs}
    by_wb: dict[str, list[Match]] = defaultdict(list)
    for m in matches:
        # A duplicate's access points belong to the row it merges into.
        target = merges.get(m.wb_id, m.wb_id)
        if all(x.ap_index != m.ap_index for x in by_wb[target]):
            by_wb[target].append(Match(target, m.ap_index, m.method, m.confidence, m.note))

    used = sorted({m.ap_index for ms in by_wb.values() for m in ms}
                  | {i for nl in new_lakes for i in nl["ap_indexes"]})
    ap_key = {}
    ap_rows = []
    for i in used:
        ap = aps[i]
        row = asdict(ap)
        row.pop("streams")
        lake = row.pop("lake")
        row["key"] = f"{ap.source}:{ap.source_id}"
        row["nhd_water"] = (lake["name"] if lake and lake["name"] else
                            (ap.streams[0]["name"] if ap.streams else None))
        ap_key[i] = row["key"]
        ap_rows.append(row)

    water_bodies = {}
    for w in wbs:
        ms = by_wb.get(w["id"], [])
        if w["id"] in merges:
            continue
        rename = proper_name(w, [aps[m.ap_index] for m in ms], river_feat)
        activate, why = (True, "already has a spot") if w["spot_id"] else activation(w, len(ms))
        entry = {
            "name": w["name"], "type": w["type"], "county": w["county"],
            "has_spot": bool(w["spot_id"]), "activate": activate, "activate_reason": why,
            "access": [{"key": ap_key[m.ap_index], "method": m.method, "confidence": m.confidence,
                        "note": m.note} for m in ms],
        }
        if rename and rename != w["name"] and activate:
            entry["rename_to"] = rename
        if is_lakeish(w) and w["type"] != "lake":
            entry["retype_to"] = "lake"
        if ms or rename or entry.get("retype_to") or not w["spot_id"]:
            water_bodies[w["id"]] = entry

    out = {
        "generated_by": "scripts/build_access_points.py",
        "access_points": ap_rows,
        "water_bodies": water_bodies,
        "merges": [{"from_id": d, "from_name": wb_by_id[d]["name"], "into_id": k, "into_name": wb_by_id[k]["name"],
                    "drop_spot": bool(wb_by_id[d]["spot_id"])}
                   for d, k in sorted(merges.items(), key=lambda kv: wb_by_id[kv[1]]["name"])],
        "new_water_bodies": [{"name": nl["name"], "county": nl["county"], "type": "lake",
                              "access": [{"key": ap_key[i], "method": "name+county", "confidence": "high",
                                          "note": None} for i in nl["ap_indexes"]]}
                             for nl in new_lakes],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
        f.write("\n")
    print(f"wrote {args.out}: {len(ap_rows)} access points, {len(water_bodies)} water bodies, "
          f"{len(out['merges'])} merges, {len(new_lakes)} new lakes")
    write_report(args.report, out, aps, wbs, river_feat, conflicts, matches)


def write_report(path, out, aps, wbs, river_feat, conflicts, matches) -> None:
    wb_by_id = {w["id"]: w for w in wbs}
    aps_by_key = {r["key"]: r for r in out["access_points"]}
    L = ["# Access points: build report\n", "## Sources\n",
         "| source | points | used |", "|---|---|---|"]
    used = Counter(r["source"] for r in out["access_points"])
    total = Counter(ap.source for ap in aps)
    for s, n in total.most_common():
        L.append(f"| {s} | {n} | {used.get(s, 0)} |")
    L.append("")

    wbe = out["water_bodies"]
    L.append("## Recommendable water bodies, before → after\n")
    L.append("| type | before (has a spot) | after (activate) | with ≥1 access point | access points |")
    L.append("|---|---|---|---|---|")
    for t in ("lake", "river", "creek", "coastal"):
        rows = [(wid, e) for wid, e in wbe.items() if (e.get("retype_to") or e["type"]) == t]
        before = sum(1 for w in wbs if w["type"] == t and w["spot_id"])
        after = sum(1 for _, e in rows if e["activate"]) + (len(out["new_water_bodies"]) if t == "lake" else 0)
        withap = sum(1 for _, e in rows if e["access"])
        naps = sum(len(e["access"]) for _, e in rows)
        L.append(f"| {t} | {before} | {after} | {withap} | {naps} |")
    conf = Counter(a["confidence"] for e in wbe.values() for a in e["access"])
    L.append(f"\nAccess-point confidence: {dict(conf)}. Merges: {len(out['merges'])}. "
             f"New lakes: {len(out['new_water_bodies'])}. Conflicts (two rows with spots, same water): {len(conflicts)}.\n")

    L.append("## Merges (duplicate rows → the recommendable row)\n")
    for m in out["merges"]:
        d = wb_by_id[m["from_id"]]
        L.append(f"- {m['from_name']} ({d['stocking_events']} stockings) → {m['into_name']}")
    L.append("")
    if conflicts:
        L.append("## Conflicts (not merged)\n")
        for ids in conflicts:
            L.append("- " + " = ".join(f"{wb_by_id[x]['name']} ({wb_by_id[x]['county']})" for x in ids))
        L.append("")

    L.append("## Renames (old name kept as an alias)\n")
    for wid, e in sorted(wbe.items(), key=lambda kv: kv[1]["name"]):
        if e.get("rename_to"):
            L.append(f"- {e['name']} → {e['rename_to']}" + (f" (and type → {e['retype_to']})" if e.get("retype_to") else ""))
    L.append("")

    L.append("## New lakes (WDFW shore access, no row today)\n")
    for nl in out["new_water_bodies"]:
        L.append(f"- {nl['name']} ({nl['county']}): {len(nl['access'])} access points")
    L.append("")

    L.append("## Not activated (no spot, stays unrecommendable)\n")
    for wid, e in sorted(wbe.items(), key=lambda kv: kv[1]["name"]):
        if not e["activate"]:
            L.append(f"- {e['name']} ({e['type']}, {e['county'] or '?'}): {e['activate_reason']}"
                     + (f" — {len(e['access'])} access points" if e["access"] else ""))
    L.append("")

    L.append("## Rivers and creeks: access points\n")
    for wid, e in sorted(wbe.items(), key=lambda kv: (kv[1].get('rename_to') or kv[1]["name"]).lower()):
        if (e.get("retype_to") or e["type"]) not in ("river", "creek") or not e["access"]:
            continue
        rf = river_feat.get(wid, {})
        L.append(f"**{e.get('rename_to') or e['name']}** — NHD {rf.get('gnis_name')} ({rf.get('how')})"
                 f"{'' if e['has_spot'] else ' — NEW'}")
        for a in e["access"]:
            ap = aps_by_key[a["key"]]
            L.append(f"- [{a['confidence']}] {ap['source']}: {ap['name']} — {ap['kind']} "
                     f"({ap['lat']:.5f},{ap['lon']:.5f}){' — ' + a['note'] if a['note'] else ''}"
                     f"{' — SEASONAL: ' + ap['seasonal'] if ap['seasonal'] else ''}")
        L.append("")

    L.append("## Lower-confidence lake matches\n")
    for wid, e in sorted(wbe.items(), key=lambda kv: kv[1]["name"]):
        if (e.get("retype_to") or e["type"]) != "lake":
            continue
        for a in e["access"]:
            if a["confidence"] != "high":
                ap = aps_by_key[a["key"]]
                L.append(f"- [{a['confidence']}] {e['name']} ({e['county']}) ← {ap['source']} {ap['name']} "
                         f"({ap['lat']:.5f},{ap['lon']:.5f}) — {a['note'] or ''}")
    L.append("")

    no_access = [e for e in wbe.values() if not e["access"] and (e.get("retype_to") or e["type"]) == "lake"]
    L.append(f"## Lakes with no access point: {len(no_access)}\n")
    for e in sorted(no_access, key=lambda e: e["name"])[:80]:
        L.append(f"- {e['name']} ({e['county']}){'' if e['has_spot'] else ' — no spot'}")
    L.append("")

    used_idx = {i for i, ap in enumerate(aps) if f"{ap.source}:{ap.source_id}" in aps_by_key}
    unmatched = [ap for i, ap in enumerate(aps) if i not in used_idx and ap.source in ("wdfw_was", "parks_launch", "wdfw_shore")]
    waters = Counter()
    for ap in unmatched:
        if ap.lake and ap.lake["name"]:
            waters[ap.lake["name"]] += 1
        elif ap.streams:
            waters[ap.streams[0]["name"]] += 1
        else:
            waters["(no named water within reach)"] += 1
    L.append(f"## Official access points matched to nothing: {len(unmatched)}\n")
    L.append("Waters they serve that we don't carry (or didn't match): "
             + ", ".join(f"{k} ×{v}" for k, v in waters.most_common(60)) + "\n")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cache", required=True, help="directory for cached HTTP responses")
    p.add_argument("--out", default="data/access_points.json")
    p.add_argument("--report", default="access_points_report.md")
    p.add_argument("--osm-gpkg", help="Geofabrik washington-latest-free.gpkg (unzipped), for river access")
    asyncio.run(build(p.parse_args()))


if __name__ == "__main__":
    main()
