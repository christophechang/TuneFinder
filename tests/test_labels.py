"""Tests for src/pipeline/labels.py — persistent label affinity memory (issue #5)."""
import argparse
import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.models import ArtistProfile, Candidate, SourceItem, Track
from src.pipeline.labels import (
    fresh_label_artist_data,
    load_label_affinity,
    save_label_affinity,
    update_label_affinity,
)
from src.pipeline.ranker import ScoringWeights, _merge_label_knowledge, rank_candidates, rank_candidates_mix_prep


def _candidate(artist="A", title="T", label=None, **kw):
    return Candidate(artist=artist, title=title, link="", source="s", label=label, **kw)


# ---------------------------------------------------------------------------
# update_label_affinity
# ---------------------------------------------------------------------------

def test_update_label_affinity_records_known_artist_association():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    now_iso = "2026-07-06T00:00:00+00:00"

    store = update_label_affinity({}, candidates, profiles_lower, None, now_iso)

    assert "astrophonica" in store
    entry = store["astrophonica"]
    assert entry["display_name"] == "Astrophonica"
    assert entry["artists"]["sully"] == {"name": "Sully", "last_seen": now_iso}
    assert entry["first_seen"] == now_iso
    assert entry["last_seen"] == now_iso


def test_update_label_affinity_ignores_unknown_artist():
    candidates = [_candidate(artist="Nobody", label="Some Label")]
    store = update_label_affinity({}, candidates, {}, None, "2026-07-06T00:00:00+00:00")
    assert store == {}


def test_update_label_affinity_ignores_candidate_without_label():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label=None)]
    store = update_label_affinity({}, candidates, profiles_lower, None, "2026-07-06T00:00:00+00:00")
    assert store == {}


def test_update_label_affinity_alias_resolution():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre")}
    aliases = {"dave skinner": "calibre"}
    candidates = [_candidate(artist="Dave Skinner", label="Signature")]
    store = update_label_affinity({}, candidates, profiles_lower, aliases, "2026-07-06T00:00:00+00:00")
    assert store["signature"]["artists"]["calibre"]["name"] == "Calibre"


def test_update_label_affinity_no_alias_map_does_not_resolve():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre")}
    candidates = [_candidate(artist="Dave Skinner", label="Signature")]
    store = update_label_affinity({}, candidates, profiles_lower, None, "2026-07-06T00:00:00+00:00")
    assert store == {}


def test_update_label_affinity_accumulates_multiple_artists_on_same_label():
    profiles_lower = {
        "sully": ArtistProfile(name="Sully"),
        "skee mask": ArtistProfile(name="Skee Mask"),
    }
    candidates = [
        _candidate(artist="Sully", label="Ilian Tape"),
        _candidate(artist="Skee Mask", label="Ilian Tape"),
    ]
    store = update_label_affinity({}, candidates, profiles_lower, None, "2026-07-06T00:00:00+00:00")
    assert set(store["ilian tape"]["artists"]) == {"sully", "skee mask"}


def test_update_label_affinity_multi_artist_credit_string_both_recorded():
    """"Bakey, Kasia" splits into two individual artists — both known — both
    should be credited to the label."""
    profiles_lower = {
        "bakey": ArtistProfile(name="Bakey"),
        "kasia": ArtistProfile(name="Kasia"),
    }
    candidates = [_candidate(artist="Bakey, Kasia", label="Metalheadz")]
    store = update_label_affinity({}, candidates, profiles_lower, None, "2026-07-06T00:00:00+00:00")
    assert set(store["metalheadz"]["artists"]) == {"bakey", "kasia"}


def test_update_label_affinity_updates_display_name_to_latest_written_form():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    now1 = "2026-06-01T00:00:00+00:00"
    now2 = "2026-07-01T00:00:00+00:00"
    store = update_label_affinity(
        {}, [_candidate(artist="Sully", label="astrophonica records")], profiles_lower, None, now1
    )
    store = update_label_affinity(
        store, [_candidate(artist="Sully", label="Astrophonica Records")], profiles_lower, None, now2
    )
    assert store["astrophonica records"]["display_name"] == "Astrophonica Records"


