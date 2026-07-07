"""
Persistent label affinity memory (issue #5).

Today's `_build_relevant_labels` (src/pipeline/ranker.py) re-derives label
relevance from scratch every run, using only that week's candidate set — a
label only "exists" if one of your known artists released there *this week*.
This module persists artist<->label associations across runs so Label Watch
can fire on quiet weeks and so a label observed in the past keeps informing
scoring even when no known artist appears in the current corpus.

Store file: data/label_affinity.json

Shape:
{
  "<label_key (lowercased, stripped label name)>": {
    "display_name": "<label name as most recently written by a source>",
    "artists": {
      "<canonical artist name, lowercased>": {
        "name": "<artist display name (ArtistProfile.name capitalisation)>",
        "last_seen": "<ISO timestamp of the most recent association>"
      },
      ...
    },
    "first_seen": "<ISO timestamp of the first time this label was recorded>",
    "last_seen": "<ISO timestamp of the most recent association on this label>"
  },
  ...
}

`first_seen`/`last_seen` are label-level (when the label entry itself was
created / most recently touched). Each artist association additionally
carries its own `last_seen` so `fresh_label_artist_data` can drop individual
stale artists from a label without discarding the whole label the moment one
old association ages out.
"""
import copy
import json
import os
from datetime import datetime, timedelta, timezone

from src.logger import get_logger
from src.models import ArtistProfile, Candidate
from src.pipeline.profile import _split_artists, resolve_profile

logger = get_logger(__name__)

_LABEL_AFFINITY_FILE = "label_affinity.json"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_label_affinity(data_dir: str) -> dict:
    path = os.path.join(data_dir, _LABEL_AFFINITY_FILE)
    if not os.path.exists(path):
        logger.info(f"[labels] No label affinity store at {path} — starting fresh")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        store = json.load(f)
    logger.info(f"[labels] Loaded label affinity store — {len(store)} labels from {path}")
    return store


def save_label_affinity(store: dict, data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _LABEL_AFFINITY_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    associations = sum(len(entry.get("artists", {})) for entry in store.values())
    logger.info(f"[labels] Saved label affinity store — {len(store)} labels, {associations} associations to {path}")


# ---------------------------------------------------------------------------
# Update (pure — no IO)
# ---------------------------------------------------------------------------

def update_label_affinity(
    store: dict,
    candidates: list[Candidate],
    profiles_lower: dict[str, ArtistProfile],
    aliases: dict[str, str] | None,
    now_iso: str,
) -> dict:
    """Return a new store with associations recorded for every candidate whose
    label carries a known artist (matched in profiles_lower, or via aliases —
    same resolution as ranker._build_relevant_labels/resolve_profile).

    Pure: does not mutate `store` or read/write disk. Pass the pre-filter
    candidate set (label_seed in __main__.py) so known artists filtered out of
    the report still contribute their label associations.
    """
    updated = copy.deepcopy(store)
    for c in candidates:
        if not c.label:
            continue
        label_key = c.label.lower().strip()
        for part in _split_artists(c.artist):
            profile = resolve_profile(part, profiles_lower, aliases)
            if not profile:
                continue
            entry = updated.setdefault(label_key, {
                "display_name": c.label,
                "artists": {},
                "first_seen": now_iso,
                "last_seen": now_iso,
            })
            entry["display_name"] = c.label  # prefer the most recently seen written form
            entry["artists"][profile.name.lower()] = {"name": profile.name, "last_seen": now_iso}
            entry["last_seen"] = now_iso

    logger.info(f"[labels] update_label_affinity — {len(updated)} labels tracked (was {len(store)})")
    return updated


# ---------------------------------------------------------------------------
# Read — fresh (non-stale) associations for ranker consumption
# ---------------------------------------------------------------------------

def fresh_label_artist_data(
    store: dict,
    max_age_weeks: int,
    now: datetime | None = None,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Return (counts, names) for label associations younger than max_age_weeks.

    counts: {label_key: number of distinct artists with a fresh association}.
    names: {label_key: sorted display names of those fresh artists}.
    A label with zero fresh artists (every association aged out) is absent
    from both dicts — it should not count as "relevant" from memory alone.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=max_age_weeks)

    counts: dict[str, int] = {}
    names: dict[str, list[str]] = {}
    for label_key, entry in store.items():
        fresh_names = []
        for artist_key, artist_data in entry.get("artists", {}).items():
            last_seen = artist_data.get("last_seen", "")
            try:
                ts = datetime.fromisoformat(last_seen)
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                fresh_names.append(artist_data.get("name", artist_key))
        if fresh_names:
            counts[label_key] = len(fresh_names)
            names[label_key] = sorted(fresh_names)

    logger.info(f"[labels] {len(counts)} labels with fresh memory (max_age={max_age_weeks}w)")
    return counts, names
