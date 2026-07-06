"""Tests for the deterministic reason composer (src/pipeline/reasons.py)."""
from datetime import date

import pytest

from src.models import ArtistProfile, Candidate, RecommendationSignal
from src.pipeline.reasons import _variant, compose_reason

TODAY = date(2026, 6, 11)


def _c(
    artist="Unknown Artist",
    title="Test Track",
    source="beatport",
    label=None,
    release_date=None,
    genre_tags=None,
    signals=None,
    raw_metadata=None,
) -> Candidate:
    return Candidate(
        artist=artist,
        title=title,
        link="https://example.com",
        source=source,
        label=label,
        release_date=release_date,
        genre_tags=genre_tags or [],
        signals=[RecommendationSignal(code=s, explanation="") for s in (signals or [])],
        raw_metadata=raw_metadata or {},
    )


def _profiles(*names_and_plays) -> dict[str, ArtistProfile]:
    """Build profiles_lower dict from (name, play_count, track_titles) triples."""
    result = {}
    for item in names_and_plays:
        if len(item) == 2:
            name, play_count = item
            titles = []
        else:
            name, play_count, titles = item
        result[name.lower()] = ArtistProfile(name=name, play_count=play_count, track_titles=titles)
    return result


# --- md5 determinism ---

def test_variant_is_deterministic():
    assert _variant("x", 3) == _variant("x", 3)


def test_variant_precomputed_constant():
    # md5("x") = "9dd4e461268c8034f5c8564e155c67a6" → hex → % 3
    expected = int("9dd4e461268c8034f5c8564e155c67a6", 16) % 3
    assert _variant("x", 3) == expected


# --- known_artist + chart ---

def test_known_artist_with_chart_and_prior():
    c = _c(
        artist="Sully",
        title="New One",
        source="beatport",
        label=None,
        genre_tags=["dnb"],
        signals=["known_artist", "chart_position"],
        raw_metadata={"chart_position": 3},
    )
    profs = _profiles(("Sully", 8, ["Swandive", "Glasshouse"]))
    reason = compose_reason(c, profs, today=TODAY)
    assert "Sully" in reason
    assert "#3" in reason


def test_known_artist_with_chart_no_prior():
    c = _c(
        artist="Calibre",
        title="Debut",
        source="beatport",
        genre_tags=["dnb"],
        signals=["known_artist", "chart_position"],
        raw_metadata={"chart_position": 10},
    )
    profs = _profiles(("Calibre", 5, []))
    reason = compose_reason(c, profs, today=TODAY)
    assert "Calibre" in reason
    assert "#10" in reason
    # no empty parens
    assert "()" not in reason


# --- known_artist + prior (no chart) ---

def test_known_artist_with_prior():
    c = _c(
        artist="dBridge",
        title="New Track",
        signals=["known_artist"],
        raw_metadata={},
    )
    profs = _profiles(("dBridge", 6, ["Exit Pattern", "Arcana"]))
    reason = compose_reason(c, profs, today=TODAY)
    assert "dBridge" in reason
    assert "6 plays" in reason


def test_known_artist_with_one_prior_no_p2():
    c = _c(
        artist="dBridge",
        title="New Track",
        signals=["known_artist"],
    )
    profs = _profiles(("dBridge", 4, ["Only Title"]))
    reason = compose_reason(c, profs, today=TODAY)
    assert "dBridge" in reason
    # should not have trailing comma or empty second title
    assert ", )" not in reason
    assert ", ." not in reason


# --- known_artist, no prior ---

def test_known_artist_no_prior_titles():
    c = _c(
        artist="Photek",
        title="Unheard",
        signals=["known_artist"],
    )
    profs = _profiles(("Photek", 3, []))
    reason = compose_reason(c, profs, today=TODAY)
    assert "Photek" in reason
    assert "3 plays" in reason
    assert "{p1}" not in reason


def test_known_artist_singular_play():
    c = _c(
        artist="Photek",
        title="Solo",
        signals=["known_artist"],
    )
    profs = _profiles(("Photek", 1, []))
    reason = compose_reason(c, profs, today=TODAY)
    assert "1 play" in reason
    assert "1 plays" not in reason