def test_update_label_affinity_first_seen_set_once_last_seen_advances():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    first = "2026-01-01T00:00:00+00:00"
    later = "2026-06-01T00:00:00+00:00"
    store = update_label_affinity({}, [_candidate(artist="Sully", label="Astrophonica")], profiles_lower, None, first)
    store = update_label_affinity(store, [_candidate(artist="Sully", label="Astrophonica")], profiles_lower, None, later)
    assert store["astrophonica"]["first_seen"] == first
    assert store["astrophonica"]["last_seen"] == later
    assert store["astrophonica"]["artists"]["sully"]["last_seen"] == later


def test_update_label_affinity_is_pure_does_not_mutate_input_store():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    original = {}
    update_label_affinity(
        original, [_candidate(artist="Sully", label="Astrophonica")], profiles_lower, None, "2026-07-06T00:00:00+00:00"
    )
    assert original == {}


# ---------------------------------------------------------------------------
# fresh_label_artist_data — staleness expiry
# ---------------------------------------------------------------------------

def _store_with_artist(label_key, artist_key, name, last_seen_iso):
    return {
        label_key: {
            "display_name": name,
            "artists": {artist_key: {"name": name, "last_seen": last_seen_iso}},
            "first_seen": last_seen_iso,
            "last_seen": last_seen_iso,
        }
    }


def test_fresh_label_artist_data_within_window_included():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    last_seen = (now - timedelta(weeks=5)).isoformat()
    store = _store_with_artist("signature", "calibre", "Calibre", last_seen)

    counts, names = fresh_label_artist_data(store, max_age_weeks=26, now=now)

    assert counts == {"signature": 1}
    assert names == {"signature": ["Calibre"]}


def test_fresh_label_artist_data_excludes_stale_label():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    last_seen = (now - timedelta(weeks=30)).isoformat()
    store = _store_with_artist("signature", "calibre", "Calibre", last_seen)

    counts, names = fresh_label_artist_data(store, max_age_weeks=26, now=now)

    assert counts == {}
    assert names == {}


def test_fresh_label_artist_data_partial_staleness_keeps_fresh_artist_only():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    fresh_seen = (now - timedelta(weeks=1)).isoformat()
    stale_seen = (now - timedelta(weeks=30)).isoformat()
    store = {
        "signature": {
            "display_name": "Signature",
            "artists": {
                "calibre": {"name": "Calibre", "last_seen": fresh_seen},
                "old artist": {"name": "Old Artist", "last_seen": stale_seen},
            },
            "first_seen": stale_seen,
            "last_seen": fresh_seen,
        }
    }

    counts, names = fresh_label_artist_data(store, max_age_weeks=26, now=now)

    assert counts == {"signature": 1}
    assert names == {"signature": ["Calibre"]}


def test_fresh_label_artist_data_handles_missing_last_seen_gracefully():
    store = {
        "signature": {
            "display_name": "Signature",
            "artists": {"calibre": {"name": "Calibre"}},
            "first_seen": "",
            "last_seen": "",
        }
    }
    counts, names = fresh_label_artist_data(store, max_age_weeks=26)
    assert counts == {}
    assert names == {}


def test_fresh_label_artist_data_defaults_now_to_current_time():
    now = datetime.now(timezone.utc)
    last_seen = (now - timedelta(weeks=1)).isoformat()
    store = _store_with_artist("signature", "calibre", "Calibre", last_seen)
    counts, names = fresh_label_artist_data(store, max_age_weeks=26)
    assert counts == {"signature": 1}


def test_fresh_label_artist_data_empty_store_returns_empty():
    counts, names = fresh_label_artist_data({}, max_age_weeks=26)
    assert counts == {}
    assert names == {}


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_load_label_affinity_missing_file_returns_empty_dict(tmp_path):
    assert load_label_affinity(str(tmp_path)) == {}


