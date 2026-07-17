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


def test_mixupload_popularity_never_fires_for_soundcloud():
    """download_count is no longer Mixupload-only (SoundCloud carries it since
    v0.14.0) — the Mixupload signal must be source-gated."""
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any("Mixupload" in s.explanation for s in c.signals)


def test_mixupload_popularity_still_fires_for_mixupload():
    c = _candidate(source="mixupload", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert any(s.code == "source_popularity" and "Mixupload" in s.explanation for s in c.signals)


def test_soundcloud_popularity_fires_at_threshold():
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 50})
    base = _candidate(source="soundcloud", raw_metadata={})
    gs = _build_genre_set({})
    _score(c, {}, set(), {}, gs)
    _score(base, {}, set(), {}, gs)
    assert any(s.code == "source_popularity" and "SoundCloud" in s.explanation for s in c.signals)
    assert c.score == pytest.approx(base.score + 0.25)
    assert c.discovery_score == pytest.approx(base.discovery_score + 0.25)


def test_soundcloud_popularity_below_threshold_silent():
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 49})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "source_popularity" for s in c.signals)


def test_soundcloud_popularity_not_for_other_sources():
    c = _candidate(source="bandcamp", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any("SoundCloud" in s.explanation for s in c.signals)


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
    pipeline_free_download_sources = []
    pipeline_free_downloads_count = 5
    pipeline_mix_prep_free_downloads_count = 10
    pipeline_free_downloads_min_score = 0.0

    @staticmethod
    def scoring_weights():
        return ScoringWeights()

    @staticmethod
    def artist_aliases():
        return {}


def _scored_candidate(score, artist="X", title="T", source="s", **kw):
    c = _candidate(artist=artist, title=title, source=source, **kw)
    c.score = score
    return c


class _LaneSettings(_MockSettings):
    pipeline_free_download_sources = ["soundcloud"]


def test_free_download_lane_is_exclusive():
    """A lane candidate never enters store sections (even with a huge score);
    a store candidate never enters free_downloads."""
    sc = _scored_candidate(99.0, artist="Lane Artist", source="soundcloud")
    store = _scored_candidate(50.0, artist="Store Artist", source="beatport")
    sections = _assign_sections([sc, store], _LaneSettings(), _build_genre_set({}))
    all_store = [c for k in ("top_picks", "label_watch", "artist_watch", "wildcards")
                 for c in sections[k]]
    assert sc not in all_store
    assert sc in sections["free_downloads"]
    assert store not in sections["free_downloads"]


def test_free_download_lane_floor_and_cap():
    lane = [_scored_candidate(0.5, artist=f"DJ {i}", title=f"Boot {i}", source="soundcloud")
            for i in range(8)]
    sections = _assign_sections(lane, _LaneSettings(), _build_genre_set({}))
    assert len(sections["free_downloads"]) == 5  # cap 5; lane floor 0 admits 0.5-scorers


def test_free_download_lane_own_floor():
    class _FlooredLane(_LaneSettings):
        pipeline_free_downloads_min_score = 0.5

    c = _scored_candidate(0.4, source="soundcloud")
    sections = _assign_sections([c], _FlooredLane(), _build_genre_set({}))
    assert sections["free_downloads"] == []


def test_free_download_lane_count_zero_disables():
    class _ZeroLane(_LaneSettings):
        pipeline_free_downloads_count = 0

    c = _scored_candidate(5.0, source="soundcloud")
    sections = _assign_sections([c], _ZeroLane(), _build_genre_set({}))
    assert sections["free_downloads"] == []


def test_lane_disabled_when_no_sources_configured():
    sc = _scored_candidate(5.0, source="soundcloud")
    sections = _assign_sections([sc], _MockSettings(), _build_genre_set({}))
    assert sections["free_downloads"] == []
    # with no lane configured the candidate competes normally
    assert sc in sections["top_picks"]


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


def test_scoring_weights_wildcards_defaults():
    w = ScoringWeights()
    assert w.wildcards_axis == "discovery"
    assert w.wildcards_max_familiarity == 0.0


# ---------------------------------------------------------------------------
# Two-axis scoring (P2): familiarity vs discovery
# ---------------------------------------------------------------------------

def test_axis_accumulation_familiarity_and_discovery_sum_to_total():
    """Known-artist + label + chart candidate: familiarity carries the artist
    signals, discovery carries label/chart, and the total is unchanged."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=4)}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    _, counts, _ = _build_relevant_labels(candidates, profiles_lower)

    c = _candidate(artist="Sully", label="Astrophonica", raw_metadata={"chart_position": 10})
    _score(c, profiles_lower, {"astrophonica"}, counts, _build_genre_set(profiles_lower))

    # familiarity: known_artist (4 * 3.0 = 12.0, capped at 10.0) + recurring (+2.0)
    assert c.familiarity_score == 12.0
    # discovery: label_match (1.5 + 0.5*1 = 2.0) + chart_position (1.5 * (1 - 9/100))
    assert c.discovery_score > 0
    assert any(s.code == "label_match" for s in c.signals)
    assert any(s.code == "chart_position" for s in c.signals)
    # total stays the sum of the two axes (rounding tolerance from independent
    # per-axis rounding vs. the single unrounded running total)
    assert c.score == pytest.approx(c.familiarity_score + c.discovery_score, abs=0.01)


def test_axis_accumulation_unknown_artist_has_zero_familiarity():
    c = _candidate(artist="Unknown", genre_tags=["house"], raw_metadata={"chart_position": 1})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.familiarity_score == 0.0
    assert c.discovery_score > 0
    assert c.score == c.discovery_score


def test_axis_accumulation_pool_age_hits_total_only():
    """pool_age is a queueing artefact, not familiarity or discovery merit —
    it is subtracted from the combined total only; both axes stay gross."""
    added = (datetime.now(timezone.utc) - timedelta(weeks=3)).isoformat()
    c = _candidate(artist="Unknown", genre_tags=["house"], pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert any(s.code == "pool_age" for s in c.signals)
    assert c.discovery_score == 0.5       # genre_match, ungrossed by pool_age
    assert c.familiarity_score == 0.0
    assert c.score == -0.25               # 0.5 (genre) - 0.75 (3 weeks pool age)


# --- Wildcards axis selection ---

def _scored_with_axes(score, familiarity, discovery, artist="X", title="T", **kw):
    c = _scored_candidate(score, artist=artist, title=title, **kw)
    c.familiarity_score = familiarity
    c.discovery_score = discovery
    return c


def test_wildcards_discovery_mode_orders_by_discovery_and_excludes_familiar():
    # Fill top_picks (cap=5) with distinct-artist, pure-familiarity fillers so
    # the interesting candidates below survive to be examined for wildcards.
    fillers = [
        _scored_with_axes(20.0, familiarity=20.0, discovery=0.0, artist=f"F{i}", title=f"Filler{i}")
        for i in range(5)
    ]
    # Known artist track: decent discovery (clears the floor) but familiar —
    # must NOT land in wildcards under the discovery axis.
    known_extra = _scored_with_axes(5.0, familiarity=5.0, discovery=1.5, artist="KnownExtra", title="TKE")
    # Two genuine discovery candidates: unknown artists, no familiarity at all.
    disco_a = _scored_with_axes(2.0, familiarity=0.0, discovery=2.0, artist="A", title="TA")
    disco_b = _scored_with_axes(3.0, familiarity=0.0, discovery=3.0, artist="B", title="TB")

    ranked = fillers + [known_extra, disco_b, disco_a]
    trace: dict = {}
    sections = _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)

    wildcards = sections["wildcards"]
    assert [c.artist for c in wildcards] == ["B", "A"]
    assert all(c.familiarity_score <= 0.0 for c in wildcards)
    assert known_extra not in wildcards
    reasons = [r for sname, r in trace.get(id(known_extra), []) if sname == "wildcards"]
    assert any("familiarity too high for wildcards" in r for r in reasons)


def test_wildcards_floor_applies_to_discovery_axis_not_total():
    """A candidate can have a high total score yet a discovery score below the
    floor — in discovery mode it must be excluded on discovery grounds, not
    slip in on its (familiarity-heavy) total."""
    fillers = [
        _scored_with_axes(20.0 - i, familiarity=20.0, discovery=0.0, artist=f"F{i}", title=f"Filler{i}")
        for i in range(5)
    ]
    target = _scored_with_axes(12.0, familiarity=12.0, discovery=0.5, artist="X", title="T")

    ranked = fillers + [target]
    trace: dict = {}
    sections = _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)

    assert target not in sections["wildcards"]
    reasons = [r for sname, r in trace.get(id(target), []) if sname == "wildcards"]
    assert any("below floor 1.0 (discovery axis)" in r for r in reasons)


def test_wildcards_combined_axis_reproduces_pre_p2_behaviour():
    """wildcards_axis='combined' ranks Wildcards by total score, exactly the
    pre-two-axis behaviour — the two axes must produce different Wildcards
    for the same input to prove this isn't a no-op."""
    class _CombinedSettings(_MockSettings):
        @staticmethod
        def scoring_weights():
            return ScoringWeights(wildcards_axis="combined")

    fillers = [
        _scored_with_axes(20.0, familiarity=20.0, discovery=0.0, artist=f"F{i}", title=f"Filler{i}")
        for i in range(5)
    ]
    known_extra = _scored_with_axes(5.0, familiarity=5.0, discovery=1.5, artist="KnownExtra", title="TKE")
    disco_a = _scored_with_axes(2.0, familiarity=0.0, discovery=2.0, artist="A", title="TA")
    disco_b = _scored_with_axes(3.0, familiarity=0.0, discovery=3.0, artist="B", title="TB")

    ranked = fillers + [known_extra, disco_b, disco_a]

    discovery_sections = _assign_sections(ranked, _MockSettings(), _build_genre_set({}))
    combined_sections = _assign_sections(ranked, _CombinedSettings(), _build_genre_set({}))

    # Discovery axis (default): known_extra excluded, ordered by discovery_score.
    assert [c.artist for c in discovery_sections["wildcards"]] == ["B", "A"]
    # Combined axis: reproduces old total-score-ordered remainder, including
    # the familiar candidate — proof the two modes genuinely diverge.
    assert [c.artist for c in combined_sections["wildcards"]] == ["KnownExtra", "B", "A"]


# ---------------------------------------------------------------------------
# Genre affinity scaling (P3): genre_match scaled by corpus-level genre share
# ---------------------------------------------------------------------------

def test_genre_affinity_none_reproduces_old_flat_score():
    """genre_affinity=None (the default) must reproduce today's behaviour exactly."""
    c = _candidate(genre_tags=["house", "techno", "dnb"])
    _score(c, {}, set(), {}, _build_genre_set({}), genre_affinity=None)
    assert c.score == 1.0  # 2 tags capped * 0.5, multiplier 1.0 — same as pre-P3


def test_genre_affinity_empty_dict_reproduces_old_flat_score():
    """An empty affinity map (e.g. no genre data yet) is treated the same as None."""
    c = _candidate(genre_tags=["house"])
    _score(c, {}, set(), {}, _build_genre_set({}), genre_affinity={})
    assert c.score == 0.5


def test_genre_affinity_dominant_genre_scales_to_max():
    """The single genre in the affinity map is trivially the dominant one —
    its multiplier hits genre_affinity_max (2.0 by default)."""
    c = _candidate(genre_tags=["dnb"])
    _score(c, {}, set(), {}, _build_genre_set({}), genre_affinity={"dnb": 1.0})
    assert c.score == pytest.approx(0.5 * 2.0)  # w_genre * genre_affinity_max


def test_genre_affinity_tag_absent_from_map_gets_floor_multiplier():
    """A genre you don't play at all (absent from the affinity map, unlike a
    merely fringe one) scores at genre_affinity_min — the floor."""
    c = _candidate(genre_tags=["house"])
    _score(c, {}, set(), {}, _build_genre_set({}), genre_affinity={"dnb": 1.0})
    assert c.score == pytest.approx(0.5 * 0.5)  # w_genre * genre_affinity_min


def test_genre_affinity_custom_min_clamps_low_share_tag():
    """A present-but-fringe tag whose raw multiplier would fall under a custom
    (raised) floor gets clamped to genre_affinity_min."""
    weights = ScoringWeights(genre_affinity_min=0.8, genre_affinity_max=2.0)
    c = _candidate(genre_tags=["house"])
    _score(
        c, {}, set(), {}, _build_genre_set({}),
        weights=weights, genre_affinity={"dnb": 0.9, "house": 0.1},
    )
    # raw = 0.5 + 1.5 * (0.1/0.9) = 0.6667 → clamped up to 0.8
    assert c.score == pytest.approx(0.5 * 0.8)


def test_genre_affinity_custom_max_clamps_dominant_tag():
    weights = ScoringWeights(genre_affinity_max=1.5)
    c = _candidate(genre_tags=["dnb"])
    _score(c, {}, set(), {}, _build_genre_set({}), weights=weights, genre_affinity={"dnb": 1.0})
    # raw = 2.0 → clamped down to 1.5
    assert c.score == pytest.approx(0.5 * 1.5)


def test_genre_affinity_cap_counts_highest_multiplier_tags_not_first_n():
    """When more tags match than genre_match_cap, the highest-affinity tags
    are counted, regardless of their position in genre_tags."""
    affinity = {"dnb": 0.6, "house": 0.3, "techno": 0.1}
    # Deliberately ordered worst-affinity-first to prove selection isn't by position.
    c = _candidate(genre_tags=["techno", "house", "dnb"])
    _score(c, {}, set(), {}, _build_genre_set({}), genre_affinity=affinity)
    # dnb: share/max=1.0 → raw=2.0 (clamped at max)
    # house: share/max=0.5 → raw=1.25
    # techno: share/max=1/6 → raw=0.75 — excluded by the cap (2 tags)
    expected = 0.5 * 2.0 + 0.5 * 1.25
    assert c.score == pytest.approx(expected, abs=0.01)
    # Reason text still lists all matching tags (display only, unaffected by the cap)
    genre_sig = next(s for s in c.signals if s.code == "genre_match")
    assert "techno" in genre_sig.explanation
    assert "house" in genre_sig.explanation
    assert "dnb" in genre_sig.explanation


# ---------------------------------------------------------------------------
# rank_candidates / rank_candidates_mix_prep — genre_affinity threading
# ---------------------------------------------------------------------------

def test_rank_candidates_threads_genre_affinity_through_to_score(tmp_path):
    from src.pipeline.ranker import rank_candidates

    class _RankSettings(_MockSettings):
        data_dir = str(tmp_path)

        @staticmethod
        def scoring_weights():
            return ScoringWeights()

    candidates_with = [_candidate(artist="X", title="T1", genre_tags=["dnb"])]
    candidates_without = [_candidate(artist="X", title="T1", genre_tags=["dnb"])]

    rank_candidates(candidates_with, {}, _RankSettings(), genre_affinity={"dnb": 1.0})
    rank_candidates(candidates_without, {}, _RankSettings(), genre_affinity=None)

    # Same candidate, dominant-genre affinity present vs. absent — score must differ,
    # proving genre_affinity actually reaches _score through the public entry point.
    assert candidates_with[0].score > candidates_without[0].score
    assert candidates_with[0].score == pytest.approx(1.0)   # 0.5 * genre_affinity_max
    assert candidates_without[0].score == pytest.approx(0.5)  # 0.5 * 1.0 (flat)


def test_rank_candidates_mix_prep_threads_genre_affinity_through_to_score(tmp_path):
    from src.pipeline.ranker import rank_candidates_mix_prep

    class _RankSettings(_MockSettings):
        data_dir = str(tmp_path)

        @staticmethod
        def scoring_weights():
            return ScoringWeights()

    candidates_with = [_candidate(artist="X", title="T1", genre_tags=["dnb"])]
    candidates_without = [_candidate(artist="X", title="T1", genre_tags=["dnb"])]

    rank_candidates_mix_prep(candidates_with, {}, _RankSettings(), genre_affinity={"dnb": 1.0})
    rank_candidates_mix_prep(candidates_without, {}, _RankSettings(), genre_affinity=None)


# ---------------------------------------------------------------------------
# Alias resolution + short-name match guard (issue #4)
# ---------------------------------------------------------------------------

def test_alias_resolution_in_score_scores_as_canonical():
    """A release credited to an alias resolves to the canonical profile and
    scores/known_artist-signals exactly as if the canonical name matched."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=2)}
    aliases = {"dave skinner": "calibre"}
    c = _candidate(artist="Dave Skinner")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), aliases=aliases)
    assert c.score == 6.0  # 2 * 3.0, same as a direct "Calibre" match
    signal = next(s for s in c.signals if s.code == "known_artist")
    assert "Calibre" in signal.explanation
    assert "Dave Skinner" not in signal.explanation


def test_alias_resolution_no_aliases_given_scores_zero():
    """Sanity check: without the alias map, the same release is invisible —
    proves the alias resolution (not some other match) drove the score above."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=2)}
    c = _candidate(artist="Dave Skinner")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower))
    assert c.score == 0.0