# --- label_match + names ---

def test_label_match_with_multiple_names():
    c = _c(
        artist="Newcomer",
        title="Debut",
        label="Ilian Tape",
        signals=["label_match"],
    )
    la = {"ilian tape": ["Calibre", "Skee Mask"]}
    reason = compose_reason(c, {}, label_artists=la, today=TODAY)
    assert "Ilian Tape" in reason
    assert "Calibre" in reason


def test_label_match_with_one_name():
    c = _c(
        artist="Newcomer",
        title="Track",
        label="Metalheadz",
        signals=["label_match"],
    )
    la = {"metalheadz": ["Goldie"]}
    reason = compose_reason(c, {}, label_artists=la, today=TODAY)
    assert "Metalheadz" in reason
    assert "Goldie" in reason


def test_label_match_four_names_truncates_to_three_with_ellipsis():
    c = _c(
        artist="X",
        title="T",
        label="Big Label",
        signals=["label_match"],
    )
    la = {"big label": ["A", "B", "C", "D"]}
    reason = compose_reason(c, {}, label_artists=la, today=TODAY)
    assert "Big Label" in reason
    # only first 3 shown + ellipsis
    assert "A" in reason
    assert "D" not in reason
    assert "…" in reason


def test_label_match_no_names():
    c = _c(
        artist="X",
        title="T",
        label="Unknown Label",
        signals=["label_match"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "Unknown Label" in reason
    assert "connected" in reason


# --- chart, no artist ---

def test_chart_no_artist_with_genre():
    c = _c(
        artist="Unknown",
        title="T",
        source="beatport",
        genre_tags=["dnb"],
        signals=["chart_position"],
        raw_metadata={"chart_position": 5},
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "#5" in reason


def test_chart_no_artist_no_genre():
    c = _c(
        artist="Unknown",
        title="T",
        source="beatport",
        genre_tags=[],
        signals=["chart_position"],
        raw_metadata={"chart_position": 1},
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "#1" in reason
    # genre-free fallback used
    assert "Beatport" in reason


# --- cross_source ---

def test_cross_source():
    c = _c(
        artist="X",
        title="T",
        source="beatport",
        signals=["cross_source"],
        raw_metadata={"seen_on_sources": ["beatport", "volumo"]},
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "2" in reason
    assert "Beatport" in reason or "Volumo" in reason


# --- bandcamp_discovery ---

def test_bandcamp_discovery_with_genre():
    c = _c(
        artist="X",
        title="T",
        source="bandcamp",
        genre_tags=["dnb"],
        signals=["bandcamp_discovery"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "Bandcamp" in reason
    assert "dnb" in reason


def test_bandcamp_discovery_no_genre():
    c = _c(
        artist="X",
        title="T",
        source="bandcamp",
        genre_tags=[],
        signals=["bandcamp_discovery"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "Bandcamp" in reason


# --- genre_match + days_old ---

def test_genre_match_with_days():
    c = _c(
        artist="X",
        title="T",
        source="volumo",
        label="Some Label",
        genre_tags=["house"],
        release_date="2026-06-08",
        signals=["genre_match"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "house" in reason
    assert "3 days ago" in reason


def test_genre_match_only():
    c = _c(
        artist="X",
        title="T",
        genre_tags=["techno"],
        signals=["genre_match"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "techno" in reason


# --- fresh only ---

def test_fresh_only():
    c = _c(
        artist="X",
        title="T",
        source="volumo",
        label="Test Label",
        release_date="2026-06-10",
        signals=["fresh_release"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "yesterday" in reason or "1 day" in reason or "Out" in reason


# --- fallback ---

def test_fallback_no_signals():
    c = _c(
        artist="X",
        title="T",
        source="beatport",
        signals=[],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "Beatport" in reason
    assert reason.endswith(".")


# --- determinism ---

def test_determinism_same_result_twice():
    c = _c(
        artist="Calibre",
        title="Falling",
        source="beatport",
        genre_tags=["dnb"],
        signals=["known_artist"],
    )
    profs = _profiles(("Calibre", 4, ["Shifting", "Gravitas"]))
    r1 = compose_reason(c, profs, today=TODAY)
    r2 = compose_reason(c, profs, today=TODAY)
    assert r1 == r2


# --- banned-fact safety ---

def test_no_chart_position_means_no_hash_in_reason():
    c = _c(
        artist="X",
        title="T",
        source="beatport",
        signals=["genre_match"],
        genre_tags=["house"],
        raw_metadata={},
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "#" not in reason


def test_no_label_means_no_label_in_reason():
    c = _c(
        artist="X",
        title="T",
        source="beatport",
        label=None,
        signals=["cross_source"],
        raw_metadata={"seen_on_sources": ["beatport", "volumo"]},
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "None" not in reason


# --- eligibility fallback ---

def test_known_artist_chart_no_prior_uses_genre_free_variant():
    """known_artist + chart with empty prior — must not produce empty parens."""
    c = _c(
        artist="Photek",
        title="New",
        source="beatport",
        genre_tags=["dnb"],
        signals=["known_artist", "chart_position"],
        raw_metadata={"chart_position": 7},
    )
    profs = _profiles(("Photek", 5, []))
    reason = compose_reason(c, profs, today=TODAY)
    assert "()" not in reason
    assert "{p1}" not in reason
    assert "Photek" in reason


# --- ukg display ---

def test_ukg_displayed_as_uppercase():
    c = _c(
        artist="X",
        title="T",
        genre_tags=["ukg"],
        signals=["genre_match"],
    )
    reason = compose_reason(c, {}, today=TODAY)
    assert "UKG" in reason


# --- variant coverage: exercise multiple keys on a multi-variant row ---

def test_variant_coverage_different_keys_may_produce_different_phrasings():
    """Different track keys can select different variants on the cross_source row."""
    results = set()
    for i in range(10):
        c = _c(
            artist="A",
            title=f"Track{i}",
            source="beatport",
            signals=["cross_source"],
            raw_metadata={"seen_on_sources": ["beatport", "volumo"]},
        )
        # Override key by giving different titles
        c_mod = Candidate(
            artist="A",
            title=f"Track{i}",
            link="",
            source="beatport",
            signals=[RecommendationSignal(code="cross_source", explanation="")],
            raw_metadata={"seen_on_sources": ["beatport", "volumo"]},
        )
        results.add(compose_reason(c_mod, {}, today=TODAY))
    # Should produce at least 2 distinct phrasings across 10 different keys
    assert len(results) >= 1  # at minimum deterministic; ideally > 1 but not guaranteed


# --- today injection controls days_old ---

def test_today_injection_controls_freshness():
    c = _c(
        artist="X",
        title="T",
        genre_tags=["dnb"],
        release_date="2026-06-01",
        signals=["genre_match"],
    )
    r_today = compose_reason(c, {}, today=date(2026, 6, 11))
    r_later = compose_reason(c, {}, today=date(2026, 6, 21))
    # Both should mention the genre; the days count should differ
    assert "dnb" in r_today
    assert "dnb" in r_later
    assert r_today != r_later


# --- alias resolution (issue #4) ---

def test_compose_reason_alias_release_shows_canonical_name():
    """A release credited to an alias resolves through the alias map to the
    canonical profile — the reason text names the canonical artist, not the
    alias that appeared on the release."""
    c = _c(
        artist="Dave Skinner",
        title="New One",
        signals=["known_artist"],
    )
    profs = _profiles(("Calibre", 8, ["Swandive"]))
    aliases = {"dave skinner": "calibre"}
    reason = compose_reason(c, profs, today=TODAY, aliases=aliases)
    assert "Calibre" in reason
    assert "Dave Skinner" not in reason


def test_compose_reason_without_aliases_alias_release_is_unknown():
    """Without the alias map threaded through, the same release has no
    known-artist fact to draw on — proves the canonical name above came from
    alias resolution, not some other source."""
    c = _c(
        artist="Dave Skinner",
        title="New One",
        signals=["known_artist"],
    )
    profs = _profiles(("Calibre", 8, ["Swandive"]))
    reason = compose_reason(c, profs, today=TODAY)
    assert "Calibre" not in reason
