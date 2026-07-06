"""Tests for the deterministic report renderer (src/pipeline/report.py)."""
from datetime import date

import pytest

from src.models import ArtistProfile, Candidate, RecommendationSignal
import re

from src.pipeline.report import (
    _format_weekly_stats,
    _format_mix_prep_stats,
    generate_report,
    generate_mix_prep_report,
    report_order,
)

TODAY = date(2026, 6, 11)


def _c(artist="A", title="T", source="beatport", label=None, link="", signals=None,
       genre_tags=None, release_date=None, raw_metadata=None):
    return Candidate(
        artist=artist, title=title, link=link, source=source, label=label,
        release_date=release_date, genre_tags=genre_tags or [],
        signals=[RecommendationSignal(code=s, explanation="") for s in (signals or [])],
        raw_metadata=raw_metadata or {},
    )


# ---------------------------------------------------------------------------
# Preserved stats tests (verbatim from original)
# ---------------------------------------------------------------------------

def test_format_weekly_stats_summarises_sections(sample_profiles):
    sections = {
        "top_picks": [
            Candidate(artist="Sully", title="T1", link="", source="s", label="L1",
                      genre_tags=["breaks"]),
            Candidate(artist="Skee Mask", title="T2", link="", source="s", label="L1",
                      genre_tags=["breaks", "electronica"]),
        ],
        "wildcards": [
            Candidate(artist="Unknown", title="T3", link="", source="s", label="L2",
                      genre_tags=["dnb"]),
        ],
    }
    line = _format_weekly_stats(sections, sample_profiles)
    assert "3 tracks" in line
    assert "2 labels" in line
    assert "2 known artists" in line
    assert "Top genres:" in line


def test_format_weekly_stats_empty_returns_empty_string():
    assert _format_weekly_stats({}, None) == ""
    assert _format_weekly_stats({"top_picks": []}, None) == ""


def test_format_weekly_stats_counts_alias_release_as_known_artist(sample_profiles):
    """A release credited to an alias resolves to its canonical profile and
    is counted once as a known artist, same as a direct-name match."""
    sections = {
        "top_picks": [
            Candidate(artist="Dave Skinner", title="T1", link="", source="s", label="L1"),
        ],
    }
    aliases = {"dave skinner": "calibre"}
    line = _format_weekly_stats(sections, sample_profiles, aliases=aliases)
    assert "1 known artists" in line


def test_format_weekly_stats_without_aliases_alias_release_not_counted(sample_profiles):
    sections = {
        "top_picks": [
            Candidate(artist="Dave Skinner", title="T1", link="", source="s", label="L1"),
        ],
    }
    line = _format_weekly_stats(sections, sample_profiles)
    assert "0 known artists" in line


def test_format_mix_prep_stats_omits_labels_and_known_artists():
    sections = {
        "top_picks": [
            Candidate(artist="A", title="T1", link="", source="s", label="L1",
                      genre_tags=["dnb"]),
            Candidate(artist="B", title="T2", link="", source="s", label="L1",
                      genre_tags=["dnb", "breaks"]),
        ],
    }
    line = _format_mix_prep_stats(sections)
    assert "2 tracks" in line
    assert "Top genres:" in line
    assert "dnb" in line
    assert "labels" not in line
    assert "known artists" not in line


def test_format_mix_prep_stats_empty():
    assert _format_mix_prep_stats({}) == ""


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------

def test_empty_section_omitted():
    sections = {"top_picks": [_c("X", "T")], "wildcards": []}
    report = generate_report(sections, "TEST", {}, object(), today=TODAY)
    assert "## 🔺 Top Picks" in report
    assert "## 🃏 Wildcards" not in report


def test_continuous_numbering_across_sections():
    sections = {
        "top_picks": [_c("A", "T1"), _c("B", "T2")],
        "wildcards": [_c("C", "T3")],
    }
    report = generate_report(sections, "TEST", {}, object(), today=TODAY)
    assert "1. **A" in report
    assert "2. **B" in report
    assert "3. **C" in report


