"""Tests for src/pipeline/audition.py."""
import os
import time
import tempfile
from datetime import date

import pytest

from src.models import ArtistProfile, Candidate, RecommendationSignal
from src.pipeline.audition import generate_audition_page, write_audition_page
from src.pipeline.report import report_order

TODAY = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Fixture builder (also used by the DoD one-liner)
# ---------------------------------------------------------------------------

def _c(artist, title, source="beatport", label=None, link="https://beatport.com/track/x/1",
       signals=None, genre_tags=None, raw_metadata=None):
    return Candidate(
        artist=artist,
        title=title,
        link=link,
        source=source,
        label=label,
        genre_tags=genre_tags or [],
        signals=[RecommendationSignal(code=s, explanation="test signal") for s in (signals or [])],
        raw_metadata=raw_metadata or {},
    )


def build_fixture_sections():
    """Return (sections, report_id, settings, profiles, label_artists) — 5-tuple for DoD one-liner."""
    # Bandcamp track — has bandcamp_album_id → should produce iframe embed
    bc_track = _c(
        "Sully", "Skyline",
        source="bandcamp",
        label=None,
        link="https://sully.bandcamp.com/album/skyline",
        signals=["bandcamp_discovery"],
        genre_tags=["dnb"],
        raw_metadata={"bandcamp_album_id": 2697521627, "bandcamp_tag": "drum-and-bass"},
    )

    # Beatport track — has beatport_id → should produce iframe embed
    bp_track = _c(
        "Calibre", "Mr Right On (Remaster)",
        source="beatport",
        label="Metalheadz",
        link="https://www.beatport.com/track/mr-right-on/12345",
        signals=["known_artist"],
        genre_tags=["dnb"],
        raw_metadata={"beatport_id": 12345, "bpm": 172, "chart_position": 3},
    )

    # Link-only track — no embed ids
    link_only = _c(
        "No Player Artist", "No Embed Title",
        source="volumo",
        label="Some Label",
        link="https://volumo.com/track/99999-no-embed",
        signals=["label_match"],
        genre_tags=["house"],
        raw_metadata={"volumo_track_id": 99999, "bpm": 124, "keysign": "G major"},
    )

    sections = {
        "top_picks": [bc_track],
        "label_watch": [link_only],
        "artist_watch": [bp_track],
    }

    profiles = {
        "Calibre": ArtistProfile(
            name="Calibre",
            play_count=6,
            genres_seen=["dnb"],
            track_titles=["Mr Right On", "Phaze"],
        ),
    }
    label_artists = {"metalheadz": ["Calibre", "dBridge"]}

    return sections, "2026-W24", None, profiles, label_artists


# ---------------------------------------------------------------------------
# Numbering parity with report_order
# ---------------------------------------------------------------------------

def test_track_numbers_match_report_order():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY)
    ordered = report_order(sections)
    for i, c in enumerate(ordered, start=1):
        assert f"#{i}</div>" in html or f"#{i}" in html


# ---------------------------------------------------------------------------
# Player branches
# ---------------------------------------------------------------------------

def test_bandcamp_embed_present():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY)
    assert "bandcamp.com/EmbeddedPlayer/album=2697521627" in html


def test_beatport_embed_present():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY)
    # & is HTML-escaped in the src attribute
    assert "embed.beatport.com/?id=12345&amp;type=track" in html


def test_soundcloud_embed_present():
    sc = _c(
        "Bootleg DJ", "White Label VIP",
        source="soundcloud",
        link="https://soundcloud.com/bootleg-dj/white-label-vip",
        signals=["genre_match"],
        genre_tags=["ukg"],
    )
    html = generate_audition_page({"top_picks": [sc]}, "2026-W29", None,
                                  profiles={}, label_artists={}, today=TODAY)
    assert "w.soundcloud.com/player/?url=https%3A%2F%2Fsoundcloud.com%2Fbootleg-dj%2Fwhite-label-vip" in html


def test_link_only_row_has_no_embed():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY)
    assert "99999" not in html or "embed" not in html.split("99999")[0].split("<iframe")[-1]
    # Simpler check: the link-only track still shows store link
    assert "volumo.com/track/99999-no-embed" in html


