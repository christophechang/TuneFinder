"""
Beatport source fetcher — genre top-100 chart via the internal v4 API.

Replaces the former __NEXT_DATA__ HTML scrape (Cloudflare-blocked since 2026-07).
Auth (login -> PKCE -> Bearer token, cached/refreshed) lives in beatport_auth.
Endpoint: GET /v4/catalog/genres/{id}/top/100/.

Key signals: chart_position (rank in the genre top-100), bpm, key (harmonic mixing).
"""
import requests

from src.fetchers import beatport_auth
from src.fetchers.common import polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://api.beatport.com/v4"
_TRACK_URL = "https://www.beatport.com/track/{slug}/{id}"
_CHART_SIZE = 100
_PER_PAGE = 100
_TIMEOUT = 25

# Maps Beatport genre slugs to internal genre tags. KEEP VERBATIM from the prior
# implementation (merged feeds + house sub-genres roll-ups).
_SLUG_TO_TAGS: dict[str, list[str]] = {
    "drum-bass": ["dnb"],
    "breaks-breakbeat-uk-bass": ["breaks", "uk-bass"],
    "house": ["house"],
    "melodic-house-techno": ["house"],
    "minimal-deep-tech": ["house"],
    "deep-house": ["house"],
    "tech-house": ["house"],
    "uk-garage-bassline": ["ukg"],
    "electronica": ["electronica"],
    "downtempo": ["downtempo"],
    "techno-raw-deep-hypnotic": ["techno"],
    "hip-hop": ["hip-hop"],
    "rb": ["funk-soul-jazz"],
}


def _get_json(url: str, session: requests.Session) -> dict:
    resp = session.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _parse_track(raw: dict, fallback_tags: list[str], chart_position: int | None = None) -> SourceItem | None:
    title = (raw.get("name") or "").strip()
    if not title:
        return None
    artists = raw.get("artists") or []
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    if not artist:
        return None

    track_id = raw.get("id", "")
    slug = raw.get("slug", "")
    link = _TRACK_URL.format(slug=slug, id=track_id) if slug and track_id else ""

    release = raw.get("release") or {}
    release_name = release.get("name") or None
    label = (release.get("label") or {}).get("name") or None
    release_date = raw.get("publish_date") or raw.get("new_release_date") or ""

    genre_slug = (raw.get("genre") or {}).get("slug", "")
    genre_tags = _SLUG_TO_TAGS.get(genre_slug) or fallback_tags
    key = (raw.get("key") or {}).get("name") or None

    return SourceItem(
        source="beatport",
        artist=artist,
        title=title,
        link=link,
        label=label,
        release_date=release_date,
        release_name=release_name,
        genre_tags=genre_tags,
        raw_metadata={
            "beatport_id": track_id,
            "bpm": raw.get("bpm"),
            "chart_position": chart_position,
            "key": key,
            "mix_name": raw.get("mix_name"),
            "isrc": raw.get("isrc"),
        },
    )


def _fetch_genre_top(session: requests.Session, genre_id) -> list[dict]:
    """Return up to 100 raw track dicts in rank order for a genre's top-100 chart.

    Follows the API's own `next` URL (an absolute URL, per DRF pagination) rather
    than hand-building a `page=N` param. This is robust to whatever scheme the
    endpoint uses (page number, cursor, or `per_page` honoured in one shot) and
    cannot re-request the same page into duplicates. Stops at `next=None`, an
    empty page, or 100 tracks.
    """
    tracks: list[dict] = []
    url = f"{_BASE}/catalog/genres/{genre_id}/top/{_CHART_SIZE}/?per_page={_PER_PAGE}"
    while url and len(tracks) < _CHART_SIZE:
        data = _get_json(url, session)
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            break
        tracks.extend(results)
        url = data.get("next") if isinstance(data, dict) else None
    return tracks[:_CHART_SIZE]


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("beatport")
    if not cfg.get("enabled", False):
        return []

    genres: list[dict] = cfg.get("genres", [])
    if target_genre is not None:
        genres = [
            g for g in genres
            if target_genre in (_SLUG_TO_TAGS.get(g.get("slug", "")) or [g.get("name", "")])
        ]
    if not genres:
        return []

    token = beatport_auth.get_access_token(settings)  # raises BeatportAuthError
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    all_items: list[SourceItem] = []
    attempted = 0
    completed = 0

    for genre in genres:
        slug = genre.get("slug", "")
        genre_id = genre.get("id", "")
        name = genre.get("name", slug)
        fallback_tags = _SLUG_TO_TAGS.get(slug) or [name]
        if not slug or not genre_id:
            logger.warning(f"[beatport] Skipping genre with missing slug/id: {genre}")
            continue

        attempted += 1
        try:
            raw_tracks = _fetch_genre_top(session, genre_id)
        except Exception as e:
            logger.warning(f"[beatport] {name}: fetch failed: {e}")
            polite_sleep(2.0)
            continue
        completed += 1

        genre_items = []
        for pos, raw in enumerate(raw_tracks, start=1):
            item = _parse_track(raw, fallback_tags, chart_position=pos)
            if item:
                genre_items.append(item)
        logger.info(f"[beatport] {name}: {len(genre_items)} tracks")
        all_items.extend(genre_items)
        polite_sleep(2.0)

    if attempted > 0 and completed == 0:
        raise RuntimeError(f"beatport: all {attempted} genres failed to fetch")

    logger.info(f"[beatport] Total: {len(all_items)} items across {completed}/{attempted} genres")
    return all_items
