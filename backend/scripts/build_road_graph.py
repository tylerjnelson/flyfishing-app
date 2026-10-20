"""
Build the road network the drive-time estimate runs on (TODO-19).

    python -m scripts.build_road_graph --osm-gpkg washington-latest-free.gpkg \
        [--access-points data/access_points.json] [--out data/road_graph.npz]

Offline, from the Geofabrik Washington extract (no network). Output is a compact
directed graph of the roads people drive between towns and waters: motorway,
trunk, primary, secondary and tertiary roads and their links. Nodes are way
endpoints and junctions only; each edge is the stretch between two of them,
weighted in minutes at the posted speed (or a default per road class) times a
real-world factor. Oneway roads are one-directional.

Also stored:
  - which edges belong to a seasonal pass closure (conditions/pass_closures.py),
    so the estimate can drop them on dates the pass is closed;
  - `main`: nodes in the network reachable from the largest piece, counting the
    ferry routes in conditions/ferries.py (an island counts if a ferry serves it);
    origins, destinations and terminals snap only to these;
  - for every access point in data/access_points.json, the network node its
    local roads (residential, unclassified, tracks) lead to and the drive from
    there, keyed by the point's coordinates. Without it a lake 3 km up a
    residential road would snap to whatever network node is nearest in a
    straight line, maybe across the lake.

The runtime side is conditions/road_network.py.
"""

import argparse
import heapq
import json
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from conditions import ferries, pass_closures
from scripts.build_access_points import _gpkg_coords, haversine_m

log = logging.getLogger("build_road_graph")

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "road_graph.npz"
DEFAULT_APS = Path(__file__).resolve().parent.parent / "data" / "access_points.json"

NETWORK = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
           "secondary", "secondary_link", "tertiary", "tertiary_link")
# km/h when OSM has no maxspeed. Links are short, slow ramps.
DEFAULT_KMH = {"motorway": 105, "trunk": 90, "primary": 80, "secondary": 70, "tertiary": 60,
               "motorway_link": 60, "trunk_link": 50, "primary_link": 45, "secondary_link": 40,
               "tertiary_link": 35}
# Nobody averages the posted speed: towns, signals, curves, trucks.
REAL_WORLD = {"motorway": 0.95, "trunk": 0.9, "primary": 0.85, "secondary": 0.85, "tertiary": 0.8}
# Local roads for the last stretch to an access point (no service roads: ~600k
# driveways and parking aisles).
LOCAL_KMH = {"unclassified": 50, "residential": 35, "living_street": 15, "track": 25,
             "track_grade1": 30, "track_grade2": 25, "track_grade3": 20, "track_grade4": 15,
             "track_grade5": 10}
_ATTACH_MAX_WALK_M = 1000
_CELL = 0.1


def _kmh(fclass: str, maxspeed: int | None) -> float:
    base = fclass.removesuffix("_link")
    kmh = float(maxspeed) if maxspeed and 15 <= maxspeed <= 130 else DEFAULT_KMH[fclass]
    if fclass.endswith("_link"):
        kmh = min(kmh, DEFAULT_KMH[fclass])
    return kmh * REAL_WORLD[base]


def _closure_of(ref: str | None, name: str | None, lat: float, lon: float) -> int:
    for i, c in enumerate(pass_closures.CLOSURES):
        s, n, w, e = c.bbox
        if not (s <= lat <= n and w <= lon <= e):
            continue
        if (ref and ref in c.refs) or (name and name in c.names):
            return i
    return -1


