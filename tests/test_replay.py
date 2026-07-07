"""Tests for tunefinder replay (src/pipeline/replay.py)."""
import json
import os
from datetime import date, timedelta

import pytest

from src.config import Settings
from src.fetchers import archive_source_items
from src.models import SourceItem
from src.pipeline.replay import (
    build_overridden_settings,
    replay_week,
    _reference_date,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings_data(data_dir, window_days=28, min_score=0.0, scoring=None):
    return {
        "data_dir": str(data_dir),
        "pipeline": {
            "top_picks_count": 5,
            "label_watch_count": 5,
            "artist_watch_count": 5,
            "wildcard_count": 3,
            "section_min_score": min_score,
            "release_date_window_days": window_days,
        },
        "scoring": scoring or {},
    }


def _settings(data_dir, **kwargs):
    return Settings(_settings_data(data_dir, **kwargs))


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _source_item(artist, title, source="beatport", genre_tags=None,
                 release_date=None, label=None):
    return SourceItem(
        source=source,
        artist=artist,
        title=title,
        link=f"https://{source}.example.com/x",
        label=label,
        release_date=release_date,
        release_name=None,
        genre_tags=genre_tags or ["dnb"],
        raw_metadata={},
    )


def _profiles(**by_name):
    """by_name: name -> (play_count, genres)."""
    out = {}
    for name, (play_count, genres) in by_name.items():
        out[name] = {
            "name": name,
            "play_count": play_count,
            "genres_seen": genres,
            "track_titles": [],
        }
    return out


def _history_record(artist, title, report_id, recommended_at=None):
    return {
        "artist": artist,
        "title": title,
        "link": "https://x.com",
        "source": "beatport",
        "recommended_at": recommended_at or "2026-06-01T12:00:00+00:00",
        "report_id": report_id,
        "track_no": 1,
        "signal_codes": ["known_artist"],
        "genre_tags": ["dnb"],
        "score": 3.5,
        "label": None,
    }


def _setup(data_dir, week, items, profiles=None, known_keys=None, history=None):
    _write_json(os.path.join(data_dir, "artist_profiles.json"), profiles or {})
    _write_json(os.path.join(data_dir, "known_tracks.json"), known_keys or [])
    _write_json(os.path.join(data_dir, "recommendation_history.json"), history or [])
    archive_source_items(items, str(data_dir), week)


# ---------------------------------------------------------------------------
# _reference_date
# ---------------------------------------------------------------------------

def test_reference_date_is_iso_week_sunday():
    assert _reference_date("2026-W23") == date.fromisocalendar(2026, 23, 7)
    # isoweekday 7 == Sunday
    assert _reference_date("2026-W23").isoweekday() == 7


def test_reference_date_rejects_garbage():
    with pytest.raises(ValueError, match="ISO week"):
        _reference_date("not-a-week")


# ---------------------------------------------------------------------------
# Release-date window is evaluated against the ARCHIVE week, not today
# ---------------------------------------------------------------------------

def test_release_window_uses_archive_week_not_now(tmp_path):
    """The key replay invariant: a track dated within the archived week passes
    the release-date filter even when replayed long after that week."""
    week = "2025-W10"  # far enough in the past that today() would drop it
    ref = date.fromisocalendar(2025, 10, 7)
    in_week = (ref - timedelta(days=2)).isoformat()

    _setup(
        tmp_path, week,
        items=[_source_item("Calibre", "New Dawn", genre_tags=["dnb"], release_date=in_week)],
        profiles=_profiles(Calibre=(4, ["dnb"])),
    )
    out = replay_week(week, [], _settings(tmp_path, window_days=28))
    assert "REPLAY — offline reconstruction" in out
    assert "Calibre — New Dawn" in out


def test_release_window_still_drops_pre_week_releases(tmp_path):
    """A track dated well before the archive week's window is still filtered."""
    week = "2025-W10"
    ref = date.fromisocalendar(2025, 10, 7)
    stale = (ref - timedelta(days=90)).isoformat()

    _setup(
        tmp_path, week,
        items=[_source_item("Ghost", "Ancient", genre_tags=["dnb"], release_date=stale)],
    )
    out = replay_week(week, [], _settings(tmp_path, window_days=28))
    assert "Ghost — Ancient" not in out


# ---------------------------------------------------------------------------
# --set override parsing / typing / isolation
# ---------------------------------------------------------------------------

def test_override_dotted_path_and_yaml_typing(tmp_path):
    base = _settings(tmp_path, scoring={"w_known_artist": 3.0})
    new = build_overridden_settings(
        base,
        [
            "scoring.w_known_artist=2.0",
            "pipeline.section_min_score=1.5",
            "testing.use_fixtures=true",
            "catalog.user_url=abc",
        ],
    )
    # dotted path lands in nested dict, YAML scalar typing preserved
    assert new._data["scoring"]["w_known_artist"] == 2.0
    assert isinstance(new._data["scoring"]["w_known_artist"], float)
    assert new._data["pipeline"]["section_min_score"] == 1.5
    assert new._data["testing"]["use_fixtures"] is True
    assert new._data["catalog"]["user_url"] == "abc"


def test_override_does_not_mutate_original(tmp_path):
    base = _settings(tmp_path, scoring={"w_known_artist": 3.0})
    build_overridden_settings(base, ["scoring.w_known_artist=9.0"])
    assert base._data["scoring"]["w_known_artist"] == 3.0


def test_override_bad_format_raises(tmp_path):
    base = _settings(tmp_path)
    with pytest.raises(ValueError, match="expected dotted.path=value"):
        build_overridden_settings(base, ["no_equals_sign"])


def test_scoring_override_changes_ranking(tmp_path):
    """A section_min_score override that floors out every candidate changes the
    replayed report (candidate no longer surfaced)."""
    week = "2026-W20"
    ref = date.fromisocalendar(2026, 20, 7)
    in_week = (ref - timedelta(days=1)).isoformat()
    _setup(
        tmp_path, week,
        items=[_source_item("Calibre", "New Dawn", genre_tags=["dnb"], release_date=in_week)],
        profiles=_profiles(Calibre=(4, ["dnb"])),
    )
    default_out = replay_week(week, [], _settings(tmp_path))
    override_out = replay_week(week, ["pipeline.section_min_score=100"], _settings(tmp_path))
    assert "Calibre — New Dawn" in default_out
    assert "Calibre — New Dawn" not in override_out


# ---------------------------------------------------------------------------
# Missing archive
# ---------------------------------------------------------------------------

def test_missing_archive_lists_available_weeks(tmp_path):
    _setup(
        tmp_path, "2026-W23",
        items=[_source_item("Calibre", "New Dawn")],
    )
    out = replay_week("2099-W01", [], _settings(tmp_path))
    assert "No archive found for week '2099-W01'" in out
    assert "2026-W23" in out


def test_missing_archive_no_archives_at_all(tmp_path):
    _write_json(os.path.join(tmp_path, "artist_profiles.json"), {})
    out = replay_week("2026-W23", [], _settings(tmp_path))
    assert "No archive found" in out
    assert "No archived weeks found" in out


# ---------------------------------------------------------------------------
# Diff vs recommendation_history
# ---------------------------------------------------------------------------

def test_diff_categorises_still_new_and_gone(tmp_path):
    week = "2026-W23"
    ref = date.fromisocalendar(2026, 23, 7)
    in_week = (ref - timedelta(days=1)).isoformat()

    items = [
        _source_item("Calibre", "New Dawn", genre_tags=["dnb"], release_date=in_week),
        _source_item("Newcomer", "Fresh Cut", genre_tags=["dnb"], release_date=in_week),
    ]
    history = [
        # Recommended that week AND still surfaces in replay -> "="
        _history_record("Calibre", "New Dawn", report_id=week),
        # Recorded that week but not in the archive -> "-"
        _history_record("Gone Artist", "Vanished", report_id=week),
    ]
    _setup(
        tmp_path, week, items=items,
        profiles=_profiles(Calibre=(4, ["dnb"])),
        history=history,
    )
    out = replay_week(week, [], _settings(tmp_path))

    assert "=== DIFF vs recommendation_history ===" in out
    assert "    = Calibre — New Dawn" in out       # would still recommend
    assert "    + Newcomer — Fresh Cut" in out     # newly surfaced
    assert "    - Gone Artist — Vanished" in out    # no longer surfaced


def test_diff_no_history_for_week(tmp_path):
    week = "2026-W21"
    ref = date.fromisocalendar(2026, 21, 7)
    in_week = (ref - timedelta(days=1)).isoformat()
    _setup(
        tmp_path, week,
        items=[_source_item("Calibre", "New Dawn", genre_tags=["dnb"], release_date=in_week)],
        profiles=_profiles(Calibre=(4, ["dnb"])),
    )
    out = replay_week(week, [], _settings(tmp_path))
    assert "No recommendation_history records for 2026-W21" in out
