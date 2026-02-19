import logging

import httpx

from .models import FoursquarePlace  # reuse same model shape

logger = logging.getLogger(__name__)

_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_RESTAURANT_TAGS = [
    '["amenity"="restaurant"]',
    '["amenity"="cafe"]',
    '["amenity"="bar"]',
]
_ACTIVITY_TAGS = [
    '["tourism"="attraction"]',
    '["tourism"="museum"]',
    '["leisure"="park"]',
    '["amenity"="theatre"]',
]


async def _geocode(location: str) -> tuple[float, float] | None:
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": "discover-app/1.0"}
        ) as client:
            r = await client.get(
                _NOMINATIM_URL,
                params={"q": location, "format": "json", "limit": 1},
            )
            r.raise_for_status()
            results = r.json()
            if results:
                return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as exc:
        logger.warning("Nominatim geocode failed: %s", exc)
    return None


def _build_query(tags: list[str], lat: float, lon: float, radius: int) -> str:
    parts = "\n".join(
        f'  node{t}(around:{radius},{lat},{lon});\n'
        f'  way{t}(around:{radius},{lat},{lon});'
        for t in tags
    )
    return f"[out:json][timeout:20];\n(\n{parts}\n);\nout center 20;"


def _parse_elements(elements: list[dict], limit: int) -> list[FoursquarePlace]:
    places: list[FoursquarePlace] = []
    seen: set[str] = set()
    for el in elements:
        tags_el = el.get("tags", {})
        name = tags_el.get("name") or tags_el.get("name:en")
        if not name or name in seen:
            continue
        seen.add(name)

        cats = [
            tags_el[k].replace("_", " ").title()
            for k in ("amenity", "tourism", "leisure", "shop")
            if k in tags_el
        ]
        addr_parts = [
            tags_el.get("addr:housenumber", ""),
            tags_el.get("addr:street", ""),
            tags_el.get("addr:city", ""),
        ]
        address = ", ".join(p for p in addr_parts if p) or None
        el_type = el.get("type", "node")
        link = f"https://www.openstreetmap.org/{el_type}/{el.get('id')}"

        # coordinates: nodes have lat/lon directly; ways have center
        if el_type == "node":
            lat = el.get("lat")
            lon = el.get("lon")
        else:
            center = el.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        places.append(
            FoursquarePlace(
                fsq_id=str(el.get("id")),
                name=name,
                categories=cats,
                address=address,
                link=link,
                lat=lat,
                lon=lon,
            )
        )
        if len(places) >= limit:
            break
    return places


async def search_osm(
    location: str,
    category: str,  # kept for API compat — we always fetch both
) -> tuple[list[FoursquarePlace], list[FoursquarePlace], list[str]]:
    """Returns (restaurant_places, activity_places, warnings)."""
    coords = await _geocode(location)
    if not coords:
        return [], [], [f"OpenStreetMap: could not geocode '{location}'"]

    lat, lon = coords
    radius = 3000

    # Fetch restaurants and activities in a single Overpass query
    all_tags = _RESTAURANT_TAGS + _ACTIVITY_TAGS
    query = _build_query(all_tags, lat, lon, radius)

    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        timeout=25, headers={"User-Agent": "discover-app/1.0"}
    ) as client:
        for mirror in _OVERPASS_MIRRORS:
            try:
                r = await client.post(mirror, data={"data": query})
                r.raise_for_status()
                elements = r.json().get("elements", [])
                break
            except Exception as exc:
                logger.warning("Overpass mirror %s failed: %s", mirror, exc)
                last_exc = exc
        else:
            return [], [], [f"OpenStreetMap unavailable: {last_exc}"]

    try:
        _FOOD_VALS = {"restaurant", "cafe", "bar", "fast_food", "pub", "bistro"}
        restaurant_els = [
            el for el in elements
            if el.get("tags", {}).get("amenity", "") in _FOOD_VALS
        ]
        activity_els = [
            el for el in elements
            if el.get("tags", {}).get("amenity", "") not in _FOOD_VALS
        ]
        return (
            _parse_elements(restaurant_els, limit=12),
            _parse_elements(activity_els, limit=12),
            [],
        )
    except Exception as exc:
        logger.warning("Overpass parse failed: %s", exc)
        return [], [], [f"OpenStreetMap parse failed: {exc}"]
