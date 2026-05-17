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
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    assert counts["ilian tape"] == 3

    target = _candidate(artist="Unknown", title="T", label="Ilian Tape")
    _score(target, profiles_lower, {"ilian tape"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 3.0


def test_label_signal_base_when_one_known_artist():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="Other", title="T", label="Astrophonica")
    _score(target, profiles_lower, {"astrophonica"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 2.0


def test_label_signal_caps_at_three_artists():
    profiles_lower = {f"a{i}": ArtistProfile(name=f"A{i}") for i in range(5)}
    candidates = [_candidate(artist=f"A{i}", label="Big Label") for i in range(5)]
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="X", title="T", label="Big Label")
    _score(target, profiles_lower, {"big label"}, counts, _build_genre_set(profiles_lower))
    assert target.score == 3.0


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
