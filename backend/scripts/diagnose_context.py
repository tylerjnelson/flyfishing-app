"""
Diagnostic: run build_context() against a real trip and print the assembled
LLM system message. Shows exactly what data the pipeline pulls at recommendation time.

Usage (from backend/):
  sudo /opt/flyfish/venv/bin/python scripts/diagnose_context.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv("/etc/flyfish/app.env")

import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

from sqlalchemy import select
from db.connection import AsyncSessionLocal
from db.models import Conversation, FishingSpot, Trip, User, WaterBody
import chat.context_builder as _cb_module

# Patch out real-time HTTP fetch — DNS resolution fails in sudo subprocess context
# but works fine in the live service. All other pipeline steps use cached DB data.
_cb_module._fetch_and_cache_realtime = lambda *a, **kw: __import__('asyncio').sleep(0)

from chat.context_builder import build_context


TRIP_ID = "51ba084c-6d8f-4322-831a-553d84dcf364"
CONV_ID = "01ebebac-48b0-4513-a17c-2d74af47f3c0"


async def main():
    async with AsyncSessionLocal() as db:
        trip = (await db.execute(select(Trip).where(Trip.id == TRIP_ID))).scalar_one()
        conv = (await db.execute(select(Conversation).where(Conversation.id == CONV_ID))).scalar_one()
        user = (await db.execute(select(User).where(User.id == trip.user_id))).scalar_one()

        print("=" * 70)
        print(f"USER:       {user.email}")
        print(f"TRIP STATE: {trip.state}")
        print(f"INTAKE:     {trip.session_intake}")
        print("=" * 70)

        # Force pipeline re-run so we see the current candidate set
        result = await build_context(
            user=user,
            trip=trip,
            conversation=conv,
            query="Where should I go fly fishing this weekend?",
            db=db,
            force_rerun=True,
        )

    print("\n--- CACHE HIT ---" if result.cached_response else "\n--- LLM MESSAGES ---")
    if result.cached_response:
        print(result.cached_response[:500])
        return

    print(f"\nTotal messages: {len(result.messages)}")
    for i, msg in enumerate(result.messages):
        role = msg['role'].upper()
        content = msg['content']
        print(f"\n[{i}] {role} ({len(content)} chars)")
        if role == "SYSTEM":
            print(content)
        else:
            print(content[:300])

    candidates = result.session_candidates.get("candidates", [])
    print(f"\n--- CANDIDATES ({len(candidates)} total, top 5 shown) ---")
    for c in candidates[:5]:
        conds = c.get("conditions") or {}
        usgs = conds.get("usgs") or {}
        nws = conds.get("noaa_nws") or {}
        nwrfc = conds.get("noaa_nwrfc") or {}
        wta = conds.get("wta") or {}
        snotel = conds.get("snotel") or {}
        airnow = conds.get("airnow") or {}
        print(
            f"  [{c['session_score']:+.2f}] {c['water_body_name']}"
            f" ({c['spot_type']}, {c.get('drive_minutes', '?')} min)"
            f" | usgs={'Y' if usgs else 'N'}"
            f" nws={'Y' if nws else 'N'}"
            f" nwrfc={'Y' if nwrfc else 'N'}"
            f" wta={'Y' if wta else 'N'}"
            f" snotel={'Y' if snotel else 'N'}"
            f" airnow={'Y' if airnow else 'N'}"
        )
        if c.get("warnings"):
            for w in c["warnings"]:
                print(f"    ⚠ {w}")


if __name__ == "__main__":
    asyncio.run(main())
