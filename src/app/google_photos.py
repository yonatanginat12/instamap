import asyncio
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx

from .models import GooglePhoto

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2)

_SCOPES = "https://www.googleapis.com/auth/photoslibrary.readonly"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://photoslibrary.googleapis.com/v1"


def get_auth_url(redirect_uri: str) -> str:
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return _AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code → access + refresh tokens."""
    resp = httpx.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": int(time.time()) + data.get("expires_in", 3600),
    }


def do_refresh(refresh_tok: str) -> dict:
    """Exchange a refresh token for a new access token."""
    resp = httpx.post(
        _TOKEN_URL,
        data={
            "refresh_token": refresh_tok,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "expires_at": int(time.time()) + data.get("expires_in", 3600),
    }


def _parse_photo(item: dict, album_title: str | None = None) -> GooglePhoto | None:
    try:
        meta = item.get("mediaMetadata", {})
        # Only include photos (not videos)
        if "photo" not in meta:
            return None
        return GooglePhoto(
            id=item["id"],
            url=item["baseUrl"] + "=w800",
            description=item.get("description", ""),
            timestamp=meta.get("creationTime", ""),
            album_title=album_title,
            product_url=item.get("productUrl", ""),
        )
    except Exception:
        return None


def _list_all_albums(headers: dict, endpoint: str) -> list[dict]:
    """Paginate through all albums (user or shared) and return the full list."""
    albums: list[dict] = []
    page_token: str | None = None
    for _ in range(10):  # max 10 pages × 50 = 500 albums
        params: dict = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        try:
            r = httpx.get(f"{_API_BASE}/{endpoint}", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("Google Photos %s page failed: %s", endpoint, exc)
            break
        key = "sharedAlbums" if endpoint == "sharedAlbums" else "albums"
        albums.extend(data.get(key, []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def _fetch_sync(access_token: str, location: str) -> list[GooglePhoto]:
    headers = {"Authorization": f"Bearer {access_token}"}
    location_lower = location.lower()
    seen: set[str] = set()
    album_photos: list[GooglePhoto] = []
    travel_photos: list[GooglePhoto] = []

    # ── Pass 1: search user albums + shared/trip albums by title ──
    all_albums: list[dict] = []
    for endpoint in ("albums", "sharedAlbums"):
        all_albums.extend(_list_all_albums(headers, endpoint))

    seen_ids: set[str] = set()
    unique_albums = [a for a in all_albums if not (a["id"] in seen_ids or seen_ids.add(a["id"]))]  # type: ignore[func-returns-value]

    matching = [a for a in unique_albums if location_lower in a.get("title", "").lower()]
    logger.info(
        "Google Photos: %d total albums, %d match '%s'. Sample: %s",
        len(unique_albums), len(matching), location,
        [a.get("title", "") for a in unique_albums[:15]],
    )

    for album in matching[:5]:
        try:
            r = httpx.post(
                f"{_API_BASE}/mediaItems:search",
                headers=headers,
                json={"albumId": album["id"], "pageSize": 25},
                timeout=15,
            )
            r.raise_for_status()
            for item in r.json().get("mediaItems", []):
                p = _parse_photo(item, album.get("title"))
                if p and p.id not in seen:
                    seen.add(p.id)
                    album_photos.append(p)
        except Exception as exc:
            logger.warning("Google Photos album '%s' failed: %s", album.get("title"), exc)

    # ── Pass 2: fetch all travel/landmark photos, prioritise ones that mention the location ──
    try:
        r = httpx.post(
            f"{_API_BASE}/mediaItems:search",
            headers=headers,
            json={
                "pageSize": 100,
                "filters": {
                    "contentFilter": {
                        "includedContentCategories": [
                            "LANDMARKS", "CITYSCAPES", "TRAVEL",
                            "LANDSCAPES", "ARCHITECTURE",
                        ]
                    },
                    "includeArchivedMedia": True,
                },
            },
            timeout=20,
        )
        r.raise_for_status()
        for item in r.json().get("mediaItems", []):
            p = _parse_photo(item)
            if p and p.id not in seen:
                seen.add(p.id)
                travel_photos.append(p)
        logger.info("Google Photos travel pool: %d photos", len(travel_photos))
    except Exception as exc:
        logger.warning("Google Photos travel fetch failed: %s", exc)

    # Sort travel photos: those whose description/filename mentions the location come first
    def _mentions(p: GooglePhoto) -> bool:
        return location_lower in (p.description + " " + (p.album_title or "")).lower()

    location_matched = [p for p in travel_photos if _mentions(p)]
    rest             = [p for p in travel_photos if not _mentions(p)]
    logger.info(
        "Google Photos: %d album, %d description-matched travel, %d other travel",
        len(album_photos), len(location_matched), len(rest),
    )

    combined = album_photos + location_matched + rest
    return combined[:12]


async def search_google_photos(access_token: str, location: str) -> list[GooglePhoto]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_sync, access_token, location)
