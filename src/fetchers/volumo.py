"""
Volumo source fetcher — curated new-release API.

Uses the semi-documented Volumo REST API at https://volumo.com/api/v1/.
Discovered via Next.js inspection; API has been stable since at least 2025.

Key behaviours:
- One request per internal TuneFinder tag (all Volumo genre IDs for that tag batched)
- Pagination: up to 3 pages of `limit_per_genre` items per tag
- curation key omitted from filter JSON when not set in config
- release_start_at validated (2020 <= year <= current+1); falls back to first_live
- VOLUMO_API_KEY optional — browsing works unauthenticated
"""
import json
import os
import re
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://volumo.com/api/v1"
_MAX_PAGES = 3


# ---------------------------------------------------------------------------
# URL and slug helpers
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"--+", "-", s)
    return s.strip("-")


def _track_link(track_id: int, title: str, version: str | None, api_slug: str | None = None) -> str:
    if api_slug:
        return f"https://volumo.com/track/{track_id}-{api_slug}"
    parts = [title]
    if version:
        parts.append(version)
    slug = _slugify(" ".join(parts))
    return f"https://volumo.com/track/{track_id}-{slug}"


def _build_url(filter_obj: dict, sort: str, limit: int, offset: int) -> str:
    encoded = urllib.parse.quote(json.dumps(filter_obj, separators=(",", ":")))
    return f"{_BASE}/albums?sort={sort}&limit={limit}&offset={offset}&filter={encoded}"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _is_valid_date(date_str: str | None) -> bool:
    if not date_str:
        return False
    try:
        year = int(str(date_str)[:4])
        current_year = datetime.now(timezone.utc).year
        return 2020 <= year <= current_year + 1
    except (ValueError, TypeError):
        return False


def _extract_release_date(track: dict, album: dict) -> str | None:
    """Return YYYY-MM-DD from track/album data, or None if both sources are invalid."""
    for candidate in (
        track.get("release_start_at"),
        album.get("release_start_at"),
        album.get("first_live"),
    ):
        if _is_valid_date(candidate):
            return str(candidate)[:10]
    return None


# ---------------------------------------------------------------------------
# JSON fetch helper (isolated for easy mocking in tests)
# ---------------------------------------------------------------------------

def _get_json(url: str, session: requests.Session) -> list | dict:
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Track parsing
# ---------------------------------------------------------------------------

def _parse_artist(artists: list) -> str:
    names = [a.get("name", "") for a in (artists or []) if a.get("name")]
    return ", ".join(names)


def _parse_track(track: dict, album: dict, tag: str, genre_ids: set[int]) -> SourceItem | None:
    track_id = track.get("id")
    title = (track.get("title") or "").strip()
    if not track_id or not title:
        return None

    # Album-level genre filter can include tracks from other genres on compilation albums.
    # Reject any track whose own genre_id isn't one we actually queried for this tag.
    track_genre_id = track.get("genre_id")
    if track_genre_id is not None and track_genre_id not in genre_ids:
        logger.debug(
            f"[volumo] skipping track {track_id} '{title}': genre_id={track_genre_id} not in {tag} genres {genre_ids}"
        )
        return None

    artist = _parse_artist(track.get("artists") or [])
    if not artist:
        return None

    version = track.get("version") or None
    api_slug = track.get("slug") or None
    link = _track_link(track_id, title, version, api_slug)

    release_date = _extract_release_date(track, album)
    if release_date is None:
        return None

    label_obj = track.get("recordlabel") or album.get("recordlabel") or {}
    label = label_obj.get("name") or None
    label_id = label_obj.get("id") or None

    return SourceItem(
        source="volumo",
        artist=artist,
        title=title,
        link=link,
        label=label,
        release_date=release_date,
        release_name=album.get("title") or None,
        genre_tags=[tag],
        raw_metadata={
            "volumo_track_id": track_id,
            "volumo_album_id": album.get("id"),
            "bpm": track.get("bpm"),
            "keysign": track.get("keysign"),
            "version": version,
            "isrc": track.get("isrc"),
            "catalog_number": album.get("catalog_number"),
            "duration_ms": track.get("duration"),
            "label_name": label,
            "label_id": label_id,
            "volumo_genre_id": track_genre_id,
        },
    )


# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------

def fetch(settings, target_genre: str | None = None,
          bpm_ranges: list[tuple[float, float]] | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("volumo")
    if not cfg.get("enabled", False):
        return []

    sort = cfg.get("sort", "purchase")
    curation = cfg.get("curation") or None
    lookback_days = cfg.get("lookback_days", 28)
    limit = cfg.get("limit_per_genre", 50)
    genres_cfg: list[dict] = cfg.get("genres", [])

    release_start_from = (date.today() - timedelta(days=lookback_days)).isoformat()

    tag_to_ids: dict[str, list[int]] = defaultdict(list)
    for entry in genres_cfg:
        tag = entry.get("name", "")
        genre_id = entry.get("id")
        if not tag or genre_id is None:
            continue
        if genre_id not in tag_to_ids[tag]:
            tag_to_ids[tag].append(genre_id)

    if target_genre is not None:
        tag_to_ids = {k: v for k, v in tag_to_ids.items() if k == target_genre}

    api_key = os.environ.get("VOLUMO_API_KEY", "")
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    if api_key:
        session.headers["Authorization"] = f"Bearer {api_key}"

    all_items: list[SourceItem] = []

    for tag, genre_ids in tag_to_ids.items():
        filter_obj: dict = {
            "genres": genre_ids,
            "release_start_from": release_start_from,
        }
        if curation:
            filter_obj["curation"] = curation

        tag_items: list[SourceItem] = []
        page = 0
        while page < _MAX_PAGES:
            offset = page * limit
            url = _build_url(filter_obj, sort, limit, offset)
            logger.info(f"[volumo] {tag}: page {page + 1} — {url}")
            try:
                data = _get_json(url, session)
            except Exception as e:
                logger.warning(f"[volumo] {tag} page {page + 1} failed: {e}")
                break

            albums = data if isinstance(data, list) else (data.get("data") or data.get("items") or [])
            page += 1

            genre_ids_set = set(genre_ids)
            for album in albums:
                for track in (album.get("tracks") or []):
                    item = _parse_track(track, album, tag, genre_ids_set)
                    if item:
                        tag_items.append(item)

            if len(albums) < limit:
                break

        logger.info(f"[volumo] {tag}: {len(tag_items)} tracks")
        all_items.extend(tag_items)

    logger.info(f"[volumo] Total: {len(all_items)} tracks across {len(tag_to_ids)} tags")
    return all_items
