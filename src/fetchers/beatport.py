"""
Beatport source fetcher — top-100 chart via __NEXT_DATA__ JSON extraction.

Beatport uses Next.js with React Query. The full track listing is embedded
in a <script id="__NEXT_DATA__"> JSON blob, so no HTML scraping is needed.
We extract the dehydrated React Query cache and find the results array.

Chart URL pattern: https://www.beatport.com/genre/{slug}/{id}/top-100
Track URL pattern: https://www.beatport.com/track/{slug}/{id}

Key signals extracted:
  - chart_position: rank on the Beatport genre top-100 chart (results are ordered)
  - bpm: tempo from track metadata
"""
from src.fetchers.common import get_html, extract_next_data, find_in_next_data, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_TRACK_URL = "https://www.beatport.com/track/{slug}/{id}"

# Maps Beatport genre slugs to internal genre tags.
# Handles merged genres (e.g. breaks-breakbeat-uk-bass → both tags)
# and sub-genres that all roll up to a parent (house sub-genres → house).
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


def _extract_tracks_from_next_data(data: dict) -> list[dict]:
    """
    Navigate the __NEXT_DATA__ dehydrated state to find the track results list.
    Beatport stores this under:
      props.pageProps.dehydratedState.queries[*].state.data.pages[*].results
    Falls back to a recursive search if the path has changed.
    """
    try:
        queries = (
            data.get("props", {})
                .get("pageProps", {})
                .get("dehydratedState", {})
                .get("queries", [])
        )
        for query in queries:
            pages = query.get("state", {}).get("data", {}).get("pages", [])
            for page in pages:
                results = page.get("results", [])
                if results and isinstance(results[0], dict) and "name" in results[0]:
                    return results
    except Exception:
        pass

    # Fallback: recursive search for any "results" list containing track-like objects
    candidates = find_in_next_data(data, "results")
    if candidates and isinstance(candidates[0], dict) and "name" in candidates[0]:
        return candidates

    return []


def _parse_track(raw: dict, fallback_tags: list[str], chart_position: int | None = None) -> SourceItem | None:
    title = raw.get("name", "").strip()
    if not title:
        return None

    artists = raw.get("artists", [])
    if not artists:
        return None
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))

    label_obj = raw.get("label") or {}
    label = label_obj.get("name") or None

    track_id = raw.get("id", "")
    slug = raw.get("slug", "")
    link = _TRACK_URL.format(slug=slug, id=track_id) if slug and track_id else ""

    release_date = raw.get("publish_date", "") or raw.get("release_date", "") or ""

    release_obj = raw.get("release") or {}
    release_name = release_obj.get("name") or None

    # Derive genre tags from the track's own genre slug if available,
    # falling back to the feed-level tags. This correctly handles merged
    # feeds (e.g. breaks-breakbeat-uk-bass) and house sub-genres.
    genre_slug = (raw.get("genre") or {}).get("slug", "")
    genre_tags = _SLUG_TO_TAGS.get(genre_slug) or fallback_tags

    return SourceItem(
        source="beatport",
        artist=artist,
        title=title,
        link=link,
        label=label,
        release_date=release_date,
        release_name=release_name,
        genre_tags=genre_tags,
        raw_metadata={"beatport_id": track_id, "bpm": raw.get("bpm"), "chart_position": chart_position},
    )


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("beatport")
    if not cfg.get("enabled", False):
        return []

    chart_pattern = cfg.get("chart_pattern", "")
    genres: list[dict] = cfg.get("genres", [])

    all_items: list[SourceItem] = []

    for genre in genres:
        slug = genre.get("slug", "")
        genre_id = genre.get("id", "")
        name = genre.get("name", slug)
        # fallback_tags used if the track's own genre slug isn't in _SLUG_TO_TAGS
        fallback_tags = _SLUG_TO_TAGS.get(slug) or [name]

        if not slug or not genre_id:
            logger.warning(f"[beatport] Skipping genre with missing slug/id: {genre}")
            continue

        url = chart_pattern.replace("{slug}", slug).replace("{id}", str(genre_id))
        logger.info(f"[beatport] Fetching top-100 chart for {name}: {url}")

        try:
            html = get_html(url)
        except Exception as e:
            logger.warning(f"[beatport] Failed to fetch {url}: {e}")
            polite_sleep(2.0)
            continue

        next_data = extract_next_data(html)
        if not next_data:
            logger.warning(f"[beatport] No __NEXT_DATA__ found on {url}")
            polite_sleep(2.0)
            continue

        raw_tracks = _extract_tracks_from_next_data(next_data)
        if not raw_tracks:
            logger.warning(f"[beatport] No tracks extracted from __NEXT_DATA__ for {name}")
            polite_sleep(2.0)
            continue

        genre_items = []
        for pos, raw in enumerate(raw_tracks, start=1):
            item = _parse_track(raw, fallback_tags, chart_position=pos)
            if item:
                genre_items.append(item)

        logger.info(f"[beatport] {name}: {len(genre_items)} tracks")
        all_items.extend(genre_items)
        polite_sleep(2.0)

    logger.info(f"[beatport] Total: {len(all_items)} items across {len(genres)} genres")
    return all_items
