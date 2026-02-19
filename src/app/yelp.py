import logging
import os

import httpx

from .models import YelpBusiness

logger = logging.getLogger(__name__)

_BASE = "https://api.yelp.com/v3"


def _term_for(category: str) -> str:
    if category == "restaurants":
        return "restaurants"
    if category == "things_to_do":
        return "things to do"
    return "restaurants"  # default for "all" — Foursquare covers activities


async def search_yelp(
    location: str, category: str
) -> tuple[list[YelpBusiness], list[str]]:
    api_key = os.getenv("YELP_API_KEY", "")
    if not api_key:
        return [], ["Yelp: set YELP_API_KEY in .env to enable restaurant results"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_BASE}/businesses/search",
                headers={"Authorization": f"Bearer {api_key}"},
                params={
                    "term": _term_for(category),
                    "location": location,
                    "limit": 10,
                    "sort_by": "rating",
                },
            )
            resp.raise_for_status()
            businesses = resp.json().get("businesses", [])

        results: list[YelpBusiness] = []
        for b in businesses:
            address = ", ".join(b.get("location", {}).get("display_address", []))
            coords = b.get("coordinates", {})
            results.append(
                YelpBusiness(
                    id=b["id"],
                    name=b["name"],
                    url=b["url"],
                    rating=b["rating"],
                    review_count=b["review_count"],
                    price=b.get("price"),
                    categories=[c["title"] for c in b.get("categories", [])],
                    address=address,
                    image_url=b.get("image_url") or None,
                    lat=coords.get("latitude") or None,
                    lon=coords.get("longitude") or None,
                )
            )
        return results, []
    except Exception as exc:
        logger.warning("Yelp search failed: %s", exc)
        return [], [f"Yelp search failed: {exc}"]
