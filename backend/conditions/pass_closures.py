"""
Mountain highways closed for winter, by typical dates (TODO-19).

WSDOT closes these every winter; the dates move with the snow (closure after
the first big storm, reopening after avalanche clearing). Inside the usual
window the road-network estimate drops the road, and HERE is told to avoid the
same box (planning-mode HERE has no date, so it would happily route over a
closed pass). Near either end of the window the road is still used, with an
advisory to check WSDOT before going.

Each closure is identified offline (scripts/build_road_graph.py) as the graph
edges whose OSM `ref` matches and that lie inside `bbox`
(south, north, west, east).
"""

from dataclasses import dataclass
from datetime import date, timedelta

WSDOT_PASSES = "https://wsdot.com/travel/real-time/mountainpasses"
_SHOULDER_DAYS = 21


@dataclass(frozen=True)
class PassClosure:
    id: str
    name: str
    refs: tuple[str, ...]        # OSM ref values; "" matches unreferenced roads by name
    names: tuple[str, ...]       # OSM names, for roads without a ref
    bbox: tuple[float, float, float, float]
    closes: tuple[int, int]      # (month, day) the road usually closes
    opens: tuple[int, int]       # (month, day) it usually reopens
    usually: str                 # for advisories


CLOSURES: tuple[PassClosure, ...] = (
    PassClosure("sr20", "SR 20 North Cascades Highway (Washington Pass)",
                ("SR 20",), (), (48.45, 48.76, -121.06, -120.50),
                (11, 20), (5, 5), "closes for winter in mid to late November and reopens in early May"),
    PassClosure("sr410", "SR 410 Chinook Pass",
                ("SR 410",), (), (46.86, 46.95, -121.56, -121.44),
                (11, 20), (5, 25), "closes for winter in November and reopens in late May"),
    PassClosure("sr123", "SR 123 Cayuse Pass",
                ("SR 123",), (), (46.76, 46.866, -121.56, -121.51),
                (11, 20), (5, 25), "closes for winter in November and reopens in late May"),
    PassClosure("stevens-canyon", "Stevens Canyon Road (Mount Rainier)",
                (), ("Stevens Canyon Road",), (46.70, 46.78, -121.66, -121.55),
                (11, 1), (5, 25), "closes for winter in fall and reopens in late May"),
)

BY_ID = {c.id: c for c in CLOSURES}


def _window(c: PassClosure, day: date) -> tuple[date, date]:
    """The closure window that `day` falls in or nearest to (it spans New Year)."""
    start = date(day.year, *c.closes)
    end = date(day.year + 1, *c.opens)
    if day <= date(day.year, *c.opens) + timedelta(days=_SHOULDER_DAYS):
        start = date(day.year - 1, *c.closes)
        end = date(day.year, *c.opens)
    return start, end


def status(c: PassClosure, day: date) -> str:
    """'closed' inside the usual window, 'uncertain' within three weeks of
    either end, else 'open'."""
    start, end = _window(c, day)
    if start <= day < end:
        return "closed"
    if start - timedelta(days=_SHOULDER_DAYS) <= day < start or end <= day < end + timedelta(days=_SHOULDER_DAYS):
        return "uncertain"
    return "open"


def closed_ids(day: date) -> frozenset[str]:
    return frozenset(c.id for c in CLOSURES if status(c, day) == "closed")


def here_avoid_areas(day: date) -> str | None:
    """HERE `avoid[areas]` value for the passes closed on `day`, or None."""
    boxes = [BY_ID[i].bbox for i in sorted(closed_ids(day))]
    if not boxes:
        return None
    return "|".join(f"bbox:{w},{s},{e},{n}" for s, n, w, e in boxes)


def advisory(c: PassClosure, day: date) -> str:
    return (f"Route uses {c.name}, which usually {c.usually}; "
            f"check WSDOT pass conditions before {day.strftime('%b')} {day.day}")
