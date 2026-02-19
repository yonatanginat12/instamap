import asyncio
import logging
import math
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

load_dotenv()

from .instagram import search_instagram  # noqa: E402
from .models import InstagramPost, PlacesResponse, SearchResponse  # noqa: E402
from .overpass import search_osm  # noqa: E402
from .yelp import search_yelp  # noqa: E402

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Discover")

_ROOT = Path(__file__).parent.parent.parent

# Separate caches for fast places and instagram — 30-min TTL
_places_cache: dict[tuple[str, str], tuple[float, PlacesResponse]] = {}
_ig_cache: dict[tuple[str, str], tuple[float, list[InstagramPost]]] = {}
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
    category: str = Query("all", pattern="^(all|restaurants|things_to_do)$"),
) -> PlacesResponse:
    """Fast endpoint: Yelp + OSM only (~3-8 s). Returns location coords for map centering."""
    key = (location.lower().strip(), category)
    cached = _places_cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    (yelp_biz, yelp_warn), osm_result = await asyncio.gather(
        _with_timeout(
            search_yelp(location, category),
            timeout=10.0,
            default=([], ["Yelp: request timed out"]),
        ),
        _with_timeout(
            search_osm(location, category),
            timeout=22.0,
            default=([], [], ["OSM: request timed out"], None),
        ),
    )
    osm_restaurants, osm_activities, osm_warn, coords = osm_result

    yelp_biz.sort(
        key=lambda b: b.rating * math.log10(b.review_count + 1), reverse=True
    )

    result = PlacesResponse(
        location=location,
        category=category,
        location_lat=coords[0] if coords else None,
        location_lon=coords[1] if coords else None,
        yelp_businesses=yelp_biz,
        osm_restaurants=osm_restaurants,
        osm_activities=osm_activities,
        warnings=yelp_warn + osm_warn,
    )
    _places_cache[key] = (time.time(), result)
    return result


@app.get("/api/search/instagram", response_model=list[InstagramPost])
async def search_instagram_endpoint(
    location: str = Query(..., min_length=2),
    category: str = Query("all", pattern="^(all|restaurants|things_to_do)$"),
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
    _ig_cache[key] = (time.time(), posts)
    return posts


# Legacy endpoint — kept for backwards compat
@app.get("/api/search", response_model=SearchResponse)
async def search(
    location: str = Query(..., min_length=2),
    category: str = Query("all", pattern="^(all|restaurants|things_to_do)$"),
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
        osm_restaurants=places.osm_restaurants,
        osm_activities=places.osm_activities,
        warnings=places.warnings,
    )
