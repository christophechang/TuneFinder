from src.models import ArtistProfile, Candidate
from src.pipeline.ranker import _build_genre_set


def test_genre_set_includes_baseline():
    gs = _build_genre_set({})
    for g in {"dnb", "breaks", "uk-bass", "ukg", "house", "techno", "electronica", "electronic"}:
        assert g in gs


def test_genre_set_augments_from_profiles_when_threshold_met():
    profiles_lower = {
        "a": ArtistProfile(name="A", genres_seen=["ambient"]),
        "b": ArtistProfile(name="B", genres_seen=["ambient"]),
        "c": ArtistProfile(name="C", genres_seen=["ambient"]),
    }
    gs = _build_genre_set(profiles_lower)
    assert "ambient" in gs


def test_genre_set_skips_below_threshold():
    profiles_lower = {
        "a": ArtistProfile(name="A", genres_seen=["industrial"]),
        "b": ArtistProfile(name="B", genres_seen=["industrial"]),
    }
    gs = _build_genre_set(profiles_lower)
    assert "industrial" not in gs


from src.pipeline.ranker import _build_relevant_labels, _score


def _candidate(artist="A", title="T", label=None, source="s", **kw):
    return Candidate(artist=artist, title=title, link="", source=source, label=label, **kw)


def test_label_signal_scales_with_known_artist_count():
    profiles_lower = {
        "sully": ArtistProfile(name="Sully"),
        "skee mask": ArtistProfile(name="Skee Mask"),
        "calibre": ArtistProfile(name="Calibre"),
    }
    candidates = [
        _candidate(artist="Sully", label="Ilian Tape"),
        _candidate(artist="Skee Mask", label="Ilian Tape"),
        _candidate(artist="Calibre", label="Ilian Tape"),
    ]
    _, counts, names = _build_relevant_labels(candidates, profiles_lower)
    assert counts["ilian tape"] == 3

    target = _candidate(artist="Unknown", title="T", label="Ilian Tape")
    _score(target, profiles_lower, {"ilian tape"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 3.0


def test_label_signal_base_when_one_known_artist():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    _, counts, _ = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="Other", title="T", label="Astrophonica")
    _score(target, profiles_lower, {"astrophonica"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 2.0


def test_label_signal_caps_at_three_artists():
    profiles_lower = {f"a{i}": ArtistProfile(name=f"A{i}") for i in range(5)}
    candidates = [_candidate(artist=f"A{i}", label="Big Label") for i in range(5)]
    _, counts, _ = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="X", title="T", label="Big Label")
    _score(target, profiles_lower, {"big label"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 3.0


# --- label_artist_names tests ---

def test_label_artist_names_two_artists_sorted():
    profiles_lower = {
        "calibre": ArtistProfile(name="Calibre"),
        "amit": ArtistProfile(name="Amit"),
    }
    candidates = [
        _candidate(artist="Calibre", label="Signature"),
        _candidate(artist="Amit", label="Signature"),
    ]
    _, _, names = _build_relevant_labels(candidates, profiles_lower)
    assert names["signature"] == ["Amit", "Calibre"]


def test_label_artist_names_four_artists_no_cap():
    profiles_lower = {f"artist{i}": ArtistProfile(name=f"Artist{i}") for i in range(4)}
    candidates = [_candidate(artist=f"Artist{i}", label="Big Label") for i in range(4)]
    _, _, names = _build_relevant_labels(candidates, profiles_lower)
    assert len(names["big label"]) == 4


def test_label_artist_names_unknown_artist_absent():
    profiles_lower = {"known": ArtistProfile(name="Known")}
    candidates = [
        _candidate(artist="Known", label="Good Label"),
        _candidate(artist="Unknown", label="Good Label"),
    ]
    _, _, names = _build_relevant_labels(candidates, profiles_lower)
    assert names["good label"] == ["Known"]


def test_cross_source_two_sources_scores_1_point_0():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 1.0


def test_cross_source_three_sources_scores_1_point_5():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b", "c"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 1.5


def test_cross_source_caps_at_four():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b", "c", "d", "e"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 2.0


def test_cross_source_one_source_no_bonus():
    c = _candidate(raw_metadata={"seen_on_sources": ["a"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.0


def test_recency_penalty_applied_when_matched_artist_in_recent_set():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), recent_artists={"sully"})
    # known_artist: 1 * 3.0 = 3.0; penalty -0.75 → 2.25
    assert c.score == 2.25


def test_recency_penalty_skipped_when_artist_not_recent():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), recent_artists=set())
    assert c.score == 3.0


def test_recency_penalty_skipped_when_no_known_artist_match():
    c = _candidate(artist="Unknown")
    _score(c, {}, set(), {}, _build_genre_set({}), recent_artists={"some-other-artist"})
    assert c.score == 0.0


from datetime import datetime, timedelta, timezone


def test_pool_age_penalty_zero_weeks_no_subtraction():
    c = _candidate(pool_added_at=datetime.now(timezone.utc).isoformat())
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.0


def test_pool_age_penalty_three_weeks():
    added = (datetime.now(timezone.utc) - timedelta(weeks=3)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == -0.75


def test_pool_age_penalty_caps_at_negative_1_point_5():
    added = (datetime.now(timezone.utc) - timedelta(weeks=20)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == -1.5


def test_pool_age_penalty_clamped_for_future_timestamp():
    added = (datetime.now(timezone.utc) + timedelta(weeks=5)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.0


def test_pool_age_penalty_handles_bad_iso_string():
    c = _candidate(pool_added_at="not-a-date")
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.0


# --- Commit 1: electronic excluded from scoring ---

def test_electronic_only_no_genre_score():
    c = _candidate(genre_tags=["electronic"])
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "genre_match" for s in c.signals)
    assert c.score == 0.0


def test_electronic_with_house_scores_house_only():
    c = _candidate(genre_tags=["house", "electronic"])
    _score(c, {}, set(), {}, _build_genre_set({}))
    genre_sigs = [s for s in c.signals if s.code == "genre_match"]
    assert len(genre_sigs) == 1
    assert "house" in genre_sigs[0].explanation
    assert "electronic" not in genre_sigs[0].explanation
    assert c.score == 0.5
