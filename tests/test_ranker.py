import pytest

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


# --- Commit 4: per-section score floor ---

from src.pipeline.ranker import _assign_sections, _assign_sections_mix_prep


class _MockSettings:
    pipeline_top_picks_count = 5
    pipeline_label_watch_count = 5
    pipeline_artist_watch_count = 5
    pipeline_wildcard_count = 3
    pipeline_mix_prep_top_picks_count = 5
    pipeline_mix_prep_deep_cuts_count = 5
    pipeline_section_min_score = 1.0


def _scored_candidate(score, artist="X", title="T", source="s", **kw):
    c = _candidate(artist=artist, title=title, source=source, **kw)
    c.score = score
    return c


def test_section_floor_skips_below_threshold():
    ranked = [_scored_candidate(0.5)]
    sections = _assign_sections(ranked, _MockSettings(), _build_genre_set({}))
    assert sections["top_picks"] == []
    assert sections["wildcards"] == []


def test_section_floor_zero_reproduces_old_behaviour():
    class _NoFloor(_MockSettings):
        pipeline_section_min_score = 0.0

    ranked = [_scored_candidate(0.5)]
    sections = _assign_sections(ranked, _NoFloor(), _build_genre_set({}))
    assert len(sections["top_picks"]) == 1


def test_section_floor_mix_prep_skips_below_threshold():
    ranked = [_scored_candidate(0.5)]
    sections = _assign_sections_mix_prep(ranked, _MockSettings())
    assert sections["top_picks"] == []
    assert sections["deep_cuts"] == []


# --- Commit 3: fresh_release threshold 7 days ---

def test_fresh_release_10_days_no_signal():
    rel = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    c = _candidate(release_date=rel)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "fresh_release" for s in c.signals)
    assert c.score == 0.0


def test_fresh_release_5_days_signal_and_score():
    rel = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    c = _candidate(release_date=rel)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert any(s.code == "fresh_release" for s in c.signals)
    assert c.score == 0.5


# --- Commit 2: genre_match capped at 2 tags ---

def test_genre_match_three_tags_capped_at_two():
    # 3 matching non-exempt tags → 2 * 0.5 = 1.0, not 1.5
    c = _candidate(genre_tags=["house", "techno", "dnb"])
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 1.0


def test_genre_match_one_tag_unchanged():
    c = _candidate(genre_tags=["house"])
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.5


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


# ---------------------------------------------------------------------------
# Commit 5: _assign_sections trace hook
# ---------------------------------------------------------------------------

def test_trace_none_sections_identical_to_no_trace():
    ranked = [_scored_candidate(2.0, artist="A", title=f"T{i}") for i in range(10)]
    settings = _MockSettings()
    genres = _build_genre_set({})

    without_trace = _assign_sections(ranked, settings, genres, trace=None)
    with_trace = _assign_sections(ranked, settings, genres, trace={})

    for key in without_trace:
        assert [c.artist for c in without_trace[key]] == [c.artist for c in with_trace[key]]


def test_trace_records_below_floor():
    low = _scored_candidate(0.1, artist="Low", title="Floor")
    ranked = [low]
    trace: dict = {}
    _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)
    reasons = [r for _, r in trace.get(id(low), [])]
    assert any("below floor" in r for r in reasons)


def test_trace_records_lacks_signal():
    # Fill top_picks (cap=5) with distinct-artist candidates so the target is
    # not selected there, then it will be examined (and skipped) in label_watch.
    fillers = [_scored_candidate(10.0 - i, artist=f"F{i}", title=f"Filler{i}") for i in range(5)]
    c = _scored_candidate(3.0, artist="NoLabel", title="T")
    # No label_match signal → skipped from label_watch
    c.signals = []
    ranked = fillers + [c]
    trace: dict = {}
    _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)
    reasons_by_section = trace.get(id(c), [])
    label_reasons = [r for sname, r in reasons_by_section if sname == "label_watch"]
    assert any("lacks label_match signal" in r for r in label_reasons)


def test_trace_records_artist_cap():
    settings = _MockSettings()
    # 3 tracks by same artist — artist cap is 2 per section
    ranked = [_scored_candidate(5.0 - i, artist="Sully", title=f"T{i}") for i in range(3)]
    trace: dict = {}
    _assign_sections(ranked, settings, _build_genre_set({}), trace=trace)
    third = ranked[2]
    reasons = [r for sname, r in trace.get(id(third), []) if sname == "top_picks"]
    assert any("artist cap" in r for r in reasons)


