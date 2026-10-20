"""
Free drive-time estimates on the offline road network (TODO-19).

One Dijkstra from the user's origin over data/road_graph.npz (built by
scripts/build_road_graph.py from OpenStreetMap) gives an estimate to every
access point in the state at once, with no HERE call. The context builder uses
it to drop waters past the user's drive limit and to choose each water's access
point; HERE is then called only for the three cards the user sees.

On the trip date:
  - car ferries (conditions/ferries.py) are edges costing crossing + the
    expected wait to board on that date; a route that uses one says which;
  - a pass inside its usual winter closure (conditions/pass_closures.py) is
    dropped; near either end of the window it is used but flagged.

Estimates are labeled as such everywhere they are shown. When the graph file is
missing, `available()` is False and callers fall back to the straight-line
estimate (routing.haversine_drive_minutes).
"""

import heapq
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np

from conditions import ferries, pass_closures
from conditions.routing import haversine_drive_minutes, haversine_km

log = logging.getLogger(__name__)

GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "road_graph.npz"
_ORIGIN_SNAP_NODES = 4      # origin connects to its few nearest network nodes
_SNAP_SLACK_KM = 0.5        # ...within this of the nearest one


class _Graph:
    def __init__(self, path: Path):
        z = np.load(path, allow_pickle=False)
        self.lat = z["lat"]
        self.lon = z["lon"]
        self.main = z["main"]
        self.indptr = z["indptr"].tolist()
        self.to = z["to"].tolist()
        self.minutes = z["minutes"].tolist()
        self.closure = z["closure"].tolist()
        self.closure_ids = [str(c) for c in z["closure_ids"]]
        self.built = str(z["built"])
        self.attach = {
            (round(float(la), 5), round(float(lo), 5)): (int(n), float(m))
            for la, lo, n, m in zip(z["ap_lat"], z["ap_lon"], z["ap_node"], z["ap_minutes"])
        }
        self._main_lat = np.where(self.main, self.lat, np.nan)
        # Ferry routes as extra edges between terminal nodes, both directions.
        self.ferry_edges: dict[int, list[tuple[int, str]]] = {}
        for r in ferries.ROUTES:
            a = self.nearest(*r.a)[0][0]
            b = self.nearest(*r.b)[0][0]
            self.ferry_edges.setdefault(a, []).append((b, r.id))
            self.ferry_edges.setdefault(b, []).append((a, r.id))

    def nearest(self, plat: float, plon: float, k: int = 1) -> list[tuple[int, float]]:
        """[(node, km)] of the k nearest main-network nodes."""
        c = np.cos(np.radians(plat))
        d2 = (self._main_lat - plat) ** 2 + ((self.lon - plon) * c) ** 2
        d2 = np.where(np.isnan(d2), np.inf, d2)
        idx = np.argpartition(d2, k)[:k] if k < len(d2) else np.arange(len(d2))
        idx = sorted(idx.tolist(), key=lambda i: d2[i])
        return [(i, haversine_km(plat, plon, float(self.lat[i]), float(self.lon[i]))) for i in idx]


_graph: _Graph | None = None
_graph_failed = False


def _load() -> _Graph | None:
    global _graph, _graph_failed
    if _graph is None and not _graph_failed:
        try:
            _graph = _Graph(GRAPH_PATH)
            log.info("road_graph_loaded", extra={"nodes": len(_graph.lat), "built": _graph.built})
        except Exception as exc:
            _graph_failed = True
            log.warning("road_graph_unavailable", extra={"reason": type(exc).__name__, "detail": str(exc)[:160]})
    return _graph


def available() -> bool:
    return _load() is not None


@dataclass
class Estimate:
    minutes: float
    ferries: list[str] = field(default_factory=list)      # route ids, in driving order
    passes: list[str] = field(default_factory=list)       # pass closure ids on the route