def build_network(gpkg: str) -> dict:
    db = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    sql = ("SELECT fclass, ref, name, oneway, maxspeed, geom FROM gis_osm_roads_free WHERE fclass IN ("
           + ",".join(f"'{c}'" for c in NETWORK) + ")")
    ways = []
    seen: dict[tuple, int] = defaultdict(int)
    for fclass, ref, name, oneway, maxspeed, geom in db.execute(sql):
        pts = [(round(y, 6), round(x, 6)) for x, y in _gpkg_coords(geom)]
        if len(pts) < 2:
            continue
        ways.append((fclass, ref, name, oneway, maxspeed, pts))
        last = len(pts) - 1
        for i, p in enumerate(pts):
            seen[p] += 2 if i in (0, last) else 1
    db.close()
    junction = {p for p, n in seen.items() if n >= 2}
    index = {p: i for i, p in enumerate(sorted(junction))}
    log.info("ways %d, vertices %d, nodes %d", len(ways), len(seen), len(index))

    best: dict[tuple[int, int], tuple[float, int]] = {}

    def add(a: int, b: int, minutes: float, closure: int):
        cur = best.get((a, b))
        if cur is None or minutes < cur[0]:
            best[(a, b)] = (minutes, closure)

    for fclass, ref, name, oneway, maxspeed, pts in ways:
        kmh = _kmh(fclass, maxspeed)
        start, metres, closure = index[pts[0]], 0.0, -1
        for p, q in zip(pts, pts[1:]):
            metres += haversine_m(p[0], p[1], q[0], q[1])
            if closure < 0:
                closure = _closure_of(ref, name, q[0], q[1])
            if q in index:
                end = index[q]
                if end != start:
                    minutes = metres / 1000 / kmh * 60
                    # Geofabrik: F = along the geometry only, T = against it only, B = both
                    if oneway != "T":
                        add(start, end, minutes, closure)
                    if oneway != "F":
                        add(end, start, minutes, closure)
                start, metres, closure = end, 0.0, -1

    n = len(index)
    lat = np.empty(n)
    lon = np.empty(n)
    for (la, lo), i in index.items():
        lat[i], lon[i] = la, lo
    order = sorted(best)
    src = np.fromiter((a for a, _ in order), dtype=np.int32, count=len(order))
    dst = np.fromiter((b for _, b in order), dtype=np.int32, count=len(order))
    mins = np.fromiter((best[k][0] for k in order), dtype=np.float32, count=len(order))
    clos = np.fromiter((best[k][1] for k in order), dtype=np.int8, count=len(order))
    indptr = np.zeros(n + 1, dtype=np.int32)
    np.add.at(indptr, src + 1, 1)
    indptr = np.cumsum(indptr).astype(np.int32)
    log.info("edges %d, closure edges %s", len(order),
             {c.id: int((clos == i).sum()) for i, c in enumerate(pass_closures.CLOSURES)})
    return {"lat": lat, "lon": lon, "indptr": indptr, "to": dst, "minutes": mins, "closure": clos}


def nearest_node(lat: np.ndarray, lon: np.ndarray, mask: np.ndarray | None, plat: float, plon: float) -> tuple[int, float]:
    """(node index, metres) of the nearest node, among `mask` if given."""
    k = np.cos(np.radians(plat))
    d2 = (lat - plat) ** 2 + ((lon - plon) * k) ** 2
    if mask is not None:
        d2 = np.where(mask, d2, np.inf)
    i = int(np.argmin(d2))
    return i, haversine_m(plat, plon, float(lat[i]), float(lon[i]))


def main_component(g: dict) -> np.ndarray:
    """Nodes connected (ignoring direction) to the largest piece, ferries included."""
    n = len(g["lat"])
    parent = np.arange(n)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    indptr, to = g["indptr"], g["to"]
    for a in range(n):
        for e in range(indptr[a], indptr[a + 1]):
            union(a, int(to[e]))
    for r in ferries.ROUTES:
        a, _ = nearest_node(g["lat"], g["lon"], None, *r.a)
        b, _ = nearest_node(g["lat"], g["lon"], None, *r.b)
        union(a, b)
    roots = np.array([find(i) for i in range(n)])
    big = np.bincount(roots).argmax()
    main = roots == big
    log.info("main network: %d of %d nodes", int(main.sum()), n)
    return main


