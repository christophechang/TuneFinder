"""
Resident Advisor source fetcher — __NEXT_DATA__ apolloState.

RA's /music/releases page embeds all review data in a __NEXT_DATA__ JSON blob.
Reviews are stored as "Review:{id}" keys in the apolloState dict, each with a
"title" field in "Artist - Release Title" format and a "labels" list.

Labels are cross-referenced from "Label:{id}" keys in the same apolloState.
"""
import re

from src.fetchers.common import get_html, extract_next_data
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://ra.co"
_ARTIST_TITLE_RE = re.compile(r"^(.+?)\s+-\s+(.+)$")


def _parse_reviews(apollo: dict) -> list[SourceItem]:
    labels = {k: v for k, v in apollo.items() if k.startswith("Label:")}
    items = []

    for key, review in apollo.items():
        if not key.startswith("Review:"):
            continue

        raw_title = review.get("title", "")
        m = _ARTIST_TITLE_RE.match(raw_title)
        if not m:
            continue

        artist = m.group(1).strip()
        title = m.group(2).strip()

        content_url = review.get("contentUrl", "")
        link = f"{_BASE}{content_url}" if content_url else ""

        label = None
        label_refs = review.get("labels", [])
        if label_refs:
            ref = label_refs[0].get("__ref", "")
            label_data = labels.get(ref, {})
            label = label_data.get("name") or None

        if not artist or not title:
            continue

        # "date" is the review publication date in ISO format — close enough to
        # release date for RA, which only reviews current releases.
        raw_date = review.get("date", "")
        release_date = raw_date[:10] if raw_date else None

        items.append(SourceItem(
            source="resident_advisor",
            artist=artist,
            title=title,
            link=link,
            label=label,
            release_date=release_date,
            release_name=title,
            raw_metadata={"ra_review_id": key.split(":")[1]},
        ))

    return items


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("resident_advisor")
    if not cfg.get("enabled", False):
        return []

    url = cfg.get("releases_url", "https://ra.co/music/releases")
    logger.info(f"[ra] Fetching: {url}")

    try:
        html = get_html(url)
    except Exception as e:
        logger.warning(f"[ra] Failed to fetch {url}: {e}")
        return []

    next_data = extract_next_data(html)
    apollo = next_data.get("props", {}).get("apolloState", {})

    if not apollo:
        logger.warning("[ra] apolloState not found in __NEXT_DATA__ — page structure may have changed.")
        return []

    results = _parse_reviews(apollo)
    logger.info(f"[ra] {len(results)} items")
    return results
