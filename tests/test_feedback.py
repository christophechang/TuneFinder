"""Tests for feedback.py — mark resolution, persistence, and stats aggregation."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from src.models import RecommendationRecord
from src.pipeline.feedback import (
    FeedbackEntry,
    OUTCOMES,
    load_feedback,
    append_feedback,
    resolve_selector,
    summarise_feedback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(days_ago: float = 0, offset_seconds: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago) - timedelta(seconds=offset_seconds)
    return dt.isoformat()


def _rec(
    artist="Calibre",
    title="New Dawn",
    source="beatport",
    report_id="2026-W24",
    track_no=1,
    days_ago=0,
    signal_codes=None,
    genre_tags=None,
    label=None,
    score=3.0,
) -> RecommendationRecord:
    return RecommendationRecord(
        artist=artist,
        title=title,
        link="",
        source=source,
        recommended_at=_iso(days_ago),
        report_id=report_id,
        track_no=track_no,
        signal_codes=signal_codes or [],
        genre_tags=genre_tags or [],
        score=score,
        label=label,
    )


def _entry(
    artist="Calibre",
    title="New Dawn",
    outcome="bought",
    report_id="2026-W24",
    track_no=1,
    history="weekly",
    days_ago=0,
) -> FeedbackEntry:
    from src.pipeline.dedup import make_dedup_key
    return FeedbackEntry(
        key=make_dedup_key(artist, title),
        artist=artist,
        title=title,
        outcome=outcome,
        marked_at=_iso(days_ago),
        report_id=report_id,
        track_no=track_no,
        history=history,
    )


# ---------------------------------------------------------------------------
# resolve_selector — number form
# ---------------------------------------------------------------------------

def test_number_resolves_latest_report_only():
    """Track #1 in an older report should NOT match when a newer report exists."""
    old = _rec(artist="OldArtist", title="OldTrack", report_id="2026-W23", track_no=1, days_ago=10)
    new = _rec(artist="NewArtist", title="NewTrack", report_id="2026-W24", track_no=1, days_ago=0)
    result, hist = resolve_selector("1", [old, new], [])
    assert result.artist == "NewArtist"
    assert hist == "weekly"


def test_number_rerun_tiebreak_latest_recommended_at():
    """Same report_id, same track_no → take latest recommended_at."""
    r1 = _rec(report_id="2026-W24", track_no=2, days_ago=1)  # older
    r2 = _rec(artist="Calibre", title="New Dawn v2", report_id="2026-W24", track_no=2, days_ago=0)  # newer
    result, _ = resolve_selector("2", [r1, r2], [])
    assert result.title == "New Dawn v2"


def test_number_pre_v080_raises():
    """Latest report with all track_no=None → error."""
    r = RecommendationRecord(
        artist="Sully", title="Glasshouse", link="", source="bandcamp",
        recommended_at=_iso(0), report_id="2026-W23",
    )
    with pytest.raises(LookupError, match="v0.8.0"):
        resolve_selector("1", [r], [])


def test_number_not_found_in_latest_report():
    r = _rec(track_no=3, report_id="2026-W24")
    with pytest.raises(LookupError, match="#5"):
        resolve_selector("5", [r], [])


# ---------------------------------------------------------------------------
# resolve_selector — string form
# ---------------------------------------------------------------------------

def test_string_resolution_exact():
    r = _rec(artist="Calibre", title="New Dawn")
    result, hist = resolve_selector("Calibre - New Dawn", [r], [])
    assert result.artist == "Calibre"
    assert hist == "weekly"


def test_string_resolution_raw_variant():
    """Record stored as 'Title (Original Mix)' found by 'Artist - Title'."""
    r = _rec(artist="Calibre", title="New Dawn (Original Mix)")
    result, hist = resolve_selector("Calibre - New Dawn", [r], [])
    assert result.title == "New Dawn (Original Mix)"
    assert hist == "weekly"


def test_string_resolution_mix_prep_fallback():
    """Not in weekly → found in mix-prep."""
    mp = _rec(artist="Zero T", title="Cascade", report_id="2026-W24-mix-prep-dnb")
    result, hist = resolve_selector("Zero T - Cascade", [], [mp])
    assert result.artist == "Zero T"
    assert hist == "mix-prep"


