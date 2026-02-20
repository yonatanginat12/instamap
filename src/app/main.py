import asyncio
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

load_dotenv()

from .instagram import search_followee_posts, search_instagram  # noqa: E402
from .models import InstagramPost, PlacesResponse, SearchResponse  # noqa: E402
from .overpass import _geocode, search_osm  # noqa: E402

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Discover")

_ROOT = Path(__file__).parent.parent.parent

# Separate caches — 30-min TTL
_places_cache: dict[tuple[str, str], tuple[float, PlacesResponse]] = {}
_ig_cache: dict[tuple[str, str], tuple[float, list[InstagramPost]]] = {}
_followee_cache: dict[tuple[str, str], tuple[float, list[InstagramPost]]] = {}
_CACHE_TTL = 1800


async def _with_timeout(coro, timeout, default):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return default


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ROOT / "templates" / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/search/places", response_model=PlacesResponse)
async def search_places(
    location: str = Query(..., min_length=2),
    category: str = Query("all", pattern="^(all|eat|do|sleep)$"),
) -> PlacesResponse:
    """Fast endpoint: OSM only (~3-8 s). Returns location coords for map centering."""
    key = (location.lower().strip(), category)
    cached = _places_cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    # Geocode runs as a separate task so location_lat/lon is always populated
    # even if the Overpass query times out.
    geo_task = asyncio.create_task(
        _with_timeout(_geocode(location), timeout=8.0, default=None)
    )

    osm_result = await _with_timeout(
        search_osm(location, category),
        timeout=22.0,
        default=([], [], [], ["OSM: request timed out"], None),
    )
    osm_eat, osm_do, osm_sleep, osm_warn, osm_coords = osm_result

    coords = osm_coords or await geo_task

    result = PlacesResponse(
        location=location,
        category=category,
        location_lat=coords[0] if coords else None,
        location_lon=coords[1] if coords else None,
        yelp_businesses=[],
        osm_eat=osm_eat,
        osm_do=osm_do,
        osm_sleep=osm_sleep,
        warnings=osm_warn,
    )
    _places_cache[key] = (time.time(), result)
    return result


@app.get("/api/search/instagram", response_model=list[InstagramPost])
async def search_instagram_endpoint(
    location: str = Query(..., min_length=2),
    category: str = Query("all", pattern="^(all|eat|do|sleep)$"),
) -> list[InstagramPost]:
    """Slow endpoint: Instagram only. Called in parallel with /places by the frontend."""
    key = (location.lower().strip(), category)
    cached = _ig_cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    posts, _ = await _with_timeout(
        search_instagram(location, category),
        timeout=25.0,
        default=([], []),
    )
    posts.sort(key=lambda p: p.likes, reverse=True)
    if posts:  # don't cache empty — could be a timeout while executor was busy
        _ig_cache[key] = (time.time(), posts)
    return posts


@app.get("/api/search/followees", response_model=list[InstagramPost])
async def search_followees_endpoint(
    location: str = Query(..., min_length=2),
    ig_username: str = Query(..., min_length=1),
) -> list[InstagramPost]:
    """Posts from accounts the user follows, filtered by location. Public accounts only."""
    key = (ig_username.lower().strip(), location.lower().strip())
    cached = _followee_cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    posts, _ = await _with_timeout(
        search_followee_posts(ig_username, location),
        timeout=35.0,
        default=([], []),
    )
    posts.sort(key=lambda p: p.likes, reverse=True)
    if posts:  # don't cache empty — could be a timeout while executor was busy
        _followee_cache[key] = (time.time(), posts)
    return posts


# Legacy endpoint — kept for backwards compat
@app.get("/api/search", response_model=SearchResponse)
async def search(
    location: str = Query(..., min_length=2),
    category: str = Query("all", pattern="^(all|eat|do|sleep)$"),
) -> SearchResponse:
    places, ig_posts = await asyncio.gather(
        search_places(location, category),
        search_instagram_endpoint(location, category),
    )
    return SearchResponse(
        location=location,
        category=category,
        location_lat=places.location_lat,
        location_lon=places.location_lon,
        instagram_posts=ig_posts,
        yelp_businesses=places.yelp_businesses,
        osm_eat=places.osm_eat,
        osm_do=places.osm_do,
        osm_sleep=places.osm_sleep,
        warnings=places.warnings,
    )
