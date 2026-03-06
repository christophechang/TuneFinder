"""
Source fetcher aggregator.

fetch_all_sources() runs all enabled fetchers and returns the combined
list of SourceItems. Results are saved to data/source_items.json for use
by the pipeline on the same run.
"""
import json
import os

from src.fetchers import bandcamp, beatport, bleep, boomkat, juno, ra, traxsource
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_SOURCE_ITEMS_FILE = "source_items.json"

_FETCHERS = [
    ("juno", juno.fetch),
    ("beatport", beatport.fetch),
    ("bandcamp", bandcamp.fetch),
    ("traxsource", traxsource.fetch),
    ("boomkat", boomkat.fetch),
    ("bleep", bleep.fetch),
    ("resident_advisor", ra.fetch),
]


def fetch_all_sources(settings) -> list[SourceItem]:
    all_items: list[SourceItem] = []
    counts: dict[str, int] = {}

    for name, fetch_fn in _FETCHERS:
        if not settings.source_enabled(name):
            logger.info(f"[sources] Skipping disabled source: {name}")
            continue
        try:
            items = fetch_fn(settings)
            counts[name] = len(items)
            all_items.extend(items)
        except Exception as e:
            logger.error(f"[sources] {name} fetch failed: {e}")
            counts[name] = 0

    logger.info(
        "[sources] Fetch complete — "
        + ", ".join(f"{k}: {v}" for k, v in counts.items())
        + f" — total: {len(all_items)}"
    )
    return all_items


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _item_to_dict(item: SourceItem) -> dict:
    return {
        "source": item.source,
        "artist": item.artist,
        "title": item.title,
        "link": item.link,
        "label": item.label,
        "release_date": item.release_date,
        "release_name": item.release_name,
        "genre_tags": item.genre_tags,
        "raw_metadata": item.raw_metadata,
    }


def _dict_to_item(d: dict) -> SourceItem:
    return SourceItem(
        source=d.get("source", ""),
        artist=d.get("artist", ""),
        title=d.get("title", ""),
        link=d.get("link", ""),
        label=d.get("label"),
        release_date=d.get("release_date"),
        release_name=d.get("release_name"),
        genre_tags=d.get("genre_tags", []),
        raw_metadata=d.get("raw_metadata", {}),
    )


def save_source_items(items: list[SourceItem], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _SOURCE_ITEMS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_item_to_dict(i) for i in items], f, indent=2, ensure_ascii=False)
    logger.info(f"[sources] Saved {len(items)} source items to {path}")


def load_source_items(data_dir: str) -> list[SourceItem]:
    path = os.path.join(data_dir, _SOURCE_ITEMS_FILE)
    if not os.path.exists(path):
        logger.warning(f"[sources] No source items file at {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = [_dict_to_item(d) for d in data]
    logger.info(f"[sources] Loaded {len(items)} source items from {path}")
    return items
