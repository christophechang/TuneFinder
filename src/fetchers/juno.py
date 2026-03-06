"""
Juno Download source fetcher — RSS-first.

Title format in the feed: "ARTIST NAME - Release Title (Label Name) format"
Each RSS item is a release (EP/LP/single), not an individual track.
We surface it as a SourceItem at release level; the ranker treats it accordingly.
"""
import re

from src.fetchers.common import parse_rss, parse_rfc2822_date, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

# Matches: "ARTIST - Release Title (Label) anything after"
_TITLE_RE = re.compile(r"^(.+?)\s+-\s+(.+?)\s+\(([^)]+)\)", re.DOTALL)


def _parse_juno_title(raw: str) -> tuple[str, str, str]:
    """
    Returns (artist, release_name, label).
    Artist is converted from ALL CAPS to Title Case.
    Falls back gracefully if the pattern doesn't match.
    """
    m = _TITLE_RE.match(raw.strip())
    if m:
        artist = m.group(1).strip().title()
        release_name = m.group(2).strip()
        label = m.group(3).strip()
        return artist, release_name, label

    # Fallback: no label found
    if " - " in raw:
        artist, rest = raw.split(" - ", 1)
        return artist.strip().title(), rest.strip(), ""

    return "", raw.strip(), ""


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("juno")
    if not cfg.get("enabled", False):
        return []

    rss_pattern = cfg.get("rss_pattern", "https://www.juno.co.uk/{genre_slug}/feeds/rss/")
    genre_map: dict[str, str] = cfg.get("genre_map", {})

    all_items: list[SourceItem] = []

    for internal_genre, juno_slug in genre_map.items():
        url = rss_pattern.replace("{genre_slug}", juno_slug)
        logger.info(f"[juno] Fetching RSS for {internal_genre}: {url}")

        try:
            entries = parse_rss(url)
        except Exception as e:
            logger.warning(f"[juno] Failed to fetch {url}: {e}")
            polite_sleep(1.0)
            continue

        for entry in entries:
            artist, release_name, label = _parse_juno_title(entry.get("title", ""))
            if not artist and not release_name:
                continue

            all_items.append(SourceItem(
                source="juno",
                artist=artist,
                title=release_name,
                link=entry.get("link", ""),
                label=label or None,
                release_date=parse_rfc2822_date(entry.get("pubDate", "")),
                release_name=release_name,
                genre_tags=[internal_genre],
                raw_metadata={"juno_genre": juno_slug},
            ))

        logger.info(f"[juno] {juno_slug}: {len(entries)} releases")
        polite_sleep(1.0)

    logger.info(f"[juno] Total: {len(all_items)} items across {len(genre_map)} genres")
    return all_items
