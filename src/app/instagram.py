import asyncio
import base64
import itertools
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import instaloader

from .models import InstagramPost

logger = logging.getLogger(__name__)


def _bootstrap_session() -> None:
    """On first boot (e.g. Railway), write the session file from the
    INSTAGRAM_SESSION_B64 env var so Instaloader can load it normally."""
    b64 = os.getenv("INSTAGRAM_SESSION_B64", "").strip()
    if not b64:
        return
    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    if not username:
        return
    session_dir = Path.home() / ".config" / "instaloader"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"session-{username}"
    session_file.write_bytes(base64.b64decode(b64))
    logger.info("Instagram session written from INSTAGRAM_SESSION_B64")


_bootstrap_session()

_executor = ThreadPoolExecutor(max_workers=1)  # single worker to respect rate limits

_loader = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    save_metadata=False,
    compress_json=False,
    quiet=True,
    request_timeout=15,
)

_logged_in = False


def _ensure_login() -> str | None:
    """Load session from file (preferred) or fall back to password login."""
    global _logged_in
    if _logged_in:
        return None
    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    if not username:
        return "Instagram: set INSTAGRAM_USERNAME in .env"

    # Preferred: load browser-imported session (avoids 403 on hashtag endpoints)
    try:
        _loader.load_session_from_file(username)
        verified = _loader.test_login()
        if verified:
            _logged_in = True
            logger.info("Instaloader session loaded for %s", verified)
            return None
        logger.warning("Session file invalid, falling back to password login")
    except FileNotFoundError:
        logger.info("No session file found, trying password login")
    except Exception as exc:
        logger.warning("Session load failed: %s", exc)

    # Fallback: password login
    password = os.getenv("INSTAGRAM_PASSWORD", "").strip()
    if not password:
        return (
            "Instagram session not found. Run: "
            "python import_instagram_session.py  "
            "(requires Firefox logged into Instagram)"
        )
    try:
        _loader.login(username, password)
        _loader.save_session_to_file()
        _logged_in = True
        logger.info("Instaloader logged in as %s", username)
        return None
    except Exception as exc:
        logger.error("Instaloader login failed: %s", exc)
        return f"Instagram login failed: {exc}"


def _slug(location: str) -> str:
    return re.sub(r"[^a-z0-9]", "", location.lower())


def _hashtags_for(location: str, category: str) -> list[str]:
    s = _slug(location)
    tags: list[str] = []
    if category in ("eat", "all"):
        # Ordered by quality: foodie self-selects engaged food audiences
        tags += [f"{s}food", f"{s}foodie", f"{s}eats", f"{s}cafe"]
    if category in ("do", "all"):
        # visit{city} used by tourism boards; {city}life broad lifestyle tag
        tags += [f"{s}", f"visit{s}", f"thingstodoin{s}", f"{s}life"]
    if category in ("sleep", "all"):
        # hotel first, then broader accommodation patterns
        tags += [f"{s}hotel", f"{s}hotels", f"{s}airbnb", f"{s}stay"]
    seen: set[str] = set()
    return [t for t in tags if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]


def _extract_media_items(raw: dict) -> list[dict]:
    """Parse Instagram's current hashtag response structure."""
    items: list[dict] = []
    for sec in raw.get("top", {}).get("sections", []):
        lc = sec.get("layout_content", {})
        for key in ("fill_items", "medias"):
            for item in lc.get(key, []):
                if "media" in item:
                    items.append(item["media"])
        one = lc.get("one_by_two_item", {})
        if "media" in one:
            items.append(one["media"])
    return items


def _parse_post(m: dict) -> InstagramPost | None:
    code = m.get("code")
    if not code:
        return None

    # caption
    cap_obj = m.get("caption") or {}
    caption = cap_obj.get("text", "") if isinstance(cap_obj, dict) else ""

    # image URL — prefer the largest candidate
    image_url = ""
    iv2 = m.get("image_versions2", {})
    candidates = iv2.get("candidates", [])
    if candidates:
        image_url = candidates[0].get("url", "")

    # user
    user = m.get("user") or {}
    username = user.get("username", "")

    # location + coordinates
    loc = m.get("location") or {}
    location_name = loc.get("name") if loc else None
    lat = loc.get("lat") or None
    lon = loc.get("lng") or None  # Instagram uses "lng"

    return InstagramPost(
        shortcode=code,
        url=f"https://www.instagram.com/p/{code}/",
        image_url=image_url,
        caption=caption[:300],
        likes=m.get("like_count", 0),
        timestamp=str(m.get("taken_at", "")),
        location_name=location_name,
        username=username,
        lat=lat,
        lon=lon,
    )