class DriveField:
    """Shortest drive from one origin to every network node, on one date."""

    def __init__(self, g: _Graph, origin_lat: float, origin_lon: float, day: date, limit_minutes: float):
        self.g = g
        self.day = day
        closed = pass_closures.closed_ids(day)
        skip = {i for i, cid in enumerate(g.closure_ids) if cid in closed}
        ferry_cost = {r.id: r.crossing_min + ferries.expected_wait(r, day) for r in ferries.ROUTES}

        n = len(g.lat)
        dist = [float("inf")] * n
        pred = [-1] * n
        via = [None] * n            # ("ferry", route id) or ("pass", closure idx) on the edge into a node
        heap = []
        near = g.nearest(origin_lat, origin_lon, _ORIGIN_SNAP_NODES)
        cutoff = near[0][1] + _SNAP_SLACK_KM
        for node, km in near:
            if km <= cutoff:
                d = haversine_drive_minutes(origin_lat, origin_lon, float(g.lat[node]), float(g.lon[node])) if km > 0.05 else 0
                if d < dist[node]:
                    dist[node] = d
                    heapq.heappush(heap, (d, node))
        self.snap_km = near[0][1]

        indptr, to, w, clos, fe = g.indptr, g.to, g.minutes, g.closure, g.ferry_edges
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u] or d > limit_minutes:
                continue
            for e in range(indptr[u], indptr[u + 1]):
                c = clos[e]
                if c >= 0 and c in skip:
                    continue
                v = to[e]
                nd = d + w[e]
                if nd < dist[v]:
                    dist[v] = nd
                    pred[v] = u
                    via[v] = ("pass", c) if c >= 0 else None
                    heapq.heappush(heap, (nd, v))
            for v, rid in fe.get(u, ()):
                nd = d + ferry_cost[rid]
                if nd < dist[v]:
                    dist[v] = nd
                    pred[v] = u
                    via[v] = ("ferry", rid)
                    heapq.heappush(heap, (nd, v))
        self.dist = dist
        self.pred = pred
        self.via = via
        self._route_memo: dict[int, tuple[tuple, tuple]] = {}

    def _route_flags(self, node: int) -> tuple[tuple, tuple]:
        """(ferry ids, pass ids) along the shortest route to `node` (memoized walk)."""
        chain = []
        x = node
        while x >= 0 and x not in self._route_memo:
            chain.append(x)
            x = self.pred[x]
        fer, pas = self._route_memo.get(x, ((), ())) if x >= 0 else ((), ())
        for y in reversed(chain):
            v = self.via[y]
            if v and v[0] == "ferry":
                fer = fer + (v[1],)
            elif v and v[0] == "pass":
                cid = self.g.closure_ids[v[1]]
                if cid not in pas:
                    pas = pas + (cid,)
            self._route_memo[y] = (fer, pas)
        return fer, pas

    def to_point(self, lat: float, lon: float, gateway: tuple | None = None) -> Estimate | None:
        """Estimate to an access point (or any coordinate); None when unreachable
        within the limit. `gateway` = (lat, lon, minutes) from the access point's
        own last-stretch data, used when the graph has no attachment for it."""
        g = self.g
        att = g.attach.get((round(lat, 5), round(lon, 5)))
        if att:
            node, extra = att
        elif gateway and gateway[0] is not None:
            node, km = g.nearest(gateway[0], gateway[1])[0]
            extra = float(gateway[2]) + haversine_drive_minutes(gateway[0], gateway[1], float(g.lat[node]), float(g.lon[node]))
        else:
            node, km = g.nearest(lat, lon)[0]
            extra = haversine_drive_minutes(lat, lon, float(g.lat[node]), float(g.lon[node])) if km > 0.05 else 0
        d = self.dist[node]
        if d == float("inf"):
            return None
        fer, pas = self._route_flags(node)
        return Estimate(minutes=d + extra, ferries=list(fer), passes=list(pas))


@lru_cache(maxsize=16)
def _field(lat_r: float, lon_r: float, day: date, limit: int) -> DriveField:
    return DriveField(_load(), lat_r, lon_r, day, limit)


def drive_field(origin_lat: float, origin_lon: float, day: date, limit_minutes: float) -> DriveField | None:
    """The drive field for an origin and date, or None when the graph is missing.
    Cached per (origin to ~100 m, date, limit): a re-run with the same intake
    reuses it."""
    if _load() is None:
        return None
    return _field(round(origin_lat, 3), round(origin_lon, 3), day, int(limit_minutes) + 1)
