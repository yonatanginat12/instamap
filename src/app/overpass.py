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

# Persistent clients — reuse TCP connections across requests
_nominatim_client = httpx.AsyncClient(
    timeout=10, headers={"User-Agent": "discover-app/1.0"}
)
_overpass_client = httpx.AsyncClient(
    timeout=25, headers={"User-Agent": "discover-app/1.0"}
)

# Geocode cache — avoid re-hitting Nominatim for the same location
_geocode_cache: dict[str, tuple[float, float]] = {}

_EAT_TAGS = [
    '["amenity"="restaurant"]',
    '["amenity"="cafe"]',
    '["amenity"="bar"]',
    '["amenity"="pub"]',
    '["amenity"="fast_food"]',
    '["amenity"="food_court"]',
]
_DO_TAGS = [
    '["tourism"="attraction"]',
    '["tourism"="museum"]',
    '["leisure"="park"]',
    '["amenity"="theatre"]',
    '["amenity"="cinema"]',
    '["amenity"="nightclub"]',
]
_SLEEP_TAGS = [
    '["tourism"="hotel"]',
    '["tourism"="hostel"]',
    '["tourism"="guest_house"]',
    '["tourism"="motel"]',
    '["tourism"="apartment"]',
]

_EAT_AMENITY = {"restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "bistro"}
_SLEEP_TOURISM = {"hotel", "hostel", "guest_house", "motel", "apartment"}


async def _geocode(location: str) -> tuple[float, float] | None:
    key = location.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        r = await _nominatim_client.get(
            _NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
        )
        r.raise_for_status()
        results = r.json()
        if results:
            coords = float(results[0]["lat"]), float(results[0]["lon"])
            _geocode_cache[key] = coords
            return coords
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
    category: str,  # kept for API compat — we always fetch all three
) -> tuple[list[FoursquarePlace], list[FoursquarePlace], list[FoursquarePlace], list[str], tuple[float, float] | None]:
    """Returns (eat, do, sleep, warnings, coords)."""
    coords = await _geocode(location)
    if not coords:
        return [], [], [], [f"OpenStreetMap: could not geocode '{location}'"], None

    lat, lon = coords
    radius = 2000

    all_tags = _EAT_TAGS + _DO_TAGS + _SLEEP_TAGS
    query = _build_query(all_tags, lat, lon, radius)

    last_exc: Exception | None = None
    for mirror in _OVERPASS_MIRRORS:
        try:
            r = await _overpass_client.post(mirror, data={"data": query})
            r.raise_for_status()
            elements = r.json().get("elements", [])
            break
        except Exception as exc:
            logger.warning("Overpass mirror %s failed: %s", mirror, exc)
            last_exc = exc
    else:
        return [], [], [], [f"OpenStreetMap unavailable: {last_exc}"], coords

    try:
        eat_els, sleep_els, do_els = [], [], []
        for el in elements:
            tags_el = el.get("tags", {})
            if tags_el.get("amenity", "") in _EAT_AMENITY:
                eat_els.append(el)
            elif tags_el.get("tourism", "") in _SLEEP_TOURISM:
                sleep_els.append(el)
            else:
                do_els.append(el)

        return (
            _parse_elements(eat_els, limit=12),
            _parse_elements(do_els, limit=12),
            _parse_elements(sleep_els, limit=12),
            [],
            coords,
        )
    except Exception as exc:
        logger.warning("Overpass parse failed: %s", exc)
        return [], [], [], [f"OpenStreetMap parse failed: {exc}"], coords
