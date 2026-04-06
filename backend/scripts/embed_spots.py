"""
Generate name_embedding for all spots that lack one — Phase 3.

Embeds: name + (aliases joined by space) + county  via nomic-embed-text (Ollama).
Updates spots.name_embedding in place.  Idempotent — skips spots that already
have an embedding.

Usage (from backend/ directory):
  python -m scripts.embed_spots
  python -m scripts.embed_spots --dry-run   # count only, no writes

Prerequisites:
  - Ollama running with nomic-embed-text model pulled
  - spots table seeded (run seed_spots.py first)
  - DATABASE_URL set in environment or /etc/flyfish/app.env loaded
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _build_embed_text(spot) -> str:
    """Concatenate name, aliases, and county into a single embedding string."""
    parts = [spot.name]
    if spot.aliases:
        parts.extend(spot.aliases)
    if spot.county:
        parts.append(spot.county)
    return " ".join(parts)


async def _run(dry_run: bool) -> None:
    from sqlalchemy import select

    from db.connection import AsyncSessionLocal
    from db.models import Spot
    from rag.embedder import embed_text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Spot).where(Spot.name_embedding.is_(None))
        )
        spots = list(result.scalars().all())

    log.info("spots_without_embedding", extra={"count": len(spots)})

    if dry_run:
        log.info("dry_run — no writes")
        return

    embedded = 0
    failed = 0
    for spot in spots:
        text = _build_embed_text(spot)
        try:
            embedding = await embed_text(text)
        except Exception as exc:
            log.warning(
                "embed_failed",
                extra={"spot_id": str(spot.id), "name": spot.name, "error": str(exc)},
            )
            failed += 1
            continue

        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(Spot).where(Spot.id == spot.id)
                )
                s = result.scalar_one()
                s.name_embedding = embedding

        embedded += 1
        if embedded % 10 == 0:
            log.info("embed_progress", extra={"embedded": embedded, "remaining": len(spots) - embedded - failed})

    log.info(
        "embed_complete",
        extra={"embedded": embedded, "failed": failed, "total": len(spots)},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate name_embedding for spots")
    parser.add_argument("--dry-run", action="store_true", help="Count only, no writes")
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