def test_alias_in_label_relevance():
    """_build_relevant_labels resolves aliases too, so a label release credited
    to an alias still marks the label relevant and credits the canonical artist."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre")}
    aliases = {"dave skinner": "calibre"}
    candidates = [_candidate(artist="Dave Skinner", label="Signature")]
    relevant, counts, names = _build_relevant_labels(candidates, profiles_lower, aliases)
    assert "signature" in relevant
    assert counts["signature"] == 1
    assert names["signature"] == ["Calibre"]


def test_alias_in_label_relevance_absent_without_aliases():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre")}
    candidates = [_candidate(artist="Dave Skinner", label="Signature")]
    relevant, counts, names = _build_relevant_labels(candidates, profiles_lower)
    assert "signature" not in relevant


# --- Short-name match guard ---

def test_short_name_guard_blocks_uncorroborated_match(caplog):
    """A 2-character matched artist name with no label or genre corroboration
    on the candidate must not fire known_artist — the worst trust-error class
    (false 'You play X' claim from a short-name string collision)."""
    import logging
    caplog.set_level(logging.INFO, logger="src.pipeline.ranker")
    profiles_lower = {"jm": ArtistProfile(name="JM", play_count=5)}
    c = _candidate(artist="JM")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower))
    assert not any(s.code == "known_artist" for s in c.signals)
    assert c.score == 0.0
    assert "short-name match 'JM' skipped (uncorroborated)" in caplog.text


def test_short_name_guard_admits_corroborated_by_label():
    """The same short match is admitted when the candidate's label is a
    relevant label — independent corroboration."""
    profiles_lower = {"jm": ArtistProfile(name="JM", play_count=5)}
    c = _candidate(artist="JM", label="Big Dada")
    _score(c, profiles_lower, {"big dada"}, {}, _build_genre_set(profiles_lower))
    assert any(s.code == "known_artist" for s in c.signals)
    # artist (5*3.0 capped at 10.0) + recurring (+2.0) + label_match (1.5 + 0.5*1)
    assert c.score == 14.0


def test_short_name_guard_admits_corroborated_by_genre():
    """The same short match is admitted when the candidate carries a
    non-exempt genre tag in the taste genre set — independent corroboration."""
    profiles_lower = {"jm": ArtistProfile(name="JM", play_count=5)}
    c = _candidate(artist="JM", genre_tags=["dnb"])
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower))
    assert any(s.code == "known_artist" for s in c.signals)


def test_short_name_guard_electronic_tag_alone_does_not_corroborate():
    """`electronic` is scoring-exempt (too broad to be evidence of fit) — it
    must not count as corroboration for the short-name guard either."""
    profiles_lower = {"jm": ArtistProfile(name="JM", play_count=5)}
    c = _candidate(artist="JM", genre_tags=["electronic"])
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower))
    assert not any(s.code == "known_artist" for s in c.signals)


def test_short_name_guard_respects_config_override():
    """Lowering min_artist_match_len admits a match the default would guard."""
    profiles_lower = {"jm": ArtistProfile(name="JM", play_count=5)}
    weights = ScoringWeights(min_artist_match_len=2)
    c = _candidate(artist="JM")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), weights=weights)
    assert any(s.code == "known_artist" for s in c.signals)


def test_short_name_guard_raising_threshold_blocks_longer_names_too():
    profiles_lower = {"amit": ArtistProfile(name="Amit", play_count=5)}
    weights = ScoringWeights(min_artist_match_len=5)
    c = _candidate(artist="Amit")  # 4 chars — clears default (4) but not a raised 5
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), weights=weights)
    assert not any(s.code == "known_artist" for s in c.signals)


def test_short_name_guard_uses_written_alias_length_not_canonical_length():
    """Alias-resolved matches are guarded on the length of the WRITTEN name
    part, not the (possibly much longer) canonical profile name."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    aliases = {"cb": "calibre"}  # 2-char alias for a 7-char canonical name
    c = _candidate(artist="CB")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), aliases=aliases)
    assert not any(s.code == "known_artist" for s in c.signals)
    assert c.score == 0.0


