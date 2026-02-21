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

# Google Photos Library API was deprecated March 31 2025.
# We now use the Drive API with spaces=photos, which is the supported replacement.
_SCOPES = "https://www.googleapis.com/auth/drive.photos.readonly"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_API = "https://www.googleapis.com/drive/v3"


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


def _parse_drive_photo(f: dict) -> GooglePhoto | None:
    try:
        thumbnail = f.get("thumbnailLink", "")
        if not thumbnail:
            return None
        # thumbnailLink is a signed lh3.googleusercontent.com URL — bump to 800 px wide
        thumbnail = thumbnail.split("=s")[0] + "=w800"
        meta = f.get("imageMediaMetadata", {})
        return GooglePhoto(
            id=f["id"],
            url=thumbnail,
            description=f.get("description", ""),
            timestamp=meta.get("time", ""),
            album_title=None,
            product_url=f.get("webViewLink", ""),
        )
    except Exception:
        return None


def _fetch_sync(access_token: str, location: str) -> list[GooglePhoto]:
    headers = {"Authorization": f"Bearer {access_token}"}
    location_lower = location.lower()

    try:
        r = httpx.get(
            f"{_DRIVE_API}/files",
            headers=headers,
            params={
                "spaces": "photos",
                "q": "mimeType contains 'image/'",
                "fields": "files(id,name,description,thumbnailLink,webViewLink,imageMediaMetadata)",
                "pageSize": 100,
                "orderBy": "modifiedTime desc",
            },
            timeout=20,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        logger.info("Drive photos API: %d files returned", len(files))
    except Exception as exc:
        logger.warning("Drive photos fetch failed: %s", exc)
        return []

    def _mentions(f: dict) -> bool:
        text = (f.get("description", "") + " " + f.get("name", "")).lower()
        return location_lower in text

    matched   = [f for f in files if _mentions(f)]
    unmatched = [f for f in files if not _mentions(f)]
    logger.info(
        "Drive photos for '%s': %d description-matched, %d other",
        location, len(matched), len(unmatched),
    )

    photos: list[GooglePhoto] = []
    for f in matched + unmatched:
        p = _parse_drive_photo(f)
        if p:
            photos.append(p)
        if len(photos) >= 12:
            break

    return photos


async def search_google_photos(access_token: str, location: str) -> list[GooglePhoto]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_sync, access_token, location)
