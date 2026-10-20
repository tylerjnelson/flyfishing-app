"""
Car ferries the road-network estimate may use, and what a ferry costs on a date (TODO-19).

The OSM extract has no ferry routes, so without this table every Olympic
Peninsula and island water looks like a drive around through Tacoma. Each route
is an edge between its two terminals (coordinates from OSM ferry_terminal
features): crossing time plus the expected wait to board on the trip date.

Waits are rough, by level of demand, not timetables:
  normal   off-season, or a summer weekday off the busiest routes
  summer   a weekday from Memorial Day weekend to Labor Day
  peak     a summer Friday-Sunday, or a holiday weekend any time of year
Busy routes (Edmonds-Kingston, Seattle-Bainbridge, Mukilteo-Clinton, the San
Juans) can run 1-2 hour waits on peak days. Routes that take vehicle
reservations (Port Townsend-Coupeville, Anacortes-San Juans) are cheaper with
one; the wait here assumes none.

A ferry trip also costs a little score (context_builder): the wait is uncertain
and the boat is a fixed schedule; more on peak days.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

WSF_SCHEDULES = "https://wsdot.wa.gov/travel/washington-state-ferries/schedules/schedule-route"
WSF_RESERVATIONS = "https://secureapps.wsdot.wa.gov/ferries/reservations/vehicle/default.aspx"

SCORE_PENALTY = {"normal": 0.25, "summer": 0.35, "peak": 0.6}


@dataclass(frozen=True)
class FerryRoute:
    id: str
    name: str
    operator: str
    a: tuple[float, float]
    b: tuple[float, float]
    crossing_min: int
    wait_min: dict              # demand level -> expected minutes waiting to board
    reservations: bool = False
    schedule_url: str | None = None
    note: str | None = None


def _w(normal: int, summer: int, peak: int) -> dict:
    return {"normal": normal, "summer": summer, "peak": peak}


ROUTES: tuple[FerryRoute, ...] = (
    FerryRoute("sea-bi", "Seattle–Bainbridge", "Washington State Ferries",
               (47.6024, -122.3390), (47.6223, -122.5095), 35, _w(20, 35, 60), schedule_url=WSF_SCHEDULES),
    FerryRoute("sea-br", "Seattle–Bremerton", "Washington State Ferries",
               (47.6024, -122.3390), (47.5618, -122.6249), 60, _w(25, 35, 60), schedule_url=WSF_SCHEDULES),
    FerryRoute("ed-king", "Edmonds–Kingston", "Washington State Ferries",
               (47.8134, -122.3855), (47.7947, -122.4948), 30, _w(20, 45, 90), schedule_url=WSF_SCHEDULES),
    FerryRoute("muk-cl", "Mukilteo–Clinton", "Washington State Ferries",
               (47.9507, -122.2970), (47.9748, -122.3494), 20, _w(15, 30, 75), schedule_url=WSF_SCHEDULES),
    FerryRoute("f-v", "Fauntleroy–Vashon", "Washington State Ferries",
               (47.5232, -122.3965), (47.5109, -122.4638), 20, _w(15, 25, 45), schedule_url=WSF_SCHEDULES),
    FerryRoute("f-s", "Fauntleroy–Southworth", "Washington State Ferries",
               (47.5232, -122.3965), (47.5130, -122.4959), 35, _w(20, 30, 45), schedule_url=WSF_SCHEDULES),
    FerryRoute("v-s", "Vashon–Southworth", "Washington State Ferries",
               (47.5109, -122.4638), (47.5130, -122.4959), 10, _w(15, 20, 30), schedule_url=WSF_SCHEDULES),
    FerryRoute("pd-tal", "Point Defiance–Tahlequah", "Washington State Ferries",
               (47.3063, -122.5141), (47.3320, -122.5078), 15, _w(20, 30, 45), schedule_url=WSF_SCHEDULES),
    FerryRoute("pt-cou", "Port Townsend–Coupeville", "Washington State Ferries",
               (48.1111, -122.7590), (48.1591, -122.6727), 35, _w(30, 45, 90), reservations=True,
               schedule_url=WSF_SCHEDULES, note="sailings are cancelled at extreme low tides"),
    FerryRoute("ana-lop", "Anacortes–Lopez Island", "Washington State Ferries",
               (48.5073, -122.6775), (48.5708, -122.8831), 45, _w(30, 60, 120), reservations=True,
               schedule_url=WSF_SCHEDULES),
    FerryRoute("ana-shaw", "Anacortes–Shaw Island", "Washington State Ferries",
               (48.5073, -122.6775), (48.5847, -122.9298), 60, _w(30, 60, 120), reservations=True,
               schedule_url=WSF_SCHEDULES),
    FerryRoute("ana-orc", "Anacortes–Orcas Island", "Washington State Ferries",
               (48.5073, -122.6775), (48.5973, -122.9436), 65, _w(30, 60, 120), reservations=True,
               schedule_url=WSF_SCHEDULES),
    FerryRoute("ana-fh", "Anacortes–Friday Harbor", "Washington State Ferries",
               (48.5073, -122.6775), (48.5357, -123.0140), 65, _w(30, 60, 120), reservations=True,
               schedule_url=WSF_SCHEDULES),
    FerryRoute("steil-and", "Steilacoom–Anderson Island", "Pierce County Ferry",
               (47.1731, -122.6031), (47.1786, -122.6773), 20, _w(30, 30, 45)),
    FerryRoute("guemes", "Anacortes–Guemes Island", "Skagit County Ferry",
               (48.5191, -122.6236), (48.5280, -122.6247), 10, _w(15, 20, 30)),
    FerryRoute("lummi", "Gooseberry Point–Lummi Island", "Whatcom County Ferry",
               (48.7312, -122.6702), (48.7204, -122.6813), 10, _w(15, 20, 30)),
    FerryRoute("keller", "Keller Ferry (SR 21, Lake Roosevelt)", "WSDOT",
               (47.9295, -118.6902), (47.9465, -118.6907), 15, _w(15, 15, 20),
               note="free; runs about 6 am to midnight"),
    FerryRoute("gif-inch", "Gifford–Inchelium (Lake Roosevelt)", "Colville Tribes",
               (48.2782, -118.1379), (48.2778, -118.1534), 15, _w(15, 15, 20),
               note="free; daytime only"),
)

BY_ID = {r.id: r for r in ROUTES}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    days = [d for d in calendar.Calendar().itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    return days[n] if n >= 0 else days[n]


def _holiday_weekends(year: int) -> list[tuple[date, date]]:
    """The long weekends ferry traffic piles up on, as (first, last) day."""
    memorial = _nth_weekday(year, 5, 0, -1)
    labor = _nth_weekday(year, 9, 0, 0)
    thanksgiving = _nth_weekday(year, 11, 3, 3)
    mlk = _nth_weekday(year, 1, 0, 2)
    presidents = _nth_weekday(year, 2, 0, 2)
    july4 = date(year, 7, 4)
    return [
        (mlk - timedelta(days=3), mlk),
        (presidents - timedelta(days=3), presidents),
        (memorial - timedelta(days=3), memorial),
        (july4 - timedelta(days=2), july4 + timedelta(days=2)),
        (labor - timedelta(days=3), labor),
        (thanksgiving - timedelta(days=1), thanksgiving + timedelta(days=3)),
        (date(year, 12, 23), date(year, 12, 26)),
        (date(year, 12, 30), date(year, 12, 31)),
        (date(year, 1, 1), date(year, 1, 1)),
    ]


def demand(day: date) -> str:
    """'peak', 'summer' or 'normal' for ferry traffic on `day`."""
    if any(a <= day <= b for a, b in _holiday_weekends(day.year)):
        return "peak"
    summer = _nth_weekday(day.year, 5, 0, -1) - timedelta(days=3) <= day <= _nth_weekday(day.year, 9, 0, 0)
    if summer:
        return "peak" if day.weekday() >= 4 else "summer"
    return "normal"


_DEMAND_TEXT = {
    "peak": "a peak day (summer weekend or holiday)",
    "summer": "a summer weekday",
    "normal": "an off-peak day",
}


def expected_wait(route: FerryRoute, day: date) -> int:
    return route.wait_min[demand(day)]


def ferry_summary(route: FerryRoute, day: date) -> dict:
    """JSON-safe dict stored on the candidate; feeds the card and the model."""
    level = demand(day)
    return {
        "id": route.id,
        "name": route.name,
        "operator": route.operator,
        "date": day.isoformat(),
        "demand": level,
        "crossing_minutes": route.crossing_min,
        "wait_minutes": route.wait_min[level],
        "reservations": route.reservations,
        "schedule_url": route.schedule_url,
        "reservations_url": WSF_RESERVATIONS if route.reservations else None,
        "note": route.note,
    }


def ferry_line(ferries: list[dict] | None) -> str | None:
    """One line for the model: which boat the drive depends on, on which date."""
    if not ferries:
        return None
    parts = []
    for f in ferries:
        day = date.fromisoformat(f["date"])
        text = (f"Ferry: {f['name']} ({f['operator']}), ~{f['crossing_minutes']} min crossing; "
                f"{day.strftime('%a %b')} {day.day} is {_DEMAND_TEXT[f['demand']]}, "
                f"expect ~{f['wait_minutes']} min to board (included in the drive time)")
        if f.get("reservations"):
            text += "; vehicle reservations recommended"
        if f.get("note"):
            text += f"; {f['note']}"
        text += "; check the sailing schedule for that date"
        parts.append(text)
    return " · ".join(parts)


def score_penalty(ferries: list[dict] | None) -> float:
    if not ferries:
        return 0.0
    return max(SCORE_PENALTY[f["demand"]] for f in ferries)


def match_terminal_pair(lat1: float, lon1: float, lat2: float, lon2: float, tol_km: float = 1.5) -> FerryRoute | None:
    """The route whose terminals are these two points (either direction), e.g.
    from a HERE ferry section's departure and arrival."""
    from conditions.routing import haversine_km

    def near(p, lat, lon):
        return haversine_km(p[0], p[1], lat, lon) <= tol_km

    for r in ROUTES:
        if (near(r.a, lat1, lon1) and near(r.b, lat2, lon2)) or (near(r.b, lat1, lon1) and near(r.a, lat2, lon2)):
            return r
    return None
