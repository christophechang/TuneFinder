import json
import os
import time

import requests

from src.logger import get_logger
from src.models import Mix, Track, TrackRef

logger = get_logger(__name__)

_BASE_URL = "https://api.changsta.com"
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


def _paginate(path: str, page_size: int) -> list[dict]:
    """Fetch all pages from a paginated endpoint and return a flat item list."""
    url = f"{_BASE_URL}{path}"
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

def _parse_mix(raw: dict) -> Mix:
    tracklist = [
        TrackRef(artist=t["artist"], title=t["title"])
        for t in raw.get("tracklist", [])
    ]
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
    )


def _parse_track(raw: dict) -> Track:
    return Track(
        artist=raw.get("artist", ""),
        title=raw.get("title", ""),
        recurrence_count=raw.get("recurrenceCount", 1),
        genres_seen=raw.get("genresSeen") or [],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_all_mixes(settings) -> list[Mix]:
    """
    Fetch all published mixes with full tracklists.
    Used for taste modelling (genre, BPM, energy, moods) and as a secondary
    source of known-track data.
    Falls back to fixtures when settings.testing_use_fixtures is True.
    """
    if settings.testing_use_fixtures:
        return _load_fixture_mixes(settings.testing_fixtures_dir)

    raw_items = _paginate("/api/catalog/mixes", _MIXES_PAGE_SIZE)
    mixes = [_parse_mix(r) for r in raw_items]
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

    raw_items = _paginate("/api/catalog/tracks", _TRACKS_PAGE_SIZE)
    tracks = [_parse_track(r) for r in raw_items]
    logger.info(f"[catalog] Parsed {len(tracks)} unique tracks")
    return tracks


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _load_fixture_mixes(fixtures_dir: str) -> list[Mix]:
    path = os.path.join(fixtures_dir, "mixes.json")
    logger.info(f"[catalog] Loading mixes fixture: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return [_parse_mix(r) for r in json.load(f)]


def _load_fixture_tracks(fixtures_dir: str) -> list[Track]:
    path = os.path.join(fixtures_dir, "tracks.json")
    logger.info(f"[catalog] Loading tracks fixture: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return [_parse_track(r) for r in json.load(f)]


def save_fixtures(settings) -> None:
    """
    Fetch live data and save raw API responses to fixtures/ for offline testing.
    Run once with: ./venv/bin/python -m tunefinder save-fixtures
    """
    os.makedirs(settings.testing_fixtures_dir, exist_ok=True)

    mixes_raw = _paginate("/api/catalog/mixes", _MIXES_PAGE_SIZE)
    mixes_path = os.path.join(settings.testing_fixtures_dir, "mixes.json")
    with open(mixes_path, "w", encoding="utf-8") as f:
        json.dump(mixes_raw, f, indent=2)
    logger.info(f"[catalog] Saved {len(mixes_raw)} mixes to {mixes_path}")

    tracks_raw = _paginate("/api/catalog/tracks", _TRACKS_PAGE_SIZE)
    tracks_path = os.path.join(settings.testing_fixtures_dir, "tracks.json")
    with open(tracks_path, "w", encoding="utf-8") as f:
        json.dump(tracks_raw, f, indent=2)
    logger.info(f"[catalog] Saved {len(tracks_raw)} tracks to {tracks_path}")
