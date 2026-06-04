"""
Bandcamp source fetcher — discover/1/discover_web API.

Uses Bandcamp's internal discover API to get newest releases per tag.
The old hub/2/dig_deeper endpoint is defunct; this uses the current
discover_web endpoint discovered from the Vue SPA JS bundle.
"""
import requests

from src.fetchers.common import polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_DISCOVER_URL = "https://bandcamp.com/api/discover/1/discover_web"
_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Origin": "https://bandcamp.com",
}
_TIMEOUT = 25


def _tag_to_genre(tag: str) -> str:
    mapping = {
        "drum-and-bass": "dnb",
        "breakbeat": "breaks",
        "uk-garage": "ukg",
        "uk-bass": "uk-bass",
        "house": "house",
        "techno": "techno",
        "electronic": "electronic",
        "funk": "funk-soul-jazz",
        "r-b-soul": "funk-soul-jazz",
        "hip-hop-rap": "hip-hop",
        "electronica": "electronica",
        "downtempo": "downtempo",
        "lounge": "downtempo",
    }
    return mapping.get(tag, tag)


def _clean_url(url: str) -> str:
    return url.split("?")[0] if url else ""


def _fetch_tag(tag: str, count: int) -> list[dict]:
    payload = {
        "category_id": 0,
        "tag_norm_names": [tag],
        "geoname_id": 0,
        "slice": "new",
        "time_facet_id": None,
        "cursor": None,
        "size": count,
        "include_result_types": ["a"],
    }
    headers = {**_HEADERS, "Referer": f"https://bandcamp.com/discover/{tag}"}
    resp = requests.post(_DISCOVER_URL, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("__api_special__") == "exception":
        raise ValueError(data.get("error_type", "Bandcamp API error"))
    return data.get("results", [])


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("bandcamp")
    if not cfg.get("enabled", False):
        return []

    tags: list[str] = cfg.get("tags", [])
    if target_genre is not None:
        tags = [tag for tag in tags if _tag_to_genre(tag) == target_genre]
    count: int = cfg.get("count_per_tag", 20)

    all_items: list[SourceItem] = []

    for tag in tags:
        logger.info(f"[bandcamp] Fetching tag: {tag}")
        try:
            raw_items = _fetch_tag(tag, count)
        except Exception as e:
            logger.warning(f"[bandcamp] Failed to fetch tag {tag}: {e}")
            polite_sleep(1.5)
            continue

        genre = _tag_to_genre(tag)
        tag_items = []

        for item in raw_items:
            artist = item.get("album_artist") or item.get("band_name") or ""
            title = item.get("title") or ""
            if not artist or not title:
                continue

            tag_items.append(SourceItem(
                source="bandcamp",
                artist=artist,
                title=title,
                link=_clean_url(item.get("item_url", "")),
                label=None,
                release_date=item.get("release_date"),
                release_name=title,
                genre_tags=[genre],
                raw_metadata={"bandcamp_tag": tag, "item_type": item.get("item_type")},
            ))

        logger.info(f"[bandcamp] {tag}: {len(tag_items)} items")
        all_items.extend(tag_items)
        polite_sleep(1.5)

    logger.info(f"[bandcamp] Total: {len(all_items)} items across {len(tags)} tags")
    return all_items