def test_long_name_match_unaffected_by_guard():
    """Matches at/above the default threshold are unaffected regardless of
    corroboration — this guards regressions against everyday-length names."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower))
    assert any(s.code == "known_artist" for s in c.signals)
    assert c.score == 3.0


# ---------------------------------------------------------------------------
# Scene one-hop signal (issue #6): _build_scene_data
# ---------------------------------------------------------------------------

from src.pipeline.ranker import _build_scene_data


def test_build_scene_data_anchor_picks_highest_play_count():
    profiles_lower = {
        "amit": ArtistProfile(name="Amit", play_count=2),
        "calibre": ArtistProfile(name="Calibre", play_count=5),
    }
    label_artist_names = {"signature": ["Amit", "Calibre"]}
    anchor_by_label, _ = _build_scene_data([], label_artist_names, profiles_lower, None)
    assert anchor_by_label["signature"] == "Calibre"


def test_build_scene_data_anchor_ties_break_alphabetically():
    """label_artist_names values are always pre-sorted — equal play_count ties
    resolve to whichever name is encountered first, i.e. alphabetically."""
    profiles_lower = {
        "amit": ArtistProfile(name="Amit", play_count=5),
        "calibre": ArtistProfile(name="Calibre", play_count=5),
    }
    label_artist_names = {"signature": ["Amit", "Calibre"]}
    anchor_by_label, _ = _build_scene_data([], label_artist_names, profiles_lower, None)
    assert anchor_by_label["signature"] == "Amit"


def test_build_scene_data_unresolvable_name_gives_no_anchor():
    """A label whose every associated name fails to resolve to a live profile
    (e.g. stale memory data with no matching profile) gets no anchor entry —
    the scene_adjacent signal cannot fire for it."""
    label_artist_names = {"signature": ["Ghost"]}
    anchor_by_label, _ = _build_scene_data([], label_artist_names, {}, None)
    assert "signature" not in anchor_by_label


def test_build_scene_data_anchor_resolves_via_alias():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    aliases = {"dave skinner": "calibre"}
    label_artist_names = {"signature": ["Dave Skinner"]}
    anchor_by_label, _ = _build_scene_data([], label_artist_names, profiles_lower, aliases)
    assert anchor_by_label["signature"] == "Calibre"


def test_build_scene_data_roster_counts_distinct_normalised_artists():
    candidates = [
        _candidate(artist="Sully", label="Astrophonica"),
        _candidate(artist="sully", label="Astrophonica"),   # same artist, different case — collapses
        _candidate(artist="Skee Mask", label="Astrophonica"),
        _candidate(artist="Someone Else", label="Other Label"),
    ]
    _, roster_by_label = _build_scene_data(candidates, {}, {}, None)
    assert roster_by_label["astrophonica"] == 2
    assert roster_by_label["other label"] == 1


def test_build_scene_data_ignores_candidates_without_label():
    candidates = [_candidate(artist="X", label=None)]
    _, roster_by_label = _build_scene_data(candidates, {}, {}, None)
    assert roster_by_label == {}


# ---------------------------------------------------------------------------
# Scene one-hop signal (issue #6): _score
# ---------------------------------------------------------------------------

def test_scene_adjacent_fires_for_unknown_artist_on_relevant_label():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"signature": "Calibre"}, {"signature": 2})
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(
        c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    scene_sig = next(s for s in c.signals if s.code == "scene_adjacent")
    assert scene_sig.explanation == "Label-mate of Calibre on Signature."
    # discovery axis carries it: label_match (1.5 + 0.5*1 = 2.0) + scene_adjacent (0.75)
    assert c.discovery_score == pytest.approx(2.75)
    assert c.score == pytest.approx(2.75)


def test_scene_adjacent_does_not_fire_when_known_artist_matched():
    """A candidate whose own artist we already play doesn't need a label-mate
    nudge — known_artist scoring already covers it."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"signature": "Calibre"}, {"signature": 2})
    c = _candidate(artist="Calibre", label="Signature")
    _score(
        c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_skipped_when_roster_exceeds_cap():
    """A mega-label (more distinct artists this week than the roster cap) is
    not evidence of a scene — the signal must not fire."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"big label": "Calibre"}, {"big label": 31})
    c = _candidate(artist="Unknown Artist", label="Big Label")
    _score(
        c, profiles_lower, {"big label"}, {"big label": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_fires_at_roster_cap_boundary():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"big label": "Calibre"}, {"big label": 30})
    c = _candidate(artist="Unknown Artist", label="Big Label")
    _score(
        c, profiles_lower, {"big label"}, {"big label": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    assert any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_disabled_when_scene_data_none():
    """Backwards compatibility: every direct _score call elsewhere in this
    suite omits scene_data, and must keep scoring exactly as before."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower))
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_disabled_by_zero_weight():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"signature": "Calibre"}, {"signature": 1})
    weights = ScoringWeights(w_scene_adjacent=0.0)
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(
        c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower),
        weights=weights, scene_data=scene_data,
    )
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_stacks_with_label_match():
    """Deliberate: both fire off the same 'label is relevant' fact — they're
    two different modest signals, not a double-charge for one thing."""
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"signature": "Calibre"}, {"signature": 1})
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(
        c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    codes = {s.code for s in c.signals}
    assert {"label_match", "scene_adjacent"} <= codes


def test_scene_adjacent_does_not_fire_for_irrelevant_label():
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    scene_data = ({"signature": "Calibre"}, {"signature": 1})
    c = _candidate(artist="Unknown Artist", label="Other Label")
    _score(
        c, profiles_lower, {"signature"}, {}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_no_anchor_does_not_fire():
    """Label is relevant and roster is small, but no artist in scene_data's
    anchor map resolved to a live profile — no anchor, no signal."""
    scene_data = ({}, {"signature": 1})
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(c, {}, {"signature"}, {"signature": 1}, _build_genre_set({}), scene_data=scene_data)
    assert not any(s.code == "scene_adjacent" for s in c.signals)


def test_scene_adjacent_missing_roster_entry_defaults_permissive():
    """A label absent from roster_by_label (no roster evidence this week —
    e.g. only a pool-injected candidate carries it) defaults to roster size 0,
    which is <= any positive cap, so the signal can still fire."""
    scene_data = ({"signature": "Calibre"}, {})  # no roster entry at all
    profiles_lower = {"calibre": ArtistProfile(name="Calibre", play_count=5)}
    c = _candidate(artist="Unknown Artist", label="Signature")
    _score(
        c, profiles_lower, {"signature"}, {"signature": 1}, _build_genre_set(profiles_lower),
        scene_data=scene_data,
    )
    assert any(s.code == "scene_adjacent" for s in c.signals)


# ---------------------------------------------------------------------------
# Scene one-hop signal (issue #6): rank_candidates end-to-end
# ---------------------------------------------------------------------------

def test_rank_candidates_scene_adjacent_via_current_corpus(tmp_path):
    from src.pipeline.ranker import rank_candidates

    class _RankSettings(_MockSettings):
        data_dir = str(tmp_path)

        @staticmethod
        def scoring_weights():
            return ScoringWeights()

    profiles = {"Calibre": ArtistProfile(name="Calibre", play_count=5)}
    known = _candidate(artist="Calibre", title="Old One", label="Signature")
    unknown = _candidate(artist="New Kid", title="Debut", label="Signature")
    candidates = [known, unknown]

    rank_candidates(candidates, profiles, _RankSettings(), label_seed=candidates)

    scene_sig = next(s for s in unknown.signals if s.code == "scene_adjacent")
    assert scene_sig.explanation == "Label-mate of Calibre on Signature."
    assert not any(s.code == "scene_adjacent" for s in known.signals)


# ---------------------------------------------------------------------------
# BPM/key-aware mix-prep demotion mechanics (issue #8)
# ---------------------------------------------------------------------------

def test_assign_sections_mix_prep_no_demoted_keys_reproduces_old_order():
    high = _scored_candidate(5.0, artist="High", title="T1")
    low = _scored_candidate(1.0, artist="Low", title="T2")
    ranked = [high, low]
    sections = _assign_sections_mix_prep(ranked, _MockSettings())
    assert sections["top_picks"] == [high, low]


def test_assign_sections_mix_prep_demotes_below_all_matches():
    """A match with a LOWER score must still outrank a demoted (BPM/key-unknown)
    candidate with a HIGHER score — demotion overrides score ordering entirely."""
    demoted_high_score = _scored_candidate(9.0, artist="Unknown BPM", title="T1")
    match_low_score = _scored_candidate(1.0, artist="Known BPM", title="T2")
    ranked = sorted([demoted_high_score, match_low_score], key=lambda c: -c.score)
    demoted_keys = {demoted_high_score.key}

    sections = _assign_sections_mix_prep(ranked, _MockSettings(), demoted_keys=demoted_keys)

    picks = sections["top_picks"]
    assert picks == [match_low_score, demoted_high_score]


def test_assign_sections_mix_prep_demoted_keys_empty_set_no_effect():
    high = _scored_candidate(5.0, artist="High", title="T1")
    low = _scored_candidate(1.0, artist="Low", title="T2")
    ranked = [high, low]
    sections = _assign_sections_mix_prep(ranked, _MockSettings(), demoted_keys=set())
    assert sections["top_picks"] == [high, low]


def test_assign_sections_mix_prep_preserves_score_order_within_each_group():
    a = _scored_candidate(5.0, artist="A", title="T1")
    b = _scored_candidate(4.0, artist="B", title="T2")
    demoted_a = _scored_candidate(9.0, artist="C", title="T3")
    demoted_b = _scored_candidate(8.0, artist="D", title="T4")
    ranked = sorted([a, b, demoted_a, demoted_b], key=lambda c: -c.score)
    demoted_keys = {demoted_a.key, demoted_b.key}

    sections = _assign_sections_mix_prep(ranked, _MockSettings(), demoted_keys=demoted_keys)

    picks = sections["top_picks"]
    assert picks == [a, b, demoted_a, demoted_b]


def test_rank_candidates_mix_prep_threads_demoted_keys_through_to_sections(tmp_path):
    from src.pipeline.ranker import rank_candidates_mix_prep

    class _RankSettings(_MockSettings):
        data_dir = str(tmp_path)
        pipeline_section_min_score = 0.0

        @staticmethod
        def scoring_weights():
            return ScoringWeights()

    # known_artist scoring gives "Known" a real score edge; "Unknown" scores 0.
    # Demoting "Known" (as if its BPM/key were unspecified) must still push
    # it below "Unknown" in the final section order.
    profiles = {"Known": ArtistProfile(name="Known", play_count=3)}
    known = _candidate(artist="Known", title="T1")
    unknown = _candidate(artist="Unknown", title="T2")
    candidates = [known, unknown]

    sections, _ = rank_candidates_mix_prep(
        candidates, profiles, _RankSettings(), demoted_keys={known.key},
    )

    assert sections["top_picks"] == [unknown, known]


def test_rank_candidates_mix_prep_no_demoted_keys_zero_behaviour_change(tmp_path):
    from src.pipeline.ranker import rank_candidates_mix_prep

    class _RankSettings(_MockSettings):
        data_dir = str(tmp_path)
        pipeline_section_min_score = 0.0

        @staticmethod
        def scoring_weights():
            return ScoringWeights()

    profiles = {"Known": ArtistProfile(name="Known", play_count=3)}
    known = _candidate(artist="Known", title="T1")
    unknown = _candidate(artist="Unknown", title="T2")
    candidates = [known, unknown]

    sections, _ = rank_candidates_mix_prep(candidates, profiles, _RankSettings())

    # No demoted_keys passed — plain score order, known_artist candidate wins.
    assert sections["top_picks"] == [known, unknown]


# ---------------------------------------------------------------------------
# Issue #12: per-source share cap (weekly _assign_sections only)
# ---------------------------------------------------------------------------

def test_source_cap_skips_over_share_candidate_with_trace():
    class _CapSettings(_MockSettings):
        pipeline_top_picks_count = 3
        pipeline_label_watch_count = 0
        pipeline_artist_watch_count = 0
        pipeline_wildcard_count = 0

        @staticmethod
        def scoring_weights():
            return ScoringWeights(max_share_per_source=0.5)

    # total_configured_slots = 3, cap = ceil(0.5 * 3) = 2 per source
    ranked = [
        _scored_candidate(5.0, artist="A1", title="T1", source="beatport"),
        _scored_candidate(4.0, artist="A2", title="T2", source="beatport"),
        _scored_candidate(3.0, artist="A3", title="T3", source="beatport"),
        _scored_candidate(2.0, artist="A4", title="T4", source="bandcamp"),
    ]
    trace: dict = {}
    sections = _assign_sections(ranked, _CapSettings(), _build_genre_set({}), trace=trace)

    picked = [(c.artist, c.source) for c in sections["top_picks"]]
    assert picked == [("A1", "beatport"), ("A2", "beatport"), ("A4", "bandcamp")]

    third = ranked[2]
    reasons = [r for _, r in trace.get(id(third), [])]
    assert any("source cap (beatport)" in r for r in reasons)


def test_source_cap_one_point_zero_disables():
    class _NoCapSettings(_MockSettings):
        pipeline_top_picks_count = 3
        pipeline_label_watch_count = 0
        pipeline_artist_watch_count = 0
        pipeline_wildcard_count = 0

        @staticmethod
        def scoring_weights():
            return ScoringWeights(max_share_per_source=1.0)

    ranked = [
        _scored_candidate(5.0, artist="A1", title="T1", source="beatport"),
        _scored_candidate(4.0, artist="A2", title="T2", source="beatport"),
        _scored_candidate(3.0, artist="A3", title="T3", source="beatport"),
        _scored_candidate(2.0, artist="A4", title="T4", source="bandcamp"),
    ]
    trace: dict = {}
    sections = _assign_sections(ranked, _NoCapSettings(), _build_genre_set({}), trace=trace)

    # Cap disabled — all three beatport candidates fill top_picks
    picked = [(c.artist, c.source) for c in sections["top_picks"]]
    assert picked == [("A1", "beatport"), ("A2", "beatport"), ("A3", "beatport")]
    assert not any("source cap" in r for reasons in trace.values() for _, r in reasons)


def test_source_cap_counts_globally_across_sections():
    class _CapSettings(_MockSettings):
        pipeline_top_picks_count = 1
        pipeline_label_watch_count = 0
        pipeline_artist_watch_count = 0
        pipeline_wildcard_count = 1

        @staticmethod
        def scoring_weights():
            return ScoringWeights(max_share_per_source=0.5, wildcards_axis="combined")

    # total_configured_slots = 2, cap = ceil(1.0) = 1 per source: after
    # top_picks consumes the one allowed beatport slot, wildcards must skip
    # the remaining beatport candidate even though its own section is empty.
    ranked = [
        _scored_candidate(5.0, artist="A1", title="T1", source="beatport"),
        _scored_candidate(4.0, artist="A2", title="T2", source="beatport"),
    ]
    trace: dict = {}
    sections = _assign_sections(ranked, _CapSettings(), _build_genre_set({}), trace=trace)

    assert [c.artist for c in sections["top_picks"]] == ["A1"]
    assert sections["wildcards"] == []
    reasons = [r for _, r in trace.get(id(ranked[1]), [])]
    assert any("source cap (beatport)" in r for r in reasons)


def test_source_cap_default_does_not_bind_small_input():
    # Default 0.6 with the default _MockSettings slots (5+5+5+3=18) → cap 11;
    # a small input must be completely unaffected (snapshot-safety check).
    ranked = [_scored_candidate(5.0 - i, artist=f"A{i}", title=f"T{i}", source="s") for i in range(5)]
    trace: dict = {}
    sections = _assign_sections(ranked, _MockSettings(), _build_genre_set({}), trace=trace)
    assert len(sections["top_picks"]) == 5
    assert not any("source cap" in r for reasons in trace.values() for _, r in reasons)


# ---------------------------------------------------------------------------
# Issue #12: Mixupload popularity signal (source_popularity)
# ---------------------------------------------------------------------------

def test_source_popularity_fires_at_threshold():
    c = _candidate(source="mixupload", raw_metadata={"download_count": 100})
    _score(c, {}, set(), {}, _build_genre_set({}))
    sigs = [s for s in c.signals if s.code == "source_popularity"]
    assert len(sigs) == 1
    assert sigs[0].explanation == "100 downloads on Mixupload."
    assert c.score == 0.25
    assert c.discovery_score == 0.25
    assert c.familiarity_score == 0.0


def test_source_popularity_fires_above_threshold_flat():
    c = _candidate(source="mixupload", raw_metadata={"download_count": 5000})
    _score(c, {}, set(), {}, _build_genre_set({}))
    # Flat bonus — fires once regardless of how far above threshold
    assert c.score == 0.25
    assert any(s.code == "source_popularity" for s in c.signals)


def test_source_popularity_does_not_fire_below_threshold():
    c = _candidate(source="mixupload", raw_metadata={"download_count": 99})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "source_popularity" for s in c.signals)
    assert c.score == 0.0
    assert c.discovery_score == 0.0


def test_source_popularity_does_not_fire_when_absent():
    c = _candidate(source="mixupload", raw_metadata={})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "source_popularity" for s in c.signals)
    assert c.score == 0.0


