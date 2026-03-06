import json
import os
from datetime import datetime, timezone

from src.logger import get_logger
from src.models import RecommendationRecord

logger = get_logger(__name__)

_HISTORY_FILE = "recommendation_history.json"


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
    }


def _dict_to_record(d: dict) -> RecommendationRecord:
    return RecommendationRecord(
        artist=d["artist"],
        title=d["title"],
        link=d["link"],
        source=d["source"],
        recommended_at=d.get("recommended_at", ""),
        report_id=d.get("report_id", ""),
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

def build_history_keys(records: list[RecommendationRecord]) -> set[str]:
    """Return normalised keys for all previously recommended tracks."""
    return {r.key for r in records}
