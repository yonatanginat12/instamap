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

_executor = ThreadPoolExecutor(max_workers=1)  # single worker: Instaloader session is not thread-safe

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
        items = _extract_media_items(raw)
        logger.info("Instagram #%s: raw sections=%d, items=%d",
                    tag, len(raw.get("top", {}).get("sections", [])), len(items))
        for m in items:
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

    seen: set[str] = set()
    all_posts: list[InstagramPost] = []
    warnings: list[str] = []

    # Try up to 3 hashtags per category; stop as soon as one returns results
    for cat in ("eat", "do", "sleep"):
        tags = _hashtags_for(location, cat)[:3]
        for tag in tags:
            fetched = _fetch_tag(tag, limit=9)
            if fetched:
                for post in fetched:
                    if post.shortcode not in seen:
                        seen.add(post.shortcode)
                        post.post_category = cat
                        all_posts.append(post)
                break  # got results — no need to try next hashtag
            logger.info("No results for #%s, trying next hashtag", tag)
        else:
            warnings.append(f"No Instagram results for {location} ({cat})")

    return all_posts, warnings


async def search_instagram(
    location: str, category: str  # category kept for API compat, ignored here
) -> tuple[list[InstagramPost], list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_sync, location)


def _fetch_followee_posts_sync(
    ig_username: str, location: str
) -> tuple[list[InstagramPost], list[str]]:
    """Two-pass search over followed accounts. Public accounts only.

    Pass 1 (free — no extra API calls): collect the first page of followees
    (~50 accounts). username and full_name come back in the same response, so
    accounts like @tokyo_eats or "Tokyo Street Food" are identified as
    priority candidates at zero extra cost.

    Pass 2: fetch posts from priority accounts first (up to 24 posts each),
    then fallback accounts (up to 6 posts each), stopping once 9 results are
    collected.
    """
    login_err = _ensure_login()
    if login_err:
        return [], [login_err]

    location_lower = location.lower()
    location_slug  = _slug(location)   # alphanumeric only, e.g. "telaviv"
    results: list[InstagramPost] = []

    try:
        ig_profile = instaloader.Profile.from_username(_loader.context, ig_username)
    except Exception as exc:
        return [], [f"Instagram user '{ig_username}' not found: {exc}"]

    # ── Pass 1: classify 50 followees using free username / full_name data ──
    # Skip private accounts immediately — the bot session can't see their posts.
    # username and full_name come back in the same followee-list response, so
    # the priority/fallback split costs zero extra API calls.
    priority: list = []
    fallback: list = []
    skipped_private = 0
    try:
        for followee in itertools.islice(ig_profile.get_followees(), 50):
            if followee.is_private:
                skipped_private += 1
                continue
            uname = followee.username.lower()
            fname = (followee.full_name or "").lower()
            if location_lower in uname or location_lower in fname \
               or location_slug in uname or location_slug in fname:
                priority.append(followee)
            else:
                fallback.append(followee)
    except Exception as exc:
        logger.warning("Error fetching followees for %s: %s", ig_username, exc)
        if not priority and not fallback:
            return [], [
                f"Could not load following list for '{ig_username}' "
                f"(account may be private or rate-limited)"
            ]

    logger.info(
        "Followee pre-filter for '%s': %d priority, %d fallback, %d private skipped",
        location, len(priority), len(fallback), skipped_private,
    )

    # ── Pass 2: check posts — bypass the location detail API ──
    # Accessing post.location triggers a secondary /explore/locations/{id}/ call
    # that returns 201 (Instagram challenge) and retries 10+ times, completely
    # blocking the thread. Instead we read the location name directly from the
    # raw post node — it carries {id, name, slug} but not lat/lng, which is fine
    # for text matching. Coordinates are left as None for followee posts.
    def _raw_location_name(post) -> str:
        try:
            loc = post._node.get("location") or {}  # type: ignore[attr-defined]
            return (loc.get("name") or "").lower()
        except Exception:
            return ""

    def _first_match(followee, max_posts: int) -> InstagramPost | None:
        try:
            posts_iter = followee.get_posts()
        except Exception as exc:
            logger.debug("get_posts failed for %s: %s", followee.username, exc)
            return None

        for _ in range(max_posts):
            try:
                post = next(posts_iter)
            except StopIteration:
                break
            except Exception as exc:
                logger.debug("post iteration error for %s: %s", followee.username, exc)
                continue

            try:
                loc_name = _raw_location_name(post)
                cap = (post.caption or "").lower()
                if location_lower not in loc_name and location_lower not in cap:
                    continue
                return InstagramPost(
                    shortcode=post.shortcode,
                    url=f"https://www.instagram.com/p/{post.shortcode}/",
                    image_url=post.url,
                    caption=(post.caption or "")[:300],
                    likes=post.likes,
                    timestamp=post.date_utc.isoformat(),
                    location_name=loc_name or None,
                    username=followee.username,
                    post_category="followee",
                    lat=None,   # coordinates require broken /explore/locations/ API
                    lon=None,
                )
            except Exception as exc:
                logger.debug("post parse error for %s: %s", followee.username, exc)
                continue

        return None

    for followee in priority:
        if len(results) >= 9:
            break
        hit = _first_match(followee, max_posts=24)
        if hit:
            results.append(hit)

    for followee in fallback:
        if len(results) >= 9:
            break
        hit = _first_match(followee, max_posts=12)
        if hit:
            results.append(hit)

    return results, []


async def search_followee_posts(
    ig_username: str, location: str
) -> tuple[list[InstagramPost], list[str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _fetch_followee_posts_sync, ig_username, location
    )
