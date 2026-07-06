"""Tests for src/pipeline/profile.py — genre affinity build + persistence (issue #3)."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.models import ArtistProfile, Mix, Track, TrackRef
from src.pipeline.profile import (
    apply_recency_weights,
    build_genre_affinity,
    load_artist_profiles,
    load_genre_affinity,
    resolve_profile,
    save_artist_profiles,
    save_genre_affinity,
)


# ---------------------------------------------------------------------------
# resolve_profile — shared alias/direct resolution helper (issue #4)
# ---------------------------------------------------------------------------

def test_resolve_profile_direct_match():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    assert resolve_profile("Sully", profiles_lower) is profiles_lower["sully"]


def test_resolve_profile_direct_match_is_case_insensitive_and_trims():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    assert resolve_profile("  SULLY  ", profiles_lower) is profiles_lower["sully"]


def test_resolve_profile_no_match_no_aliases_returns_none():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    assert resolve_profile("Unknown", profiles_lower) is None


def test_resolve_profile_resolves_via_alias():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre")}
    aliases = {"dave skinner": "calibre"}
    assert resolve_profile("Dave Skinner", profiles_lower, aliases) is profiles_lower["calibre"]


def test_resolve_profile_direct_match_wins_over_alias():
    """If the written name is itself a known profile, that beats any alias
    entry that happens to share the same key (defensive — shouldn't occur in
    practice, but direct match must always take priority)."""
    profiles_lower = {
        "sully": ArtistProfile(name="Sully"),
        "calibre": ArtistProfile(name="Calibre"),
    }
    aliases = {"sully": "calibre"}
    assert resolve_profile("Sully", profiles_lower, aliases) is profiles_lower["sully"]


def test_resolve_profile_alias_pointing_at_unknown_canonical_returns_none():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    aliases = {"some alias": "nonexistent canonical"}
    assert resolve_profile("Some Alias", profiles_lower, aliases) is None


def test_resolve_profile_none_aliases_behaves_like_no_aliases():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    assert resolve_profile("Unknown", profiles_lower, None) is None


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


# ---------------------------------------------------------------------------
# apply_recency_weights (issue #11 — taste recency weighting)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _mix(published_at: str, tracklist: list[TrackRef], mix_id: str = "m1") -> Mix:
    return Mix(id=mix_id, title="Set", genre="dnb", published_at=published_at, tracklist=tracklist)


def test_apply_recency_weights_recent_mix_near_full_weight_per_track():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix(_NOW.isoformat(), [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 1.0


def test_apply_recency_weights_half_life_old_mix_gives_half_weight():
    published = _NOW - timedelta(days=18 * 30.44)
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix(published.isoformat(), [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 0.5


def test_apply_recency_weights_missing_published_at_mix_is_skipped():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix("", [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 0.0


def test_apply_recency_weights_blank_published_at_mix_is_skipped():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix("   ", [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 0.0


def test_apply_recency_weights_unparseable_published_at_mix_is_skipped():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix("not-a-date", [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 0.0


def test_apply_recency_weights_unmatched_artist_ignored_no_error():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix(_NOW.isoformat(), [TrackRef(artist="Someone Else", title="Track")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 0.0


def test_apply_recency_weights_profile_never_seen_stays_zero():
    profiles = {"Sully": ArtistProfile(name="Sully"), "Calibre": ArtistProfile(name="Calibre")}
    mixes = [_mix(_NOW.isoformat(), [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Calibre"].recency_weighted_play_count == 0.0


def test_apply_recency_weights_sums_across_multiple_occurrences():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [
        _mix(_NOW.isoformat(), [TrackRef(artist="Sully", title="Swandive")], mix_id="m1"),
        _mix(_NOW.isoformat(), [TrackRef(artist="Sully", title="Ballistic")], mix_id="m2"),
    ]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 2.0


def test_apply_recency_weights_splits_collaborative_artist_string():
    profiles = {"Bakey": ArtistProfile(name="Bakey"), "Kasia": ArtistProfile(name="Kasia")}
    mixes = [_mix(_NOW.isoformat(), [TrackRef(artist="Bakey, Kasia", title="Track")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Bakey"].recency_weighted_play_count == 1.0
    assert profiles["Kasia"].recency_weighted_play_count == 1.0


def test_apply_recency_weights_matches_case_insensitively():
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix(_NOW.isoformat(), [TrackRef(artist="  sully  ", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    assert profiles["Sully"].recency_weighted_play_count == 1.0


def test_apply_recency_weights_rounds_to_three_decimal_places():
    published = _NOW - timedelta(days=1 * 30.44)
    profiles = {"Sully": ArtistProfile(name="Sully")}
    mixes = [_mix(published.isoformat(), [TrackRef(artist="Sully", title="Swandive")])]
    apply_recency_weights(profiles, mixes, half_life_months=18.0, now=_NOW)
    expected = round(0.5 ** (1 / 18), 3)
    assert profiles["Sully"].recency_weighted_play_count == expected


# ---------------------------------------------------------------------------
# Persistence round-trip — recency_weighted_play_count (issue #11)
# ---------------------------------------------------------------------------

def test_save_and_load_artist_profiles_round_trips_recency_weighted_play_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        profiles = {"Sully": ArtistProfile(name="Sully", play_count=5, recency_weighted_play_count=2.345)}
        save_artist_profiles(profiles, tmpdir)
        loaded = load_artist_profiles(tmpdir)
        assert loaded["Sully"].recency_weighted_play_count == 2.345
        assert loaded["Sully"].play_count == 5


def test_load_artist_profiles_old_file_without_recency_field_defaults_to_zero():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "artist_profiles.json")
        # Simulates a profile file saved before issue #11 — no recency key at all.
        old_data = {"Sully": {"name": "Sully", "play_count": 5, "genres_seen": [], "track_titles": []}}
        with open(path, "w") as f:
            json.dump(old_data, f)
        loaded = load_artist_profiles(tmpdir)
        assert loaded["Sully"].recency_weighted_play_count == 0.0
        assert loaded["Sully"].play_count == 5
