"""
Persistent candidate pool.

Tracks that are scored but not recommended are saved here so they can compete
in future runs alongside fresh material. The pool is capped at _POOL_CAP entries,
kept sorted by last_score descending, so the weakest candidates naturally age out.
"""
import json
import os

from src.logger import get_logger
from src.models import Candidate, PoolRecord

logger = get_logger(__name__)

_POOL_FILE = "candidate_pool.json"
POOL_CAP = 500


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _record_to_dict(r: PoolRecord) -> dict:
    return {
        "artist": r.artist,
        "title": r.title,
        "link": r.link,
        "source": r.source,
        "added_at": r.added_at,
        "last_score": r.last_score,
        "label": r.label,
        "release_date": r.release_date,
        "release_name": r.release_name,
        "genre_tags": r.genre_tags,
        "raw_metadata": r.raw_metadata,
    }


def _dict_to_record(d: dict) -> PoolRecord:
    return PoolRecord(
        artist=d["artist"],
        title=d["title"],
        link=d["link"],
        source=d["source"],
        added_at=d.get("added_at", ""),
        last_score=d.get("last_score", 0.0),
        label=d.get("label"),
        release_date=d.get("release_date"),
        release_name=d.get("release_name"),
        genre_tags=d.get("genre_tags", []),
        raw_metadata=d.get("raw_metadata", {}),
    )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_pool(data_dir: str) -> list[PoolRecord]:
    path = os.path.join(data_dir, _POOL_FILE)
    if not os.path.exists(path):
        logger.info(f"[pool] No pool file at {path} — starting fresh")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = [_dict_to_record(d) for d in data]
    logger.info(f"[pool] Loaded {len(records)} pool records")
    return records


def save_pool(records: list[PoolRecord], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _POOL_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_record_to_dict(r) for r in records], f, indent=2, ensure_ascii=False)
    logger.info(f"[pool] Saved {len(records)} pool records to {path}")


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def pool_to_candidates(records: list[PoolRecord]) -> list[Candidate]:
    """Convert pool records to Candidate objects ready for scoring (score=0, signals=[])."""
    return [
        Candidate(
            artist=r.artist,
            title=r.title,
            link=r.link,
            source=r.source,
            label=r.label,
            release_date=r.release_date,
            release_name=r.release_name,
            genre_tags=r.genre_tags,
            raw_metadata=r.raw_metadata,
        )
        for r in records
    ]