def test_save_load_round_trip(tmp_path):
    store = _store_with_artist("signature", "calibre", "Calibre", "2026-07-06T00:00:00+00:00")
    save_label_affinity(store, str(tmp_path))
    loaded = load_label_affinity(str(tmp_path))
    assert loaded == store


def test_save_label_affinity_writes_json_file(tmp_path):
    save_label_affinity({}, str(tmp_path))
    assert os.path.exists(os.path.join(str(tmp_path), "label_affinity.json"))


# ---------------------------------------------------------------------------
# Ranker union — _merge_label_knowledge
# ---------------------------------------------------------------------------

def test_merge_label_knowledge_none_memory_is_noop():
    relevant, counts, names = _merge_label_knowledge({"a"}, {"a": 1}, {"a": ["X"]}, None)
    assert relevant == {"a"}
    assert counts == {"a": 1}
    assert names == {"a": ["X"]}


def test_merge_label_knowledge_adds_memory_only_label():
    relevant, counts, names = _merge_label_knowledge(
        set(), {}, {}, ({"old label": 2}, {"old label": ["Amit", "Calibre"]})
    )
    assert "old label" in relevant
    assert counts["old label"] == 2
    assert names["old label"] == ["Amit", "Calibre"]


def test_merge_label_knowledge_counts_take_max_not_sum():
    relevant, counts, names = _merge_label_knowledge(
        {"signature"}, {"signature": 3}, {"signature": ["Amit"]},
        ({"signature": 1}, {"signature": ["Amit"]}),
    )
    assert counts["signature"] == 3  # max(3, 1), never summed to 4


def test_merge_label_knowledge_names_are_unioned_and_sorted():
    relevant, counts, names = _merge_label_knowledge(
        {"signature"}, {"signature": 1}, {"signature": ["Calibre"]},
        ({"signature": 1}, {"signature": ["Amit"]}),
    )
    assert names["signature"] == ["Amit", "Calibre"]


# ---------------------------------------------------------------------------
# rank_candidates / rank_candidates_mix_prep end-to-end with label_memory
# ---------------------------------------------------------------------------

class _RankSettings:
    pipeline_top_picks_count = 5
    pipeline_label_watch_count = 5
    pipeline_artist_watch_count = 5
    pipeline_wildcard_count = 5
    pipeline_mix_prep_top_picks_count = 5
    pipeline_mix_prep_deep_cuts_count = 5
    pipeline_section_min_score = 0.0

    def __init__(self, data_dir):
        self.data_dir = data_dir

    @staticmethod
    def scoring_weights():
        return ScoringWeights()

    @staticmethod
    def artist_aliases():
        return {}


def test_rank_candidates_label_memory_fires_label_match_for_absent_artist(tmp_path):
    """A label remembered from a past run (issue #5) fires label_match on a
    fresh candidate this week even though the known artist who made the label
    relevant doesn't appear anywhere in this week's corpus."""
    candidate = _candidate(artist="Totally Unknown", title="New One", label="Old Label")
    label_memory = ({"old label": 1}, {"old label": ["Amit"]})

    sections, label_artists = rank_candidates(
        [candidate], {}, _RankSettings(str(tmp_path)), label_memory=label_memory,
    )

    assert any(s.code == "label_match" for s in candidate.signals)
    assert label_artists["old label"] == ["Amit"]


def test_rank_candidates_no_label_memory_reproduces_amnesiac_behaviour(tmp_path):
    """Sanity check: without label_memory, the same label with no known artist
    this week fires no label_match — proves the fixture above is genuinely
    memory-driven, not some other match."""
    candidate = _candidate(artist="Totally Unknown", title="New One", label="Old Label")

    sections, label_artists = rank_candidates([candidate], {}, _RankSettings(str(tmp_path)))

    assert not any(s.code == "label_match" for s in candidate.signals)


def test_rank_candidates_mix_prep_label_memory_fires_label_match(tmp_path):
    candidate = _candidate(artist="Totally Unknown", title="New One", label="Old Label")
    label_memory = ({"old label": 1}, {"old label": ["Amit"]})

    sections, label_artists = rank_candidates_mix_prep(
        [candidate], {}, _RankSettings(str(tmp_path)), label_memory=label_memory,
    )

    assert any(s.code == "label_match" for s in candidate.signals)