def test_source_popularity_configurable_threshold_and_weight():
    weights = ScoringWeights(w_mixupload_popularity=0.5, mixupload_popularity_downloads=10)
    c = _candidate(source="mixupload", raw_metadata={"download_count": 10})
    _score(c, {}, set(), {}, _build_genre_set({}), weights=weights)
    assert c.score == 0.5
    assert c.discovery_score == 0.5


# ---------------------------------------------------------------------------
# Taste recency weighting (issue #11) — recency_weighted_play_count in known_artist scoring
# ---------------------------------------------------------------------------

def test_scoring_weights_recency_and_skip_penalty_defaults():
    w = ScoringWeights()
    assert w.taste_half_life_months == 18.0
    assert w.w_skipped_artist == 1.0
    assert w.skipped_artist_min_skips == 2


def test_known_artist_uses_recency_weighted_count_when_positive():
    """A profile with rwpc > 0 scores off rwpc, not the (larger) raw play_count —
    the two must differ for this test to prove rwpc is actually used."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=10, recency_weighted_play_count=2.5)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}))
    assert c.score == 2.5 * 3.0  # 7.5 — NOT 10 * 3.0 = 30.0


def test_known_artist_falls_back_to_raw_play_count_when_rwpc_zero():
    """rwpc == 0.0 (never seen in a dated mix, or mixes fetch unavailable) must
    reproduce the exact pre-issue-#11 score off raw play_count — no artist is
    silently zeroed out just because recency data doesn't cover them."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=4, recency_weighted_play_count=0.0)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}))
    assert c.score == 4 * 3.0  # unchanged from pre-#11 behaviour