def test_string_resolution_zero_match():
    r = _rec(artist="Calibre", title="New Dawn")
    with pytest.raises(LookupError, match="No recommended track"):
        resolve_selector("Unknown - Track", [r], [])


def test_string_resolution_missing_separator():
    with pytest.raises(LookupError, match="parse"):
        resolve_selector("NoSeparatorHere", [], [])


# ---------------------------------------------------------------------------
# append / load round-trip
# ---------------------------------------------------------------------------

def test_append_and_load_round_trip(tmp_path):
    e = _entry()
    append_feedback(e, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].key == e.key
    assert loaded[0].outcome == "bought"
    assert loaded[0].history == "weekly"


def test_load_empty_returns_empty_list(tmp_path):
    assert load_feedback(str(tmp_path)) == []


def test_remark_appends_not_overwrites(tmp_path):
    """Re-marking appends a new entry — full audit trail."""
    e1 = _entry(outcome="liked", days_ago=2)
    e2 = _entry(outcome="bought", days_ago=0)
    append_feedback(e1, str(tmp_path))
    append_feedback(e2, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert len(loaded) == 2
    assert loaded[0].outcome == "liked"
    assert loaded[1].outcome == "bought"


# ---------------------------------------------------------------------------
# summarise_feedback
# ---------------------------------------------------------------------------

def _weekly_record(**kwargs) -> RecommendationRecord:
    return _rec(**kwargs)


def _mix_prep_record(**kwargs) -> RecommendationRecord:
    return _rec(report_id="2026-W24-mix-prep-dnb", **kwargs)


def test_empty_feedback_returns_empty_dict():
    result = summarise_feedback([], [], [])
    assert result == {}


def test_coverage_and_positive_rate():
    w = [_weekly_record(track_no=1), _weekly_record(artist="Sully", title="G", track_no=2)]
    entries = [_entry(outcome="bought")]  # only 1 of 2 marked
    result = summarise_feedback(w, [], entries)
    bucket = result["weekly"]
    assert bucket["recommended"] == 2
    assert bucket["marked"] == 1
    assert bucket["coverage_pct"] == 50.0
    assert bucket["positive_rate"] == 100.0


def test_own_excluded_from_positive_rate():
    w = [_weekly_record(track_no=1)]
    entries = [_entry(outcome="own")]
    result = summarise_feedback(w, [], entries)
    bucket = result["weekly"]
    assert bucket["own_count"] == 1
    assert bucket["positive_rate"] == 0.0
    assert bucket["marked"] == 1


def test_latest_entry_wins_per_history_key(tmp_path):
    """A later mix-prep mark must not erase a weekly mark for the same track."""
    w = [_weekly_record(track_no=1, signal_codes=["known_artist"])]
    mp = [_mix_prep_record(track_no=1)]

    weekly_entry = _entry(outcome="bought", history="weekly", days_ago=5)
    mp_entry = _entry(outcome="skip", history="mix-prep", days_ago=0)  # later, different history

    result = summarise_feedback(w, mp, [weekly_entry, mp_entry])
    # Weekly bucket still shows bought
    assert result["weekly"]["positive_rate"] == 100.0
    # Mix-prep bucket shows skip (0% positive)
    assert result["mix_prep"]["positive_rate"] == 0.0


def test_pre_v080_bucketed_separately():
    """Records with empty signal_codes / genre_tags → (pre-v0.8.0) bucket."""
    w = [_weekly_record(track_no=1, signal_codes=[], genre_tags=[])]
    entries = [_entry(outcome="liked")]
    result = summarise_feedback(w, [], entries)
    bucket = result["weekly"]
    assert "(pre-v0.8.0)" in bucket["by_signal"]
    assert "(pre-v0.8.0)" in bucket["by_genre"]


def test_by_signal_counts_all_codes_on_record():
    """A marked record with multiple signals contributes to each code."""
    w = [_weekly_record(track_no=1, signal_codes=["known_artist", "label_match"])]
    entries = [_entry(outcome="bought")]
    result = summarise_feedback(w, [], entries)
    sig = result["weekly"]["by_signal"]
    assert sig["known_artist"]["marked"] == 1
    assert sig["label_match"]["marked"] == 1