# ---------------------------------------------------------------------------
# Scene one-hop signal (issue #6) — end-to-end through rank_candidates with a
# memory-only label (the label affinity store remembers "Amit" on this label,
# but no known artist appears anywhere in this week's corpus)
# ---------------------------------------------------------------------------

def test_rank_candidates_scene_adjacent_fires_via_memory_only_label(tmp_path):
    profiles = {"Amit": ArtistProfile(name="Amit", play_count=3)}
    candidate = _candidate(artist="Totally Unknown", title="New One", label="Old Label")
    label_memory = ({"old label": 1}, {"old label": ["Amit"]})

    sections, label_artists = rank_candidates(
        [candidate], profiles, _RankSettings(str(tmp_path)), label_memory=label_memory,
    )

    scene_sig = next(s for s in candidate.signals if s.code == "scene_adjacent")
    assert scene_sig.explanation == "Label-mate of Amit on Old Label."
    # Deliberately stacks with label_match — both fired off the same
    # memory-derived label relevance fact.
    assert any(s.code == "label_match" for s in candidate.signals)


def test_rank_candidates_scene_adjacent_absent_without_memory(tmp_path):
    """Sanity check: without label_memory, the same setup has no relevant
    label at all this week, so neither label_match nor scene_adjacent fire —
    proves the fixture above is genuinely memory-driven."""
    profiles = {"Amit": ArtistProfile(name="Amit", play_count=3)}
    candidate = _candidate(artist="Totally Unknown", title="New One", label="Old Label")

    rank_candidates([candidate], profiles, _RankSettings(str(tmp_path)))

    assert not any(s.code == "scene_adjacent" for s in candidate.signals)


# ---------------------------------------------------------------------------
# cmd_run / cmd_mix_prep — dry-run must not write, live run must
# ---------------------------------------------------------------------------

def _run_settings(data_dir):
    settings = MagicMock()
    settings.data_dir = data_dir
    settings.pipeline_release_date_window_days = None
    settings.alerts_source_drop_threshold_pct = 50
    settings.alerts_min_history_runs = 2
    settings.scoring_weights = MagicMock(return_value=ScoringWeights())
    settings.artist_aliases = MagicMock(return_value={})
    settings.pipeline_top_picks_count = 5
    settings.pipeline_label_watch_count = 5
    settings.pipeline_artist_watch_count = 5
    settings.pipeline_wildcard_count = 3
    settings.pipeline_section_min_score = 0.0
    settings.validate = MagicMock()
    return settings


def _mix_prep_settings(data_dir):
    settings = _run_settings(data_dir)
    settings.pipeline_mix_prep_top_picks_count = 20
    settings.pipeline_mix_prep_deep_cuts_count = 20
    settings.pipeline_genre_exclusions = {}
    settings.discord_mix_prep_channel = "mix-prep"
    return settings


def _known_source_item():
    return SourceItem(
        source="beatport", artist="Sully", title="New Track", link="https://example.com/x",
        label="Astrophonica", release_date=None, genre_tags=["breaks"],
    )


def _known_track():
    return Track(artist="Sully", title="Old Track", recurrence_count=2, genres_seen=["breaks"])


def test_cmd_run_dry_run_does_not_write_label_affinity(tmp_path):
    from tunefinder.__main__ import cmd_run

    settings = _run_settings(str(tmp_path))
    args = argparse.Namespace(dry_run=True)

    with patch("tunefinder.__main__.load_settings", return_value=settings), \
         patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.fetch_all_sources", return_value=([_known_source_item()], {})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        cmd_run(args)

    assert not (tmp_path / "label_affinity.json").exists()