def test_recurring_artist_threshold_uses_effective_count():
    """A profile whose rwpc alone clears recurring_threshold (but whose raw
    play_count does not) still earns the recurring bonus — the threshold
    check uses the effective (recency-weighted) count."""
    profiles_lower = {
        "sully": ArtistProfile(name="Sully", play_count=1, recency_weighted_play_count=3.0)
    }
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}))
    assert any(s.code == "recurring_artist" for s in c.signals)
    # known_artist: 3.0 * 3.0 = 9.0; recurring bonus: +2.0
    assert c.score == 9.0 + 2.0


def test_recurring_artist_reason_text_states_raw_play_count_not_weighted():
    """The recurring_artist explanation must keep quoting the RAW play_count
    fact ("appears in N of your mixes") even though the score math and
    threshold check both use the fractional recency-weighted count."""
    profiles_lower = {
        "sully": ArtistProfile(name="Sully", play_count=6, recency_weighted_play_count=3.25)
    }
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}))
    recurring = next(s for s in c.signals if s.code == "recurring_artist")
    assert "appears in 6 of your mixes" in recurring.explanation
    assert "3.25" not in recurring.explanation


def test_recency_weighting_configurable_half_life_does_not_affect_score_directly():
    """taste_half_life_months only matters to apply_recency_weights (profile
    build time) — _score just reads whatever rwpc is already stored, so a
    weights override here changes nothing about the score."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=10, recency_weighted_play_count=2.0)}
    c = _candidate(artist="Sully")
    weights = ScoringWeights(taste_half_life_months=6.0)
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), weights=weights)
    assert c.score == 2.0 * 3.0


# ---------------------------------------------------------------------------
# Skip-derived negative signal (issue #11)
# ---------------------------------------------------------------------------

def test_skip_penalty_fires_once_for_matched_artist():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), skip_penalty_artists={"sully"})
    # known_artist: 1 * 3.0 = 3.0; skip penalty: -1.0
    assert c.score == 2.0
    sigs = [s for s in c.signals if s.code == "skipped_artist"]
    assert len(sigs) == 1
    assert sigs[0].explanation == "You've skipped Sully recently."


def test_skip_penalty_fires_for_unresolved_artist_named_in_set():
    """The skip set can name an artist part that never resolved to a live
    profile at all (e.g. discovered once, skipped, never played) — the
    penalty still applies, checking the raw split parts of c.artist."""
    c = _candidate(artist="Unknown Artist")
    _score(c, {}, set(), {}, _build_genre_set({}), skip_penalty_artists={"unknown artist"})
    assert c.score == -1.0
    sigs = [s for s in c.signals if s.code == "skipped_artist"]
    assert len(sigs) == 1
    assert sigs[0].explanation == "You've skipped Unknown Artist recently."


def test_skip_penalty_applies_to_total_only_not_axes():
    """The penalty is a correction, not evidence — familiarity_score and
    discovery_score stay exactly what they'd be without the penalty; only the
    combined total absorbs it (same convention as pool_age)."""
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c_no_penalty = _candidate(artist="Sully")
    _score(c_no_penalty, profiles_lower, set(), {}, _build_genre_set({}))

    c_penalty = _candidate(artist="Sully")
    _score(c_penalty, profiles_lower, set(), {}, _build_genre_set({}), skip_penalty_artists={"sully"})

    assert c_penalty.familiarity_score == c_no_penalty.familiarity_score
    assert c_penalty.discovery_score == c_no_penalty.discovery_score
    assert c_penalty.score == c_no_penalty.score - 1.0


def test_skip_penalty_not_fired_when_set_is_none():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), skip_penalty_artists=None)
    assert c.score == 3.0
    assert not any(s.code == "skipped_artist" for s in c.signals)


def test_skip_penalty_not_fired_when_set_is_empty():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), skip_penalty_artists=set())
    assert c.score == 3.0
    assert not any(s.code == "skipped_artist" for s in c.signals)


def test_skip_penalty_not_fired_when_artist_not_in_set():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), skip_penalty_artists={"someone else"})
    assert c.score == 3.0
    assert not any(s.code == "skipped_artist" for s in c.signals)


def test_skip_penalty_uses_configurable_weight():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    weights = ScoringWeights(w_skipped_artist=2.5)
    _score(c, profiles_lower, set(), {}, _build_genre_set({}), weights=weights, skip_penalty_artists={"sully"})
    assert c.score == 3.0 - 2.5


def test_skip_penalty_matches_split_collaborator_part():
    """A skip set entry naming one half of a collaborative artist string still
    fires the penalty for that candidate."""
    c = _candidate(artist="Bakey, Kasia")
    _score(c, {}, set(), {}, _build_genre_set({}), skip_penalty_artists={"kasia"})
    assert c.score == -1.0
    sigs = [s for s in c.signals if s.code == "skipped_artist"]
    assert len(sigs) == 1
    assert sigs[0].explanation == "You've skipped Kasia recently."
