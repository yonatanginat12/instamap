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
from .models import SearchResponse  # noqa: E402
from .overpass import search_osm  # noqa: E402
from .yelp import search_yelp  # noqa: E402

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Discover")

# Simple in-memory cache — keyed by (location_lower, category), TTL 30 min
_cache: dict[tuple[str, str], tuple[float, SearchResponse]] = {}
_CACHE_TTL = 1800  # seconds

_ROOT = Path(__file__).parent.parent.parent


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ROOT / "templates" / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/search", response_model=SearchResponse)
async def search(
    location: str = Query(..., min_length=2, description="City or neighborhood"),
    category: str = Query("all", pattern="^(all|restaurants|things_to_do)$"),
) -> SearchResponse:
    key = (location.lower().strip(), category)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    (posts, ig_warn), (yelp_biz, yelp_warn), osm_result = await asyncio.gather(
        search_instagram(location, category),
        search_yelp(location, category),
        search_osm(location, category),
    )
    osm_restaurants, osm_activities, osm_warn = osm_result

    # Sort by popularity
    posts.sort(key=lambda p: p.likes, reverse=True)
    yelp_biz.sort(
        key=lambda b: b.rating * math.log10(b.review_count + 1), reverse=True
    )
    # OSM has no popularity signal — keep original order

    result = SearchResponse(
        location=location,
        category=category,
        instagram_posts=posts,
        yelp_businesses=yelp_biz,
        osm_restaurants=osm_restaurants,
        osm_activities=osm_activities,
        warnings=ig_warn + yelp_warn + osm_warn,
    )
    _cache[key] = (time.time(), result)
    return result