def test_label_watch_grouping_and_italic_artist_line():
    label_artists = {"ilian tape": ["Calibre", "Skee Mask"]}
    c = _c("Newcomer", "Track", "volumo", label="Ilian Tape", link="https://example.com",
           signals=["label_match"])
    sections = {"label_watch": [c]}
    report = generate_report(sections, "TEST", {}, object(),
                             label_artists=label_artists, today=TODAY)
    assert "**Ilian Tape**" in report
    assert "*2 of your artists release here: Calibre, Skee Mask*" in report


def test_label_watch_four_artists_true_count_with_first_three():
    label_artists = {"big label": ["A", "B", "C", "D"]}
    c = _c("X", "T", "volumo", label="Big Label", signals=["label_match"])
    sections = {"label_watch": [c]}
    report = generate_report(sections, "TEST", {}, object(),
                             label_artists=label_artists, today=TODAY)
    # True count (4), first 3 names, ellipsis
    assert "4 of your artists" in report
    assert "A, B, C, …" in report
    # D should not appear as a name in the artist line
    assert "D" not in report.split("**Big Label**")[1].split("\n")[1]


def test_label_watch_no_names_omits_artist_line():
    c = _c("X", "T", "volumo", label="Mystery Label", signals=["label_match"])
    sections = {"label_watch": [c]}
    report = generate_report(sections, "TEST", {}, object(),
                             label_artists={}, today=TODAY)
    assert "**Mystery Label**" in report
    assert "of your artists" not in report


def test_no_label_track_no_label_bracket():
    c = _c("X", "T", "beatport", label=None)
    sections = {"wildcards": [c]}
    report = generate_report(sections, "TEST", {}, object(), today=TODAY)
    assert "[None]" not in report
    assert "**X — T**" in report


def test_no_link_no_listen():
    c = _c("X", "T", "volumo", link="")
    sections = {"wildcards": [c]}
    report = generate_report(sections, "TEST", {}, object(), today=TODAY)
    assert "Listen" not in report
    assert "→" not in report


def test_sanitiser_applied_to_output():
    # A bare URL in a reason would get stripped by _sanitize_report
    c = _c("X", "T", "volumo", link="https://volumo.com/track/abc",
           raw_metadata={"seen_on_sources": ["volumo", "beatport"]},
           signals=["cross_source"])
    sections = {"wildcards": [c]}
    report = generate_report(sections, "TEST", {}, object(), today=TODAY)
    # Ensure all links in report are in angle-bracket form
    import re
    bare_url_re = re.compile(r'(?<![(<])(https?://\S+)(?![)>])')
    assert not bare_url_re.search(report)


def test_profile_path_produces_artist_grounded_reason():
    profiles = {"calibre": ArtistProfile(name="Calibre", play_count=6,
                                          track_titles=["Shifting"])}
    c = _c("Calibre", "New One", "beatport", signals=["known_artist"])
    sections = {"artist_watch": [c]}
    report = generate_report(sections, "TEST", {}, object(),
                             profiles={"Calibre": profiles["calibre"]}, today=TODAY)
    # Should produce an artist-grounded reason (You play / plays in your mix history)
    assert "Calibre" in report
    assert ("6 plays" in report or "You play" in report or "follow-up" in report)


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------

def _make_weekly_fixture():
    profiles = {
        "Calibre": ArtistProfile(name="Calibre", play_count=8, genres_seen=["dnb"],
                                  track_titles=["Shifting", "Gravitas"]),
        "Sully": ArtistProfile(name="Sully", play_count=4, genres_seen=["breaks"],
                                track_titles=["Swandive"]),
    }
    c1 = _c("Calibre", "New Dawn", "beatport", label="Signature",
            link="https://beatport.com/track/new-dawn/1",
            signals=["known_artist", "chart_position"],
            genre_tags=["dnb"], release_date="2026-06-08",
            raw_metadata={"chart_position": 3})
    c2 = _c("Skee Mask", "Idealism", "beatport",
            link="https://beatport.com/track/idealism/2",
            signals=["chart_position"],
            genre_tags=["electronica"], release_date="2026-06-06",
            raw_metadata={"chart_position": 7})
    c3 = _c("Pola & Bryson", "Somewhere", "volumo", label="Metalheadz",
            link="https://volumo.com/track/somewhere",
            signals=["label_match"],
            genre_tags=["dnb"], release_date="2026-06-05")
    c4 = _c("Sully", "Glasshouse", "bandcamp",
            link="https://bandcamp.com/track/glasshouse",
            signals=["known_artist"],
            genre_tags=["breaks"], release_date="2026-06-09")
    c5 = _c("Unknown Producer", "Deep Space", "volumo",
            link="", signals=[], genre_tags=["house"])
    return profiles, {
        "top_picks": [c1, c2],
        "label_watch": [c3],
        "artist_watch": [c4],
        "wildcards": [c5],
    }