def test_trace_records_genre_cap():
    class _SmallCap(_MockSettings):
        pipeline_top_picks_count = 10
        pipeline_label_watch_count = 10
        pipeline_artist_watch_count = 10
        pipeline_wildcard_count = 10

    # 4 tracks tagged 'house' — genre cap is 3 globally
    ranked = [_scored_candidate(5.0 - i, artist=f"A{i}", title=f"T{i}", genre_tags=["house"]) for i in range(4)]
    trace: dict = {}
    _assign_sections(ranked, _SmallCap(), _build_genre_set({}), trace=trace)
    fourth = ranked[3]
    reasons = [r for _, r in trace.get(id(fourth), [])]
    assert any("genre cap" in r for r in reasons)


def test_trace_same_candidate_skipped_in_two_sections_for_different_reasons():
    # Fill top_picks so the target is not consumed there, then it's examined
    # in label_watch (lacks label_match) and artist_watch (lacks known_artist).
    fillers = [_scored_candidate(10.0 - i, artist=f"F{i}", title=f"Filler{i}") for i in range(5)]
    c = _scored_candidate(3.0, artist="Solo", title="T")
    c.signals = []  # no label_match, no known_artist
    ranked = fillers + [c]
    trace: dict = {}
    _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)
    entries = trace.get(id(c), [])
    sections_seen = {sname for sname, _ in entries}
    # Should appear in label_watch and artist_watch (lacks signal)
    assert "label_watch" in sections_seen
    assert "artist_watch" in sections_seen


# --- ScoringWeights configuration tests ---

from src.pipeline.ranker import ScoringWeights


def test_scoring_weights_defaults_match_legacy_values():
    """Verify that ScoringWeights defaults equal the old hardcoded values."""
    w = ScoringWeights()
    assert w.w_known_artist == 3.0
    assert w.w_recurring == 2.0
    assert w.w_label_base == 1.5
    assert w.w_label_per_artist == 0.5
    assert w.label_artist_cap == 3
    assert w.w_cross_source_per == 0.5
    assert w.cross_source_cap == 4
    assert w.w_recency_penalty == 0.75
    assert w.recency_weeks == 4
    assert w.w_pool_age_per_week == 0.25
    assert w.pool_age_penalty_max == 1.5
    assert w.w_genre == 0.5
    assert w.genre_match_cap == 2
    assert w.w_fresh == 0.5
    assert w.fresh_days == 7
    assert w.w_chart_top == 1.5
    assert w.w_bandcamp == 1.0
    assert w.max_artist_score == 10.0
    assert w.recurring_threshold == 3


def test_scoring_weights_custom_w_known_artist_changes_score():
    """Verify that overriding w_known_artist changes candidate score."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=2)}
    c = _candidate(artist="Sully")

    # Default weight
    default_weights = ScoringWeights()
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), weights=default_weights)
    default_score = c.score
    assert default_score == 2 * 3.0  # 2 * 3.0 = 6.0

    # Overridden weight
    c2 = _candidate(artist="Sully")
    custom_weights = ScoringWeights(w_known_artist=5.0)
    _score(c2, profiles_lower, set(), {}, _build_genre_set({}), weights=custom_weights)
    custom_score = c2.score
    assert custom_score == 2 * 5.0  # 2 * 5.0 = 10.0
    assert custom_score != default_score


def test_scoring_weights_custom_w_label_base_changes_score():
    """Verify that overriding w_label_base changes label bonus."""
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    _, counts, _ = _build_relevant_labels(candidates, profiles_lower)

    # Default weight
    c1 = _candidate(artist="Other", label="Astrophonica")
    default_weights = ScoringWeights()
    _score(c1, profiles_lower, {"astrophonica"}, counts, _build_genre_set({}), weights=default_weights)
    default_score = c1.score
    assert default_score == 2.0  # 1.5 + 0.5 * 1 = 2.0

    # Overridden weight
    c2 = _candidate(artist="Other", label="Astrophonica")
    custom_weights = ScoringWeights(w_label_base=3.0)
    _score(c2, profiles_lower, {"astrophonica"}, counts, _build_genre_set({}), weights=custom_weights)
    custom_score = c2.score
    assert custom_score == 3.5  # 3.0 + 0.5 * 1 = 3.5
    assert custom_score != default_score


def test_scoring_weights_is_frozen():
    """Verify that ScoringWeights is immutable (frozen dataclass)."""
    w = ScoringWeights()
    with pytest.raises(Exception):  # dataclass frozen raises
        w.w_known_artist = 5.0
