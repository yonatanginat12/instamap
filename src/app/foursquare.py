import logging
import os

import httpx

from .models import FoursquarePlace

logger = logging.getLogger(__name__)

_BASE = "https://api.foursquare.com/v3"


def _query_for(category: str) -> str:
    if category == "restaurants":
        return "restaurants"
    if category == "things_to_do":
        return "things to do"
    return "things to do"  # default for "all" — Yelp covers restaurants


async def search_foursquare(
    location: str, category: str
) -> tuple[list[FoursquarePlace], list[str]]:
    api_key = os.getenv("FOURSQUARE_API_KEY", "")
    if not api_key:
        return [], [
            "Foursquare: set FOURSQUARE_API_KEY in .env to enable place results"
        ]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_BASE}/places/search",
                headers={"Authorization": api_key, "Accept": "application/json"},
                params={"query": _query_for(category), "near": location, "limit": 10},
            )
            resp.raise_for_status()
            raw = resp.json().get("results", [])

        places: list[FoursquarePlace] = []
        for p in raw:
            cats = [c.get("name", "") for c in p.get("categories", [])]
            loc = p.get("location", {})
            address = loc.get("formatted_address") or loc.get("address") or ""
            places.append(
                FoursquarePlace(
                    fsq_id=p["fsq_id"],
                    name=p["name"],
                    categories=cats,
                    address=address,
                    distance=p.get("distance"),
                    link=f"https://foursquare.com/v/{p['fsq_id']}",
                )
            )
        return places, []
    except Exception as exc:
        logger.warning("Foursquare search failed: %s", exc)
        return [], [f"Foursquare search failed: {exc}"]