def _fetch_tag(tag: str, limit: int) -> list[InstagramPost]:
    posts: list[InstagramPost] = []
    try:
        hashtag = instaloader.Hashtag.from_name(_loader.context, tag)
        raw = hashtag._metadata()
        for m in _extract_media_items(raw):
            post = _parse_post(m)
            if post:
                posts.append(post)
            if len(posts) >= limit:
                break
    except Exception as exc:
        logger.warning("Instagram fetch failed for #%s: %s", tag, exc)
    return posts


def _search_sync(location: str) -> tuple[list[InstagramPost], list[str]]:
    login_err = _ensure_login()
    if login_err:
        return [], [login_err]

    # Always fetch both categories so the UI can show both sections
    plan: list[tuple[str, str]] = [
        *[(t, "eat")   for t in _hashtags_for(location, "eat")[:1]],
        *[(t, "do")    for t in _hashtags_for(location, "do")[:1]],
        *[(t, "sleep") for t in _hashtags_for(location, "sleep")[:1]],
    ]

    seen: set[str] = set()
    all_posts: list[InstagramPost] = []
    warnings: list[str] = []

    for tag, cat in plan:
        fetched = _fetch_tag(tag, limit=9)
        if not fetched:
            warnings.append(f"No Instagram results for #{tag}")
        for post in fetched:
            if post.shortcode not in seen:
                seen.add(post.shortcode)
                post.post_category = cat
                all_posts.append(post)

    return all_posts, warnings


async def search_instagram(
    location: str, category: str  # category kept for API compat, ignored here
) -> tuple[list[InstagramPost], list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_sync, location)


def _fetch_followee_posts_sync(
    ig_username: str, location: str
) -> tuple[list[InstagramPost], list[str]]:
    """Search recent posts from followed accounts for location. Public accounts only."""
    login_err = _ensure_login()
    if login_err:
        return [], [login_err]

    location_lower = location.lower()
    results: list[InstagramPost] = []

    try:
        ig_profile = instaloader.Profile.from_username(_loader.context, ig_username)
    except Exception as exc:
        return [], [f"Instagram user '{ig_username}' not found: {exc}"]

    followees_checked = 0
    try:
        for followee in ig_profile.get_followees():
            if followees_checked >= 20 or len(results) >= 9:
                break
            followees_checked += 1
            try:
                for post in itertools.islice(followee.get_posts(), 6):
                    loc_match = (
                        post.location and location_lower in post.location.name.lower()
                    ) or (
                        post.caption and location_lower in post.caption.lower()
                    )
                    if loc_match:
                        results.append(InstagramPost(
                            shortcode=post.shortcode,
                            url=f"https://www.instagram.com/p/{post.shortcode}/",
                            image_url=post.url,
                            caption=(post.caption or "")[:300],
                            likes=post.likes,
                            timestamp=post.date_utc.isoformat(),
                            location_name=post.location.name if post.location else None,
                            username=followee.username,
                            post_category="followee",
                            lat=post.location.lat if post.location else None,
                            lon=post.location.lng if post.location else None,
                        ))
                        break  # one match per followee keeps results diverse
            except Exception as exc:
                logger.debug("Skipping followee %s: %s", followee.username, exc)
    except Exception as exc:
        logger.warning("Error fetching followees for %s: %s", ig_username, exc)
        if not results:
            return [], [
                f"Could not load following list for '{ig_username}' "
                f"(account may be private or rate-limited)"
            ]

    return results, []


async def search_followee_posts(
    ig_username: str, location: str
) -> tuple[list[InstagramPost], list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _fetch_followee_posts_sync, ig_username, location
    )