def attach_access_points(gpkg: str, g: dict, main: np.ndarray, points: list[tuple[float, float]]) -> dict:
    """For each access point: the main network node its local roads reach first,
    and the minutes from there (multi-source Dijkstra from every network node,
    over local and network roads near the points)."""
    cells = {(int(la // _CELL), int(lo // _CELL)) for la, lo in points}
    wanted = {(i + di, j + dj) for i, j in cells for di in (-1, 0, 1) for dj in (-1, 0, 1)}
    net_index = {(round(float(la), 6), round(float(lo), 6)): i
                 for i, (la, lo, m) in enumerate(zip(g["lat"], g["lon"], main)) if m}

    db = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    classes = tuple(LOCAL_KMH) + NETWORK
    sql = ("SELECT fclass, geom FROM gis_osm_roads_free WHERE fclass IN ("
           + ",".join(f"'{c}'" for c in classes) + ")")
    adj: dict[tuple, list] = defaultdict(list)
    for fclass, geom in db.execute(sql):
        pts = [(round(y, 6), round(x, 6)) for x, y in _gpkg_coords(geom)]
        if not any((int(p[0] // _CELL), int(p[1] // _CELL)) in wanted for p in (pts[0], pts[-1])):
            continue
        kmh = LOCAL_KMH.get(fclass) or DEFAULT_KMH[fclass] * 0.8
        for a, b in zip(pts, pts[1:]):
            minutes = haversine_m(a[0], a[1], b[0], b[1]) / 1000 / kmh * 60
            adj[a].append((b, minutes))
            adj[b].append((a, minutes))
    db.close()

    best: dict[tuple, tuple[float, int]] = {}
    heap = [(0.0, p, i) for p, i in net_index.items() if p in adj]
    heapq.heapify(heap)
    while heap:
        d, p, src = heapq.heappop(heap)
        if p in best:
            continue
        best[p] = (d, src)
        for q, w in adj[p]:
            if q not in best:
                heapq.heappush(heap, (d + w, q, src))

    grid: dict[tuple, list] = defaultdict(list)
    for p in best:
        grid[(int(p[0] // 0.005), int(p[1] // 0.005))].append(p)
    out = {}
    for la, lo in points:
        i, j = int(la // 0.005), int(lo // 0.005)
        near = [p for di in (-2, -1, 0, 1, 2) for dj in (-2, -1, 0, 1, 2) for p in grid.get((i + di, j + dj), [])]
        if not near:
            continue
        p = min(near, key=lambda p: haversine_m(la, lo, p[0], p[1]))
        walk = haversine_m(la, lo, p[0], p[1])
        if walk > _ATTACH_MAX_WALK_M:
            continue
        d, src = best[p]
        out[(la, lo)] = (src, d + walk / 1000 / 20 * 60)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--osm-gpkg", required=True)
    ap.add_argument("--access-points", default=str(DEFAULT_APS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    t = time.time()
    g = build_network(args.osm_gpkg)
    main_mask = main_component(g)
    pts = sorted({(round(p["lat"], 5), round(p["lon"], 5))
                  for p in json.loads(Path(args.access_points).read_text())["access_points"]})
    attach = attach_access_points(args.osm_gpkg, g, main_mask, pts)
    log.info("access points attached by local roads: %d of %d", len(attach), len(pts))
    keys = sorted(attach)
    np.savez_compressed(
        args.out,
        lat=g["lat"], lon=g["lon"], main=main_mask,
        indptr=g["indptr"], to=g["to"], minutes=g["minutes"], closure=g["closure"],
        closure_ids=np.array([c.id for c in pass_closures.CLOSURES]),
        ap_lat=np.array([k[0] for k in keys]), ap_lon=np.array([k[1] for k in keys]),
        ap_node=np.array([attach[k][0] for k in keys], dtype=np.int32),
        ap_minutes=np.array([attach[k][1] for k in keys], dtype=np.float32),
        source=np.array(Path(args.osm_gpkg).name),
        built=np.array(time.strftime("%Y-%m-%d")),
    )
    log.info("wrote %s (%.1f MB) in %.0f s", args.out, Path(args.out).stat().st_size / 1e6, time.time() - t)


if __name__ == "__main__":
    main()
