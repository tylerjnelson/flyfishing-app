"""
NPS alerts fetcher — 2-hour interval via APScheduler.

Fetches active alerts for North Cascades (noca) and Olympic (olym) national parks
from the NPS Developer API. Park closures and warnings may affect access to fishing
spots within or adjacent to these parks.

Alerts are stored globally (spot_id=None) in conditions_cache as source='nps_alerts'.
The Phase 5 context_builder reads this cache entry and surfaces relevant park warnings
to the LLM as part of the conditions summary.

API: https://developer.nps.gov/api/v1/alerts
Key: NPS_API_KEY env var (optional; defaults to DEMO_KEY at 50 req/hr)
Parks: noca (North Cascades), olym (Olympic National Park)
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import nps_breaker
from config import settings

log = logging.getLogger(__name__)

_ALERTS_URL = "https://developer.nps.gov/api/v1/alerts"
_PARK_CODES = "noca,olym"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)


async def fetch_nps_alerts() -> dict | None:
    """
    Fetch active NPS alerts for NOCA and OLYM parks.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        alerts = await _fetch()
        return _normalize(alerts)
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "nps_alerts"})
        return None


@nps_breaker
async def _fetch() -> list[dict]:
    """Fetch all pages of NPS alerts for configured parks."""
    all_alerts: list[dict] = []
    start = 0
    limit = 50

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            resp = await client.get(
                _ALERTS_URL,
                params={
                    "parkCode": _PARK_CODES,
                    "limit": limit,
                    "start": start,
                    "api_key": settings.nps_api_key,
                },
            )
            resp.raise_for_status()
            payload = resp.json()

            data = payload.get("data", [])
            all_alerts.extend(data)

            total = int(payload.get("total", "0"))
            if start + limit >= total:
                break
            start += limit

    return all_alerts


def _normalize(alerts: list[dict]) -> dict:
    """
    Normalise NPS alert records into a conditions_cache-compatible payload.

    Splits by park for easy filtering in the context_builder.
    Keeps only fields relevant to trip planning.
    """
    now = datetime.now(tz=timezone.utc).isoformat()

    by_park: dict[str, list[dict]] = {"noca": [], "olym": []}

    for alert in alerts:
        park = (alert.get("parkCode") or "").lower()
        if park not in by_park:
            continue
        by_park[park].append({
            "id": alert.get("id"),
            "title": alert.get("title", ""),
            "description": alert.get("description", ""),
            "category": alert.get("category", ""),
            "url": alert.get("url", ""),
        })

    return {
        "parks": {
            "noca": {
                "name": "North Cascades National Park",
                "alert_count": len(by_park["noca"]),
                "alerts": by_park["noca"],
            },
            "olym": {
                "name": "Olympic National Park",
                "alert_count": len(by_park["olym"]),
                "alerts": by_park["olym"],
            },
        },
        "fetched_at": now,
    }
