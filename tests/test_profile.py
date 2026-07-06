"""Tests for src/pipeline/profile.py — genre affinity build + persistence (issue #3)."""
import json
import os
import tempfile

from src.models import Track
from src.pipeline.profile import (
    build_genre_affinity,
    load_genre_affinity,
    save_genre_affinity,
)


# ---------------------------------------------------------------------------
# build_genre_affinity
# ---------------------------------------------------------------------------

def test_build_genre_affinity_weights_by_recurrence_count():
    tracks = [
        Track(artist="A", title="T1", genres_seen=["dnb"], recurrence_count=400),
        Track(artist="B", title="T2", genres_seen=["downtempo"], recurrence_count=3),
    ]
    affinity = build_genre_affinity(tracks)
    assert affinity["dnb"] == 400 / 403
    assert affinity["downtempo"] == 3 / 403
    assert sum(affinity.values()) == 1.0


def test_build_genre_affinity_empty_tracks_returns_empty_dict():
    assert build_genre_affinity([]) == {}


def test_build_genre_affinity_tracks_with_no_genres_returns_empty_dict():
    tracks = [Track(artist="A", title="T1", genres_seen=[], recurrence_count=5)]
    assert build_genre_affinity(tracks) == {}


def test_build_genre_affinity_multiple_genres_per_track_each_credited():
    # A track tagged with two genres contributes its full recurrence_count to both —
    # genre tags aren't mutually exclusive, so this is not double counting a single fact.
    tracks = [
        Track(artist="A", title="T1", genres_seen=["house", "techno"], recurrence_count=2),
    ]
    affinity = build_genre_affinity(tracks)
    assert affinity["house"] == 0.5
    assert affinity["techno"] == 0.5


def test_build_genre_affinity_shares_sum_to_one_with_many_genres():
    tracks = [
        Track(artist="A", title="T1", genres_seen=["dnb"], recurrence_count=10),
        Track(artist="B", title="T2", genres_seen=["house"], recurrence_count=5),
        Track(artist="C", title="T3", genres_seen=["techno"], recurrence_count=1),
    ]
    affinity = build_genre_affinity(tracks)
    assert abs(sum(affinity.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_genre_affinity_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        affinity = {"dnb": 0.8, "downtempo": 0.2}
        save_genre_affinity(affinity, tmpdir)
        loaded = load_genre_affinity(tmpdir)
        assert loaded == affinity


def test_load_genre_affinity_missing_file_returns_empty_dict():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert load_genre_affinity(tmpdir) == {}


def test_save_genre_affinity_creates_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        nested = os.path.join(tmpdir, "nested", "data")
        save_genre_affinity({"house": 1.0}, nested)
        assert os.path.exists(os.path.join(nested, "genre_affinity.json"))


def test_save_genre_affinity_writes_valid_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        save_genre_affinity({"dnb": 1.0}, tmpdir)
        with open(os.path.join(tmpdir, "genre_affinity.json")) as f:
            data = json.load(f)
        assert data == {"dnb": 1.0}