def _make_mix_prep_fixture():
    profiles = {
        "Calibre": ArtistProfile(name="Calibre", play_count=8, genres_seen=["dnb"],
                                  track_titles=["Shifting", "Gravitas"]),
    }
    mp1 = _c("Calibre", "Soul On Fire", "beatport", label="Signature",
             link="https://beatport.com/track/soul/3",
             signals=["known_artist", "chart_position"],
             genre_tags=["dnb"], release_date="2026-06-07",
             raw_metadata={"chart_position": 1})
    mp2 = _c("Zero T", "Cascade", "volumo", label="Hospital",
             link="https://volumo.com/track/cascade",
             signals=["genre_match"],
             genre_tags=["dnb"], release_date="2026-06-03")
    return profiles, {"top_picks": [mp1], "deep_cuts": [mp2]}


_WEEKLY_SNAPSHOT = (
    "**TuneFinder — 11 June 2026 (2026-W24)**\n"
    "*This week: 5 tracks across 2 labels, 2 known artists. Top genres: dnb, electronica, breaks.*\n"
    "\n"
    "## 🔺 Top Picks\n"
    "1. **Calibre — New Dawn** [Signature] [Beatport] → [Listen](<https://beatport.com/track/new-dawn/1>)\n"
    "> Calibre again — #3 on Beatport dnb; you've played Shifting.\n"
    "2. **Skee Mask — Idealism** [Beatport] → [Listen](<https://beatport.com/track/idealism/2>)\n"
    "> #7 on the Beatport electronica chart this week.\n"
    "\n"
    "## 🏷️ Label Watch\n"
    "**Metalheadz**\n"
    "*2 of your artists release here: Calibre, Sully*\n"
    "3. **Pola & Bryson — Somewhere** [Metalheadz] [Volumo] → [Listen](<https://volumo.com/track/somewhere>)\n"
    "> Metalheadz — Calibre, Sully release here; you play them all.\n"
    "\n"
    "## 👁️ Artist Watch\n"
    "4. **Sully — Glasshouse** [Bandcamp] → [Listen](<https://bandcamp.com/track/glasshouse>)\n"
    "> Sully follow-up to Swandive — 4 plays in your mix history.\n"
    "\n"
    "## 🃏 Wildcards\n"
    "5. **Unknown Producer — Deep Space** [Volumo]\n"
    "> New release via Volumo.\n"
    "\n"
    "## ⚙️ Processing Summary\n"
    "📥 Sources fetched: **100**\n"
    "🔀 After dedup: **80**\n"
    "🎵 After known-track filter: **60**\n"
    "📋 After history filter: **55**\n"
    "♻️ Pool injected: **5**\n"
    "🎯 Tracks in report: **5**\n"
    "`Report ID: 2026-W24`"
)

_MIX_PREP_SNAPSHOT = (
    "🎛️ Dnb Mix Prep Report\n"
    "Report ID: 2026-W24-mix-prep-dnb\n"
    "Date: 11 June 2026\n"
    "\n"
    "*This set: 2 tracks. Top genres: dnb.*\n"
    "\n"
    "## 🔺 Top Picks (dnb)\n"
    "1. **Calibre — Soul On Fire** [Signature] [Beatport] → [Listen](<https://beatport.com/track/soul/3>)\n"
    "> You play Calibre (Shifting) — now #1 on the Beatport dnb chart.\n"
    "\n"
    "## 🎧 Deep Cuts\n"
    "2. **Zero T — Cascade** [Hospital] [Volumo] → [Listen](<https://volumo.com/track/cascade>)\n"
    "> Fresh dnb on Hospital, out 8 days ago.\n"
    "\n"
    "## ⚙️ Processing Summary\n"
    "📥 Sources fetched: **50**\n"
    "🔀 After dedup: **40**\n"
    "🎚️ After genre filter: **20**\n"
    "♻️ Pool injected: **2**\n"
    "`Report ID: 2026-W24-mix-prep-dnb`"
)