def test_cmd_run_live_writes_label_affinity_with_known_artist_association(tmp_path):
    from tunefinder.__main__ import cmd_run

    settings = _run_settings(str(tmp_path))
    args = argparse.Namespace(dry_run=False)

    with patch("tunefinder.__main__.load_settings", return_value=settings), \
         patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.fetch_all_sources", return_value=([_known_source_item()], {})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        cmd_run(args)

    store_path = tmp_path / "label_affinity.json"
    assert store_path.exists()
    store = json.loads(store_path.read_text())
    assert "astrophonica" in store
    assert "sully" in store["astrophonica"]["artists"]


def test_cmd_mix_prep_dry_run_does_not_write_label_affinity(tmp_path):
    from tunefinder.__main__ import cmd_mix_prep

    settings = _mix_prep_settings(str(tmp_path))
    args = argparse.Namespace(genre="breaks", dry_run=True)

    with patch("tunefinder.__main__.load_settings", return_value=settings), \
         patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.fetch_all_sources", return_value=([_known_source_item()], {})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        cmd_mix_prep(args)

    assert not (tmp_path / "label_affinity.json").exists()


def test_cmd_mix_prep_live_writes_label_affinity(tmp_path):
    from tunefinder.__main__ import cmd_mix_prep

    settings = _mix_prep_settings(str(tmp_path))
    args = argparse.Namespace(genre="breaks", dry_run=False)

    with patch("tunefinder.__main__.load_settings", return_value=settings), \
         patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.fetch_all_sources", return_value=([_known_source_item()], {})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        cmd_mix_prep(args)

    store_path = tmp_path / "label_affinity.json"
    assert store_path.exists()
    store = json.loads(store_path.read_text())
    assert "astrophonica" in store


# ---------------------------------------------------------------------------
# backfill-labels — replay against synthetic archives, idempotent convergence
# ---------------------------------------------------------------------------

def _write_gzip_archive(path, items):
    payload = json.dumps(items).encode("utf-8")
    with gzip.open(str(path), "wb") as f:
        f.write(payload)


def test_backfill_labels_idempotent_convergence(tmp_path):
    from tunefinder.__main__ import cmd_backfill_labels

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    profiles_path = tmp_path / "artist_profiles.json"
    profiles_path.write_text(json.dumps({
        "Sully": {"name": "Sully", "play_count": 4, "genres_seen": ["breaks"], "track_titles": []},
    }))

    item1 = {
        "source": "beatport", "artist": "Sully", "title": "Track A", "link": "https://x",
        "label": "Astrophonica", "release_date": None, "release_name": None,
        "genre_tags": ["breaks"], "raw_metadata": {},
    }
    item2 = {
        "source": "bandcamp", "artist": "Sully", "title": "Track B", "link": "https://y",
        "label": "Osiris Music", "release_date": None, "release_name": None,
        "genre_tags": ["dnb"], "raw_metadata": {},
    }
    _write_gzip_archive(archive_dir / "source_items_2026-W01.json.gz", [item1])
    _write_gzip_archive(archive_dir / "source_items_2026-W02.json.gz", [item2])

    settings = MagicMock()
    settings.data_dir = str(tmp_path)
    settings.artist_aliases = MagicMock(return_value={})

    store_path = tmp_path / "label_affinity.json"

    with patch("tunefinder.__main__.load_settings", return_value=settings):
        cmd_backfill_labels(argparse.Namespace())
        first = json.loads(store_path.read_text())

        cmd_backfill_labels(argparse.Namespace())
        second = json.loads(store_path.read_text())

    assert first == second
    assert set(first.keys()) == {"astrophonica", "osiris music"}
    assert "sully" in first["astrophonica"]["artists"]
    assert "sully" in first["osiris music"]["artists"]


def test_backfill_labels_no_archives_leaves_store_untouched(tmp_path):
    from tunefinder.__main__ import cmd_backfill_labels

    settings = MagicMock()
    settings.data_dir = str(tmp_path)
    settings.artist_aliases = MagicMock(return_value={})

    with patch("tunefinder.__main__.load_settings", return_value=settings):
        cmd_backfill_labels(argparse.Namespace())

    assert not (tmp_path / "label_affinity.json").exists()
