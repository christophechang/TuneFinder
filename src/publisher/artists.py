"""CONTRACTS §2 — the thirteen-week artist index the publisher keeps itself.

Pool documents expire after 45 days, so "how often has this artist appeared in
this family?" cannot be asked of the pool: by the time thirteen weeks have
passed, ten of them are gone. The publisher therefore keeps its own tally beside
its snapshots and posts a window of it every run.

The store is one JSON file under the caller's pool directory:

    index[family][week_monday][artist_name] = {"keys": [key_v2...], "labels": [...]}

Weeks are Mondays, because both ends must agree where a week starts — the API
refuses any other day. Counts are **distinct `key_v2`s**, not sightings: a track
seen on Beatport on Tuesday and on SoundCloud on Friday is one appearance that
week, and the same track re-charting the next week is a new one. Sets are stored
as sorted lists so the file diffs cleanly and reloads identically.

The only IO here is that one file, written through `atomic_write_json` under a
pool directory the caller names; nothing imports `requests` or reaches `data/`.
"""
import json
import os
import re
from datetime import date, timedelta

from src.pipeline.storage import atomic_write_json
from src.publisher.payload import SCHEMA_VERSION

ARTIST_INDEX_FILE = "artist_index.json"

# artists.schema.json's `maxItems`. A family with more artists than this in
# thirteen weeks sends its busiest.
MAX_ARTISTS = 5000

# "Zero T feat. Steo" is Zero T's track; the guest is not a second credit here.
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat|ft|featuring)\.?\s+.*$", re.IGNORECASE)
# The separators the stores actually use between credits. " x " and " vs " need
# their spaces — "Max Cooper" is one artist.
_SPLIT_RE = re.compile(r"\s*(?:,|&|/|\sx\s|\svs\.?\s)\s*", re.IGNORECASE)


def week_monday(d: date) -> date:
    """The Monday of `d`'s week — the bucket every count falls in."""
    return d - timedelta(days=d.weekday())


def split_artists(artist: str) -> list[str]:
    """One credit string as the artists it names, in order, casing untouched.

    Splitting is deliberately mechanical: it is the same rule on both sides of
    the wire, and a name that legitimately contains a separator ("Chase &
    Status") splits. The counts are a ranking signal, not a discography.
    """
    if not artist:
        return []
    names: list[str] = []
    for part in _SPLIT_RE.split(_FEAT_TAIL_RE.sub("", artist)):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def load_artist_index(pool_dir: str) -> dict:
    """The stored index, or an empty one before the first run."""
    path = os.path.join(pool_dir, ARTIST_INDEX_FILE)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        index = json.load(f)
    return index if isinstance(index, dict) else {}


def save_artist_index(index: dict, pool_dir: str) -> None:
    """Write the index atomically — a crash mid-write must not cost the tally."""
    atomic_write_json(os.path.join(pool_dir, ARTIST_INDEX_FILE), index)


def update_artist_index(index: dict, items: list[dict], observed_on: date) -> dict:
    """Fold one run's built items into `index` (in place) and return it.

    Every artist named by an item's credit is counted under every family the
    item belongs to: a track in two families is an appearance in both.
    """
    week = week_monday(observed_on).isoformat()
    for item in items:
        key_v2 = item.get("key_v2")
        if not key_v2:
            continue
        label = item.get("label")
        names = split_artists(item.get("artist") or "")
        for family in item.get("families", []):
            bucket = index.setdefault(family, {}).setdefault(week, {})
            for name in names:
                entry = bucket.setdefault(name, {"keys": [], "labels": []})
                entry["keys"] = sorted(set(entry.get("keys", [])) | {key_v2})
                labels = set(entry.get("labels", []))
                if label:
                    labels.add(label)
                entry["labels"] = sorted(labels)
    return index


def prune_artist_index(index: dict, keep_weeks: int, today: date) -> dict:
    """A copy of `index` holding only the last `keep_weeks` weeks.

    The window ends at this week's Monday and includes it, so `keep_weeks=13`
    keeps twelve Mondays back and today's. A family left with no week at all is
    dropped rather than stored empty.
    """
    cutoff = (week_monday(today) - timedelta(weeks=keep_weeks - 1)).isoformat()
    pruned: dict = {}
    for family, weeks in index.items():
        kept = {week: artists for week, artists in weeks.items() if week >= cutoff}
        if kept:
            pruned[family] = kept
    return pruned


def artists_payloads(
    index: dict, run_id: str, taxonomy_version: int, *, weeks: int, today: date
) -> list[dict]:
    """One `POST /api/ingest/artists` body per family with anything to say.

    The weeks are the last `weeks` Mondays, ascending, ending at this week's;
    `counts` has one entry per week in that order, 0 where the artist is absent,
    and `labels` is the union over the whole window. Artists are ordered by
    total appearances descending, then by name, and the busiest `MAX_ARTISTS`
    are sent — the cap is the schema's, and it should cut the tail, not the head.
    """
    week_isos = [
        (week_monday(today) - timedelta(weeks=offset)).isoformat()
        for offset in reversed(range(weeks))
    ]

    payloads: list[dict] = []
    for family in sorted(index):
        counts_by_artist: dict[str, list[int]] = {}
        labels_by_artist: dict[str, set[str]] = {}
        for position, week in enumerate(week_isos):
            for name, entry in index[family].get(week, {}).items():
                counts = counts_by_artist.setdefault(name, [0] * len(week_isos))
                counts[position] = len(entry.get("keys", []))
                labels_by_artist.setdefault(name, set()).update(entry.get("labels", []))

        if not counts_by_artist:
            continue

        ranked = sorted(
            counts_by_artist.items(), key=lambda pair: (-sum(pair[1]), pair[0])
        )[:MAX_ARTISTS]
        payloads.append(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "taxonomy_version": taxonomy_version,
                "family": family,
                "weeks": week_isos,
                "artists": [
                    {
                        "name": name,
                        "counts": counts,
                        "labels": sorted(labels_by_artist[name]),
                    }
                    for name, counts in ranked
                ],
            }
        )
    return payloads
