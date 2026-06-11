"""
Source fetcher aggregator.

fetch_all_sources() runs all enabled fetchers and returns the combined
list of SourceItems. Results are saved to data/source_items.json for use
by the pipeline on the same run.
"""
import gzip
import json
import os

from src.fetchers import bandcamp, beatport, bleep, boomkat, mixupload, ra, traxsource, volumo
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_SOURCE_ITEMS_FILE = "source_items.json"

_FETCHERS = [
    ("beatport", beatport.fetch),
    ("bandcamp", bandcamp.fetch),
    ("traxsource", traxsource.fetch),
    ("boomkat", boomkat.fetch),
    ("bleep", bleep.fetch),
    ("resident_advisor", ra.fetch),
    ("mixupload", mixupload.fetch),
    ("volumo", volumo.fetch),
]


def fetch_all_sources(settings, target_genre: str | None = None) -> tuple[list[SourceItem], dict[str, dict]]:
    """
    Run all enabled fetchers and return (items, health).

    If target_genre is set, fetchers may use it to narrow their source-specific
    genre lists. Fetchers that do not support genre narrowing can ignore it.

    health is a dict keyed by source name:
      {"count": int, "error": str | None}
    where error is set if the fetcher raised an exception.
    A count of 0 with no error is a warning (possible schema/config issue).
    """
    all_items: list[SourceItem] = []
    health: dict[str, dict] = {}

    for name, fetch_fn in _FETCHERS:
        if not settings.source_enabled(name):
            logger.info(f"[sources] Skipping disabled source: {name}")
            continue
        try:
            items = fetch_fn(settings, target_genre=target_genre)
            health[name] = {"count": len(items), "error": None}
            all_items.extend(items)
        except Exception as e:
            logger.error(f"[sources] {name} fetch failed: {e}")
            health[name] = {"count": 0, "error": str(e)}

    logger.info(
        "[sources] Fetch complete — "
        + ", ".join(f"{k}: {v['count']}" for k, v in health.items())
        + f" — total: {len(all_items)}"
    )
    return all_items, health


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


_ARCHIVE_RETAIN = 26


def archive_source_items(items: list[SourceItem], data_dir: str, report_id: str) -> None:
    """Write a gzip'd snapshot of source items for the week, then prune oldest beyond 26."""
    archive_dir = os.path.join(data_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"source_items_{report_id}.json.gz")
    payload = json.dumps([_item_to_dict(i) for i in items], ensure_ascii=False).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(payload)

    # Prune oldest files beyond the retention limit (by mtime)
    gz_files = sorted(
        [os.path.join(archive_dir, fn) for fn in os.listdir(archive_dir) if fn.endswith(".json.gz")],
        key=os.path.getmtime,
    )
    retained = gz_files[-_ARCHIVE_RETAIN:]
    for old in gz_files[:-_ARCHIVE_RETAIN]:
        os.remove(old)
    logger.info(f"[sources] Archived {len(items)} items → {path}; retained {len(retained)} snapshots")


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
