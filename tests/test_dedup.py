"""Tests for cross-source deduplication and _merge_group backfill."""
import pytest

from src.models import SourceItem
from src.pipeline.dedup import (
    _MERGE_BACKFILL_KEYS,
    _merge_group,
    deduplicate_source_items,
    make_dedup_key,
)


def _item(source, artist="Artist", title="Title", label=None, release_date=None, raw_metadata=None):
    return SourceItem(
        source=source,
        artist=artist,
        title=title,
        link=f"https://{source}.example.com/track",
        label=label,
        release_date=release_date,
        genre_tags=[],
        raw_metadata=raw_metadata or {},
    )


# ---------------------------------------------------------------------------
# _merge_group backfill
# ---------------------------------------------------------------------------

def test_winner_values_not_overwritten():
    winner = _item("beatport", raw_metadata={"beatport_id": 99, "bpm": 140})
    loser = _item("volumo", raw_metadata={"beatport_id": 1, "bpm": 999, "volumo_track_id": 42})
    # winner has higher richness via label
    winner.label = "Lab"
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["beatport_id"] == 99
    assert merged.raw_metadata["bpm"] == 140


def test_missing_keys_backfilled_from_losers():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 10})
    loser = _item("volumo", raw_metadata={"volumo_track_id": 55, "volumo_album_id": 77, "keysign": "Am"})
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["volumo_track_id"] == 55
    assert merged.raw_metadata["volumo_album_id"] == 77
    assert merged.raw_metadata["keysign"] == "Am"
    assert merged.raw_metadata["beatport_id"] == 10  # winner's own value preserved


def test_non_allowlisted_keys_not_copied():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 10})
    loser = _item("volumo", raw_metadata={"secret_field": "should_not_copy", "volumo_track_id": 5})
    merged = _merge_group([winner, loser])
    assert "secret_field" not in merged.raw_metadata
    assert merged.raw_metadata["volumo_track_id"] == 5


def test_single_item_group_no_backfill_error():
    item = _item("beatport", raw_metadata={"beatport_id": 7})
    merged = _merge_group([item])
    assert merged.raw_metadata["beatport_id"] == 7
    # seen_on_sources set even for single-item groups (existing behaviour)
    assert "seen_on_sources" in merged.raw_metadata


def test_bandcamp_album_id_backfilled():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 20})
    loser = _item("bandcamp", raw_metadata={"bandcamp_album_id": 12345})
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["bandcamp_album_id"] == 12345


def test_backfill_keys_allowlist_exhaustive():
    # All expected keys present in the allowlist constant
    for key in ("beatport_id", "volumo_track_id", "volumo_album_id",
                "bandcamp_album_id", "bpm", "key", "keysign"):
        assert key in _MERGE_BACKFILL_KEYS


# ---------------------------------------------------------------------------
# deduplicate_source_items integration
# ---------------------------------------------------------------------------

def test_dedup_merges_cross_source_and_backfills():
    bp = _item("beatport", artist="Sully", title="Skyline",
               label="Metalheadz", raw_metadata={"beatport_id": 500})
    bc = _item("bandcamp", artist="Sully", title="Skyline",
               raw_metadata={"bandcamp_album_id": 9999})
    merged = deduplicate_source_items([bp, bc])
    assert len(merged) == 1
    m = merged[0]
    assert m.raw_metadata["beatport_id"] == 500
    assert m.raw_metadata["bandcamp_album_id"] == 9999
    assert sorted(m.raw_metadata["seen_on_sources"]) == ["bandcamp", "beatport"]