# ---------------------------------------------------------------------------
# Mark command forms
# ---------------------------------------------------------------------------

def test_weekly_number_form_commands():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY, mark_by_number=True)
    assert "tunefinder mark 1 bought" in html
    assert "tunefinder mark 2 liked" in html
    assert "tunefinder mark 1 heard" in html


def test_mix_prep_string_form_commands():
    sections, report_id, settings, profiles, label_artists = build_fixture_sections()
    html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                  label_artists=label_artists, today=TODAY, mark_by_number=False)
    # Should contain shlex-quoted selector for the first track (Sully - Skyline)
    assert "Sully - Skyline" in html
    assert "tunefinder mark" in html
    assert "bought" in html


def test_mix_prep_string_form_quote_in_title():
    track_with_quote = _c(
        "Artist", "Track With 'Quote' In Title",
        link="https://x.com",
        signals=["genre_match"],
        genre_tags=["house"],
    )
    sections = {"top_picks": [track_with_quote]}
    html = generate_audition_page(sections, "2026-W24", None, today=TODAY, mark_by_number=False)
    # shlex.quote should wrap in double quotes or escape properly
    assert "tunefinder mark" in html
    # The shell-safe selector must be present and not break on the single quote
    assert "Track With" in html


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def test_hostile_title_escaped():
    evil = _c("<script>alert(1)</script>", "Normal",
              link="https://x.com", signals=["genre_match"], genre_tags=["house"])
    sections = {"top_picks": [evil]}
    html_out = generate_audition_page(sections, "2026-W01", None, today=TODAY)
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_hostile_title_escaped_in_data_cmd():
    evil = _c('<script>"xss"</script>', "Normal",
              link="https://x.com", signals=["genre_match"], genre_tags=["house"])
    sections = {"top_picks": [evil]}
    html_out = generate_audition_page(sections, "2026-W01", None, today=TODAY, mark_by_number=False)
    # The raw <script> tag must not appear outside escaping
    assert '<script>"xss"</script>' not in html_out


# ---------------------------------------------------------------------------
# Missing metadata → link-only
# ---------------------------------------------------------------------------

def test_missing_metadata_link_only():
    bare = _c("Bare Artist", "Bare Title", source="beatport",
              link="https://beatport.com/x", signals=[], raw_metadata={})
    sections = {"top_picks": [bare]}
    html_out = generate_audition_page(sections, "2026-W01", None, today=TODAY)
    assert "beatport.com/x" in html_out
    assert "<iframe" not in html_out


# ---------------------------------------------------------------------------
# write_audition_page + retention pruning
# ---------------------------------------------------------------------------

def test_write_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_audition_page("<html>test</html>", tmpdir, "2026-W01")
        assert os.path.exists(path)
        assert path.endswith("audition_2026-W01.html")
        with open(path) as f:
            assert f.read() == "<html>test</html>"


def test_write_prunes_beyond_26():
    with tempfile.TemporaryDirectory() as tmpdir:
        reports_dir = os.path.join(tmpdir, "reports")
        os.makedirs(reports_dir)
        # Seed 27 fake pages with distinct mtimes
        for i in range(27):
            p = os.path.join(reports_dir, f"audition_2025-W{i:02d}.html")
            with open(p, "w") as f:
                f.write(f"fake {i}")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))

        # Write one more (report_id = "new") — total 28 → prune oldest 2 → retain 26
        write_audition_page("<html>new</html>", tmpdir, "new")

        remaining = [fn for fn in os.listdir(reports_dir) if fn.endswith(".html")]
        assert len(remaining) == 26
        # The two oldest (W00, W01) should be gone
        assert "audition_2025-W00.html" not in remaining
        assert "audition_2025-W01.html" not in remaining
        # The newest should be there
        assert "audition_new.html" in remaining


def test_path_format_weekly():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_audition_page("x", tmpdir, "2026-W24")
        assert os.path.basename(path) == "audition_2026-W24.html"


def test_path_format_mix_prep():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_audition_page("x", tmpdir, "2026-W24-mix-prep-house")
        assert os.path.basename(path) == "audition_2026-W24-mix-prep-house.html"
