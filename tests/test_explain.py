"""Tests for tunefinder explain (src/pipeline/explain.py)."""
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.pipeline.explain import explain_track


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _settings(data_dir, window_days=28, min_score=0.0):
    from src.pipeline.ranker import ScoringWeights
    s = MagicMock()
    s.data_dir = data_dir
    s.pipeline_release_date_window_days = window_days
    s.pipeline_section_min_score = min_score
    s.pipeline_top_picks_count = 5
    s.pipeline_label_watch_count = 5
    s.pipeline_artist_watch_count = 5
    s.pipeline_wildcard_count = 3
    s.scoring_weights = MagicMock(return_value=ScoringWeights())
    return s


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _source_item(artist, title, source="beatport", genre_tags=None, release_date=None,
                 raw_metadata=None):
    return {
        "source": source,
        "artist": artist,
        "title": title,
        "link": f"https://{source}.example.com/x",
        "label": "Test Label",
        "release_date": release_date or "2026-06-01",
        "release_name": None,
        "genre_tags": genre_tags or ["house"],
        "raw_metadata": raw_metadata or {},
    }


def _history_record(artist, title, report_id="2026-W22", recommended_at=None):
    return {
        "artist": artist,
        "title": title,
        "link": "https://x.com",
        "source": "beatport",
        "recommended_at": recommended_at or "2026-06-01T12:00:00+00:00",
        "report_id": report_id,
        "track_no": 1,
        "signal_codes": ["known_artist"],
        "genre_tags": ["house"],
        "score": 3.5,
        "label": "Test Label",
    }


def _pool_record(artist, title, added_at=None):
    return {
        "artist": artist,
        "title": title,
        "link": "https://volumo.com/x",
        "source": "volumo",
        "added_at": added_at or "2026-05-01T00:00:00+00:00",
        "last_score": 2.0,
        "label": None,
        "release_date": "2026-04-01",
        "release_name": None,
        "genre_tags": ["house"],
        "raw_metadata": {},
    }


def _setup_data(tmpdir, source_items=None, history=None, pool=None,
                known_keys=None, profiles=None, feedback=None):
    _write_json(os.path.join(tmpdir, "source_items.json"), source_items or [])
    _write_json(os.path.join(tmpdir, "recommendation_history.json"), history or [])
    _write_json(os.path.join(tmpdir, "mix_prep_history.json"), [])
    _write_json(os.path.join(tmpdir, "candidate_pool.json"), pool or [])
    _write_json(os.path.join(tmpdir, "known_tracks.json"), known_keys or [])
    _write_json(os.path.join(tmpdir, "artist_profiles.json"), profiles or {})
    _write_json(os.path.join(tmpdir, "feedback.json"), feedback or [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_unknown_selector():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_data(tmpdir)
        out = explain_track("No One - Nowhere", _settings(tmpdir))
        assert "not in the current week" in out.lower()
        assert "not in pool" in out.lower()


def test_known_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Sully", "Skyline"
        from src.pipeline.dedup import make_dedup_key
        key = make_dedup_key(artist, title)
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title)],
            known_keys=[key],
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir))
        assert "FILTERED" in out
        assert "known-track" in out.lower() or "known" in out.lower()


def test_history_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Calibre", "Phaze"
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title)],
            history=[_history_record(artist, title, report_id="2026-W20")],
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir))
        assert "FILTERED" in out
        assert "2026-W20" in out


def test_window_dropped():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Old Track", "Stale"
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title, release_date="2020-01-01")],
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir, window_days=28))
        assert "FILTERED" in out or "outside" in out.lower()


def test_scored_and_sectioned():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Skee Mask", "Rio Dembo"
        profiles = {
            "Skee Mask": {
                "name": "Skee Mask",
                "play_count": 4,
                "genres_seen": ["electronica"],
                "track_titles": ["Rio Dembo"],
            }
        }
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title, genre_tags=["electronica"])],
            profiles=profiles,
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir, min_score=0.0))
        assert "known_artist" in out
        assert "Signals:" in out


def test_floor_blocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Mystery", "Floored"
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title, genre_tags=["techno"])],
        )
        # High min_score to block everything
        out = explain_track(f"{artist} - {title}", _settings(tmpdir, min_score=100.0))
        assert "below floor" in out or "outscored" in out or "SECTION" in out


def test_pool_resident():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Pool Artist", "Held Back"
        _setup_data(
            tmpdir,
            source_items=[],  # not in fetch
            pool=[_pool_record(artist, title, added_at="2026-05-15T00:00:00+00:00")],
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir, min_score=0.0))
        assert "In pool" in out
        assert "2026-05-15" in out


def test_marked_with_feedback():
    with tempfile.TemporaryDirectory() as tmpdir:
        artist, title = "Marked Artist", "Feedback Track"
        from src.pipeline.dedup import make_dedup_key
        key = make_dedup_key(artist, title)
        feedback = [{
            "key": key,
            "artist": artist,
            "title": title,
            "outcome": "liked",
            "marked_at": "2026-06-10T09:00:00+00:00",
            "report_id": "2026-W23",
            "track_no": 5,
            "history": "weekly",
        }]
        _setup_data(
            tmpdir,
            source_items=[_source_item(artist, title)],
            feedback=feedback,
        )
        out = explain_track(f"{artist} - {title}", _settings(tmpdir))
        assert "liked" in out
        assert "2026-06-10" in out


def test_completely_unknown_selector():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_data(tmpdir)
        out = explain_track("Unknown Artist - Ghost Track", _settings(tmpdir))
        assert "Not in the current week" in out or "not in the current week" in out.lower()
        assert "Not in pool" in out or "not in pool" in out.lower()
