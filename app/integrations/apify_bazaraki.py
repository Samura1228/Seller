"""Comparable listings from Bazaraki via an Apify actor (Bazaraki blocks datacenter IPs, Apify doesn't care).

Actor: logiover/bazaraki-scraper (configurable via APIFY_BAZARAKI_ACTOR).
If the actor changes its fields, only `_normalize` needs fixing.
"""

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


class ComparablesError(Exception):
    pass


def _normalize(row: dict[str, Any]) -> dict[str, Any] | None:
    price = row.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if not price or price <= 0:
        return None
    return {
        "source": "bazaraki",
        "title": (row.get("title") or "")[:200],
        "price": price,
        "url": row.get("detailUrl") or row.get("canonicalUrl") or row.get("url"),
        "location": row.get("location") or row.get("city"),
        "negotiable": row.get("isNegotiable"),
        "posted_at": row.get("postedAt"),
    }


async def search(query: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.apify_token:
        raise ComparablesError("APIFY_TOKEN is not set")

    run_input = {
        "searchQueries": [query],
        "language": "en",
        "scrapeDetails": False,  # titles + prices are enough; detail pages cost extra
        "maxListings": settings.apify_max_listings,
        "maxPagesPerQuery": 1,
    }
    url = API.format(actor=settings.apify_bazaraki_actor)
    try:
        async with httpx.AsyncClient(timeout=settings.apify_timeout_s + 30) as client:
            resp = await client.post(
                url,
                params={"timeout": settings.apify_timeout_s},
                headers={"Authorization": f"Bearer {settings.apify_token}"},
                json=run_input,
            )
    except httpx.HTTPError as e:
        raise ComparablesError(f"Apify request failed: {e}") from e
    if resp.status_code >= 400:
        raise ComparablesError(f"Apify HTTP {resp.status_code}: {resp.text[:300]}")

    rows = resp.json()
    if not isinstance(rows, list):
        raise ComparablesError("Unexpected Apify response")
    comps = [c for c in (_normalize(r) for r in rows if isinstance(r, dict)) if c]
    log.info("Bazaraki comps for %r: %d", query, len(comps))
    return comps
