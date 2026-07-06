import json
import os
import re
import time

import requests

from src.logger import get_logger
from src.models import Mix, Track, TrackRef

logger = get_logger(__name__)

_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,3}[.)]\s+")


def _clean_artist(name: str) -> str:
    return _NUMBER_PREFIX_RE.sub("", name)

_MIXES_PAGE_SIZE = 50
_TRACKS_PAGE_SIZE = 50
_REQUEST_TIMEOUT = 30
_POLITE_DELAY = 0.1  # seconds between paginated requests


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _paginate(path: str, page_size: int, base_url: str) -> list[dict]:
    """Fetch all pages from a paginated endpoint and return a flat item list."""
    if not base_url:
        raise ValueError("catalog.user_url not configured")
    url = f"{base_url}{path}"
    page = 1
    all_items: list[dict] = []

    while True:
        logger.info(f"[catalog] GET {path} page={page}")
        try:
            data = _get(url, {"page": page, "pageSize": page_size})
        except requests.RequestException as e:
            logger.error(f"[catalog] Request failed — {path} page {page}: {e}")
            if page == 1:
                raise
            logger.warning("[catalog] Returning partial data due to request failure")
            break

        items = data.get("items", [])
        all_items.extend(items)

        if page >= data.get("totalPages", 1):
            break

        page += 1
        time.sleep(_POLITE_DELAY)

    logger.info(f"[catalog] {path} — loaded {len(all_items)} total items")
    return all_items


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_mix(raw: dict) -> tuple[Mix, int]:
    strips = 0
    tracklist = []
    for t in raw.get("tracklist", []):
        raw_artist = t["artist"]
        cleaned = _clean_artist(raw_artist)
        if cleaned != raw_artist:
            strips += 1
        tracklist.append(TrackRef(artist=cleaned, title=t["title"]))
    return Mix(
        id=raw.get("id", ""),
        title=raw.get("title", ""),
        url=raw.get("url", ""),
        description=raw.get("description", ""),
        genre=raw.get("genre", ""),
        energy=raw.get("energy", ""),
        bpm_min=raw.get("bpmMin") or 0,
        bpm_max=raw.get("bpmMax") or 0,
        moods=raw.get("moods") or [],
        published_at=raw.get("publishedAt", ""),
        tracklist=tracklist,
    ), strips


def _parse_track(raw: dict) -> tuple[Track, int]:
    raw_artist = raw.get("artist", "")
    cleaned = _clean_artist(raw_artist)
    strips = 1 if cleaned != raw_artist else 0
    return Track(
        artist=cleaned,
        title=raw.get("title", ""),
        recurrence_count=raw.get("recurrenceCount", 1),
        genres_seen=raw.get("genresSeen") or [],
    ), strips


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Caller: src/pipeline/profile.apply_recency_weights (issue #11), via
# tunefinder.__main__._load_profile_state / cmd_build_profile — timestamps each
# artist's plays to compute a recency-weighted play count. Also retained for
# future BPM/energy-aware mix-prep taste modelling. See docs/improvement-plan.md §5.
def fetch_all_mixes(settings) -> list[Mix]:
    """
    Fetch all published mixes with full tracklists.
    Used for taste modelling (genre, BPM, energy, moods, recency) and as a
    secondary source of known-track data.
    Falls back to fixtures when settings.testing_use_fixtures is True.
    """
    if settings.testing_use_fixtures:
        return _load_fixture_mixes(settings.testing_fixtures_dir)

    base_url = settings.catalog_user_url
    raw_items = _paginate("/api/catalog/mixes", _MIXES_PAGE_SIZE, base_url)
    pairs = [_parse_mix(r) for r in raw_items]
    mixes = [m for m, _ in pairs]
    total_strips = sum(s for _, s in pairs)
    if total_strips:
        logger.info(f"[catalog] Stripped numbering prefix from {total_strips} artist names")
    logger.info(f"[catalog] Parsed {len(mixes)} mixes")
    return mixes


def fetch_all_tracks(settings) -> list[Track]:
    """
    Fetch the deduplicated track catalogue with recurrence counts and genres.
    Primary source for the known-track exclusion set and artist play counts.
    Falls back to fixtures when settings.testing_use_fixtures is True.
    """
    if settings.testing_use_fixtures:
        return _load_fixture_tracks(settings.testing_fixtures_dir)

    base_url = settings.catalog_user_url
    raw_items = _paginate("/api/catalog/tracks", _TRACKS_PAGE_SIZE, base_url)
    pairs = [_parse_track(r) for r in raw_items]
    tracks = [t for t, _ in pairs]
    total_strips = sum(s for _, s in pairs)
    if total_strips:
        logger.info(f"[catalog] Stripped numbering prefix from {total_strips} artist names")
    logger.info(f"[catalog] Parsed {len(tracks)} unique tracks")
    return tracks


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _load_fixture_mixes(fixtures_dir: str) -> list[Mix]:
    path = os.path.join(fixtures_dir, "mixes.json")
    logger.info(f"[catalog] Loading mixes fixture: {path}")
    with open(path, "r", encoding="utf-8") as f:
        pairs = [_parse_mix(r) for r in json.load(f)]
    mixes = [m for m, _ in pairs]
    total_strips = sum(s for _, s in pairs)
    if total_strips:
        logger.info(f"[catalog] Stripped numbering prefix from {total_strips} artist names")
    return mixes


def _load_fixture_tracks(fixtures_dir: str) -> list[Track]:
    path = os.path.join(fixtures_dir, "tracks.json")
    logger.info(f"[catalog] Loading tracks fixture: {path}")
    with open(path, "r", encoding="utf-8") as f:
        pairs = [_parse_track(r) for r in json.load(f)]
    tracks = [t for t, _ in pairs]
    total_strips = sum(s for _, s in pairs)
    if total_strips:
        logger.info(f"[catalog] Stripped numbering prefix from {total_strips} artist names")
    return tracks


def save_fixtures(settings) -> None:
    """
    Fetch live data and save raw API responses to fixtures/ for offline testing.
    Run once with: ./venv/bin/python -m tunefinder save-fixtures
    """
    os.makedirs(settings.testing_fixtures_dir, exist_ok=True)

    base_url = settings.catalog_user_url
    mixes_raw = _paginate("/api/catalog/mixes", _MIXES_PAGE_SIZE, base_url)
    mixes_path = os.path.join(settings.testing_fixtures_dir, "mixes.json")
    with open(mixes_path, "w", encoding="utf-8") as f:
        json.dump(mixes_raw, f, indent=2)
    logger.info(f"[catalog] Saved {len(mixes_raw)} mixes to {mixes_path}")

    tracks_raw = _paginate("/api/catalog/tracks", _TRACKS_PAGE_SIZE, base_url)
    tracks_path = os.path.join(settings.testing_fixtures_dir, "tracks.json")
    with open(tracks_path, "w", encoding="utf-8") as f:
        json.dump(tracks_raw, f, indent=2)
    logger.info(f"[catalog] Saved {len(tracks_raw)} tracks to {tracks_path}")
