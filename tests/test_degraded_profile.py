"""Degraded profile mode (issue #12) — tunefinder.__main__._load_profile_state.

When fetch_all_tracks fails, the run must fall back to the last-saved profile
state (data/artist_profiles.json, data/genre_affinity.json,
data/known_tracks.json), alert on live runs (log-only on dry runs), and skip
all save_* calls. The helper is tested directly — no full cmd_run.
"""
import logging

import pytest

from src.models import ArtistProfile, Track
from src.pipeline.profile import (
    build_known_track_keys,
    save_artist_profiles,
    save_genre_affinity,
    save_known_tracks,
)
from tunefinder.__main__ import _load_profile_state


class _StubSettings:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.testing_use_fixtures = False


def _seed_saved_state(data_dir):
    """Write synthetic last-saved profile state into data_dir."""
    tracks = [
        Track(artist="Calibre", title="Falling", recurrence_count=3, genres_seen=["dnb"]),
        Track(artist="Sully", title="Swandive", recurrence_count=1, genres_seen=["ukg"]),
    ]
    profiles = {
        "Calibre": ArtistProfile(name="Calibre", play_count=3, genres_seen=["dnb"]),
        "Sully": ArtistProfile(name="Sully", play_count=1, genres_seen=["ukg"]),
    }
    affinity = {"dnb": 0.75, "ukg": 0.25}
    save_known_tracks(tracks, str(data_dir))
    save_artist_profiles(profiles, str(data_dir))
    save_genre_affinity(affinity, str(data_dir))
    return tracks, profiles, affinity


def _failing_fetch(settings):
    raise ConnectionError("catalog API unreachable")


def test_fallback_returns_last_saved_state(tmp_path, monkeypatch):
    tracks, profiles, affinity = _seed_saved_state(tmp_path)
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", _failing_fetch)

    alerts = []
    out_profiles, out_affinity, out_keys, used_fallback = _load_profile_state(
        _StubSettings(tmp_path), logging.getLogger("test"), dry_run=False,
        post_alert_fn=alerts.append,
    )

    assert used_fallback is True
    assert set(out_profiles) == {"Calibre", "Sully"}
    assert out_profiles["Calibre"].play_count == 3
    assert out_affinity == affinity
    # known_tracks.json stores build_known_track_keys output — round-trips exactly
    assert out_keys == build_known_track_keys(tracks)


def test_fallback_live_run_posts_alert(tmp_path, monkeypatch):
    _seed_saved_state(tmp_path)
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", _failing_fetch)

    alerts = []
    _load_profile_state(
        _StubSettings(tmp_path), logging.getLogger("test"), dry_run=False,
        post_alert_fn=alerts.append,
    )

    assert len(alerts) == 1
    assert "Profile fetch failed" in alerts[0]
    assert "2 cached artist profiles" in alerts[0]


def test_fallback_dry_run_logs_instead_of_alerting(tmp_path, monkeypatch, caplog):
    _seed_saved_state(tmp_path)
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", _failing_fetch)

    alerts = []
    with caplog.at_level(logging.WARNING):
        _load_profile_state(
            _StubSettings(tmp_path), logging.getLogger("test"), dry_run=True,
            post_alert_fn=alerts.append,
        )

    assert alerts == []
    assert any("ALERT (dry-run, not posted)" in r.message for r in caplog.records)


def test_fallback_empty_state_alert_says_discovery_only(tmp_path, monkeypatch):
    # No saved files at all in tmp_path — fallback state is also empty
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", _failing_fetch)

    alerts = []
    out_profiles, out_affinity, out_keys, used_fallback = _load_profile_state(
        _StubSettings(tmp_path), logging.getLogger("test"), dry_run=False,
        post_alert_fn=alerts.append,
    )

    assert used_fallback is True
    assert out_profiles == {}
    assert out_affinity == {}
    assert out_keys == set()
    assert len(alerts) == 1
    assert "discovery-only" in alerts[0]


def test_fallback_skips_save_calls(tmp_path, monkeypatch):
    _seed_saved_state(tmp_path)
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", _failing_fetch)

    saves = []
    monkeypatch.setattr("src.pipeline.profile.save_known_tracks",
                        lambda *a, **k: saves.append("known_tracks"))
    monkeypatch.setattr("src.pipeline.profile.save_artist_profiles",
                        lambda *a, **k: saves.append("artist_profiles"))
    monkeypatch.setattr("src.pipeline.profile.save_genre_affinity",
                        lambda *a, **k: saves.append("genre_affinity"))

    _load_profile_state(
        _StubSettings(tmp_path), logging.getLogger("test"), dry_run=False,
        post_alert_fn=lambda msg: None,
    )

    assert saves == []


def test_success_path_saves_and_no_alert(tmp_path, monkeypatch):
    fresh_tracks = [
        Track(artist="Om Unit", title="Nautilus", recurrence_count=2, genres_seen=["dnb"]),
    ]
    monkeypatch.setattr("src.fetchers.catalog.fetch_all_tracks", lambda settings: fresh_tracks)

    saves = []
    monkeypatch.setattr("src.pipeline.profile.save_known_tracks",
                        lambda *a, **k: saves.append("known_tracks"))
    monkeypatch.setattr("src.pipeline.profile.save_artist_profiles",
                        lambda *a, **k: saves.append("artist_profiles"))
    monkeypatch.setattr("src.pipeline.profile.save_genre_affinity",
                        lambda *a, **k: saves.append("genre_affinity"))

    alerts = []
    out_profiles, out_affinity, out_keys, used_fallback = _load_profile_state(
        _StubSettings(tmp_path), logging.getLogger("test"), dry_run=False,
        post_alert_fn=alerts.append,
    )

    assert used_fallback is False
    assert alerts == []
    assert set(saves) == {"known_tracks", "artist_profiles", "genre_affinity"}
    assert "Om Unit" in out_profiles
    assert out_keys == build_known_track_keys(fresh_tracks)