def test_weekly_snapshot():
    profiles, sections = _make_weekly_fixture()
    label_artists = {
        "signature": ["Calibre", "Sully"],
        "metalheadz": ["Calibre", "Sully"],
    }
    result = generate_report(
        sections, "2026-W24",
        {"sources_fetched": 100, "after_dedup": 80, "after_known": 60,
         "after_history": 55, "after_release_date": 40, "pool_injected": 5},
        object(),
        profiles=profiles,
        label_artists=label_artists,
        today=TODAY,
    )
    assert result == _WEEKLY_SNAPSHOT


def test_mix_prep_snapshot():
    profiles, sections = _make_mix_prep_fixture()
    result = generate_mix_prep_report(
        sections, "2026-W24-mix-prep-dnb",
        {"sources_fetched": 50, "after_dedup": 40, "after_genre": 20, "pool_injected": 2},
        "dnb",
        object(),
        profiles=profiles,
        label_artists={"signature": ["Calibre"]},
        today=TODAY,
    )
    assert result == _MIX_PREP_SNAPSHOT


# ---------------------------------------------------------------------------
# report_order tests
# ---------------------------------------------------------------------------

def _parse_track_numbers(report_text: str) -> list[tuple[int, str, str]]:
    """Extract (n, artist, title) from '1. **Artist — Title**...' lines."""
    found = []
    for line in report_text.splitlines():
        m = re.match(r'^(\d+)\. \*\*(.+?) — (.+?)\*\*', line)
        if m:
            found.append((int(m.group(1)), m.group(2), m.group(3)))
    return found


def test_report_order_matches_weekly_render():
    profiles, sections = _make_weekly_fixture()
    label_artists = {
        "signature": ["Calibre", "Sully"],
        "metalheadz": ["Calibre", "Sully"],
    }
    rendered = generate_report(
        sections, "2026-W24",
        {"sources_fetched": 100, "after_dedup": 80, "after_known": 60,
         "after_history": 55, "after_release_date": 40, "pool_injected": 5},
        object(), profiles=profiles, label_artists=label_artists, today=TODAY,
    )
    parsed = _parse_track_numbers(rendered)
    ordered = list(enumerate(report_order(sections), start=1))
    for (n, artist, title), (i, c) in zip(parsed, ordered):
        assert n == i
        assert artist == c.artist
        assert title == c.title


def test_report_order_matches_mix_prep_render():
    profiles, sections = _make_mix_prep_fixture()
    rendered = generate_mix_prep_report(
        sections, "2026-W24-mix-prep-dnb",
        {"sources_fetched": 50, "after_dedup": 40, "after_genre": 20, "pool_injected": 2},
        "dnb", object(), profiles=profiles,
        label_artists={"signature": ["Calibre"]}, today=TODAY,
    )
    parsed = _parse_track_numbers(rendered)
    ordered = list(enumerate(report_order(sections), start=1))
    for (n, artist, title), (i, c) in zip(parsed, ordered):
        assert n == i
        assert artist == c.artist
        assert title == c.title


def test_report_order_interleaved_labels():
    """label_watch with interleaved labels: rendered groups by label, not raw order."""
    cA1 = _c("ArtistA", "Track1", label="LabelA")
    cB1 = _c("ArtistB", "Track2", label="LabelB")
    cA2 = _c("ArtistA2", "Track3", label="LabelA")
    sections = {"label_watch": [cA1, cB1, cA2]}
    ordered = report_order(sections)
    # LabelA group first (both tracks), then LabelB
    assert ordered == [cA1, cA2, cB1]


def test_report_order_shuffled_key_order_identical():
    """Dict key order must not affect report_order output."""
    profiles, sections_natural = _make_weekly_fixture()
    # Rebuild with reversed key order
    sections_reversed = {k: sections_natural[k] for k in reversed(list(sections_natural))}
    assert report_order(sections_natural) == report_order(sections_reversed)


def test_report_order_unknown_key_raises():
    sections = {"top_picks": [], "unknown_section": []}
    with pytest.raises(ValueError, match="unknown_section"):
        report_order(sections)
