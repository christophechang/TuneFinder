import json
import os
from datetime import datetime, timezone

from src.logger import get_logger
from src.models import RecommendationRecord

logger = get_logger(__name__)

_HISTORY_FILE = "recommendation_history.json"
_MIX_PREP_HISTORY_FILE = "mix_prep_history.json"


def make_report_id() -> str:
    """Return the ISO week report ID for the current run, e.g. '2026-W10'."""
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _record_to_dict(r: RecommendationRecord) -> dict:
    return {
        "artist": r.artist,
        "title": r.title,
        "link": r.link,
        "source": r.source,
        "recommended_at": r.recommended_at,
        "report_id": r.report_id,
        "track_no": r.track_no,
        "signal_codes": r.signal_codes,
        "genre_tags": r.genre_tags,
        "score": r.score,
        "label": r.label,
    }


def _dict_to_record(d: dict) -> RecommendationRecord:
    return RecommendationRecord(
        artist=d["artist"],
        title=d["title"],
        link=d["link"],
        source=d["source"],
        recommended_at=d.get("recommended_at", ""),
        report_id=d.get("report_id", ""),
        track_no=d.get("track_no"),
        signal_codes=d.get("signal_codes", []),
        genre_tags=d.get("genre_tags", []),
        score=d.get("score"),
        label=d.get("label"),
    )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_history(data_dir: str) -> list[RecommendationRecord]:
    path = os.path.join(data_dir, _HISTORY_FILE)
    if not os.path.exists(path):
        logger.info(f"[history] No history file at {path} — starting fresh")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = [_dict_to_record(d) for d in data]
    logger.info(f"[history] Loaded {len(records)} recommendation records")
    return records


def save_history(records: list[RecommendationRecord], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _HISTORY_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_record_to_dict(r) for r in records], f, indent=2, ensure_ascii=False)
    logger.info(f"[history] Saved {len(records)} recommendation records to {path}")


def append_records(new_records: list[RecommendationRecord], data_dir: str) -> None:
    """Append newly recommended tracks to the history file."""
    existing = load_history(data_dir)
    combined = existing + new_records
    save_history(combined, data_dir)
    logger.info(f"[history] Appended {len(new_records)} new records (total: {len(combined)})")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def build_history_keys(records: list[RecommendationRecord], remix_aware: bool = False) -> set[str]:
    """Return keys for all previously recommended tracks.

    Includes both the raw key (as stored) and the normalised key (version
    suffixes and feat. credits stripped) so that a track saved as
    "Title (Original Mix)" still blocks "Title" in a future run.

    When remix_aware is True, ALSO include the remix-aware key. The legacy key is
    still emitted for backward compatibility so old history records keep blocking
    their exact old-style matches under both regimes.
    """
    from src.pipeline.dedup import make_dedup_key
    keys: set[str] = set()
    for r in records:
        keys.add(r.key)
        keys.add(make_dedup_key(r.artist, r.title))
        if remix_aware:
            keys.add(make_dedup_key(r.artist, r.title, remix_aware=True))
    return keys


def load_mix_prep_history(data_dir: str) -> list[RecommendationRecord]:
    path = os.path.join(data_dir, _MIX_PREP_HISTORY_FILE)
    if not os.path.exists(path):
        logger.info(f"[history] No mix-prep history file at {path} — starting fresh")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = [_dict_to_record(d) for d in data]
    logger.info(f"[history] Loaded {len(records)} mix-prep history records")
    return records


def save_mix_prep_history(records: list[RecommendationRecord], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _MIX_PREP_HISTORY_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_record_to_dict(r) for r in records], f, indent=2, ensure_ascii=False)
    logger.info(f"[history] Saved {len(records)} mix-prep history records to {path}")


def append_mix_prep_records(new_records: list[RecommendationRecord], data_dir: str) -> None:
    """Append newly recommended tracks to the mix-prep history file (separate from weekly history)."""
    existing = load_mix_prep_history(data_dir)
    combined = existing + new_records
    save_mix_prep_history(combined, data_dir)
    logger.info(f"[history] Appended {len(new_records)} mix-prep records (total: {len(combined)})")


# ---------------------------------------------------------------------------
# Artist-level recency lookup
# ---------------------------------------------------------------------------

def recent_recommended_artists(data_dir: str, weeks: int = 4) -> set[str]:
    """Return normalised artist strings recommended within the last `weeks` weeks
    across BOTH weekly history (recommendation_history.json) and mix-prep history
    (mix_prep_history.json). Both represent tracks the DJ already saw — both
    should suppress repeats at the artist level.

    Each record's artist string is split into individual artists (handles
    "A, B" / "A feat. B" / "A & B" / "A x B") and normalised via dedup.
    """
    from datetime import timedelta
    from src.pipeline.dedup import normalise_artist
    from src.pipeline.profile import _split_artists

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    records = load_history(data_dir) + load_mix_prep_history(data_dir)

    recent: set[str] = set()
    for r in records:
        if not r.recommended_at:
            continue
        try:
            ts = datetime.fromisoformat(r.recommended_at)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        for part in _split_artists(r.artist):
            recent.add(normalise_artist(part))

    logger.info(f"[history] {len(recent)} artists in {weeks}-week recency window")
    return recent
