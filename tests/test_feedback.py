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
    latest_marks,
    skipped_artists,
    tune_report,
    _MIN_MARKS_FOR_CONCLUSIONS,
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


# ---------------------------------------------------------------------------
# latest_marks (shared latest-mark-per-(history,key) helper)
# ---------------------------------------------------------------------------

def test_latest_marks_collapses_to_newest_per_history_key():
    old = _entry(outcome="skip", history="weekly", days_ago=5)
    new = _entry(outcome="bought", history="weekly", days_ago=0)
    result = latest_marks([old, new])
    assert len(result) == 1
    assert result[0].outcome == "bought"


def test_latest_marks_keeps_distinct_histories():
    w = _entry(outcome="bought", history="weekly")
    mp = _entry(outcome="skip", history="mix-prep")
    result = latest_marks([w, mp])
    assert len(result) == 2
    assert {e.history for e in result} == {"weekly", "mix-prep"}


# ---------------------------------------------------------------------------
# skipped_artists — skip-derived negative signal (issue #11)
# ---------------------------------------------------------------------------

def test_skipped_artists_below_threshold_not_included():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", days_ago=2),
    ]
    assert "sully" not in skipped_artists(entries, min_skips=2)


def test_skipped_artists_meets_threshold_included():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", days_ago=2),
        _entry(artist="Sully", title="T2", outcome="skip", days_ago=1),
    ]
    assert skipped_artists(entries, min_skips=2) == {"sully"}


def test_skipped_artists_positive_mark_disqualifies_even_with_enough_skips():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", days_ago=3),
        _entry(artist="Sully", title="T2", outcome="skip", days_ago=2),
        _entry(artist="Sully", title="T3", outcome="liked", days_ago=1),
    ]
    assert skipped_artists(entries, min_skips=2) == set()


def test_skipped_artists_bought_also_disqualifies():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", days_ago=3),
        _entry(artist="Sully", title="T2", outcome="skip", days_ago=2),
        _entry(artist="Sully", title="T3", outcome="bought", days_ago=1),
    ]
    assert skipped_artists(entries, min_skips=2) == set()


def test_skipped_artists_latest_mark_semantics_skip_then_liked_supersedes():
    """A track re-marked 'liked' after an earlier 'skip' on the SAME track means
    only the latest mark (liked) counts for that (history, key) — the earlier
    skip is superseded, not counted at all."""
    from src.pipeline.dedup import make_dedup_key
    entries = [
        FeedbackEntry(
            key=make_dedup_key("Sully", "Same Track"), artist="Sully", title="Same Track",
            outcome="skip", marked_at=_iso(5), report_id="2026-W20", track_no=1, history="weekly",
        ),
        FeedbackEntry(
            key=make_dedup_key("Sully", "Same Track"), artist="Sully", title="Same Track",
            outcome="liked", marked_at=_iso(1), report_id="2026-W20", track_no=1, history="weekly",
        ),
        # A second, distinct track still skipped and un-superseded.
        _entry(artist="Sully", title="Other Track", outcome="skip", days_ago=2),
    ]
    # Only 1 surviving 'skip' (the second track) — below threshold of 2, and
    # the surviving 'liked' mark disqualifies the artist regardless.
    assert skipped_artists(entries, min_skips=2) == set()


def test_skipped_artists_own_outcome_is_neutral():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", days_ago=3),
        _entry(artist="Sully", title="T2", outcome="skip", days_ago=2),
        _entry(artist="Sully", title="T3", outcome="own", days_ago=1),
    ]
    assert skipped_artists(entries, min_skips=2) == {"sully"}


def test_skipped_artists_combines_both_histories():
    entries = [
        _entry(artist="Sully", title="T1", outcome="skip", history="weekly", days_ago=2),
        _entry(artist="Sully", title="T2", outcome="skip", history="mix-prep", days_ago=1),
    ]
    assert skipped_artists(entries, min_skips=2) == {"sully"}


def test_skipped_artists_splits_collaborative_artist_string():
    entries = [
        _entry(artist="Bakey, Kasia", title="T1", outcome="skip", days_ago=3),
        _entry(artist="Bakey, Kasia", title="T2", outcome="skip", days_ago=2),
    ]
    result = skipped_artists(entries, min_skips=2)
    assert result == {"bakey", "kasia"}


def test_skipped_artists_split_collaborator_positive_only_disqualifies_that_artist():
    entries = [
        _entry(artist="Bakey, Kasia", title="T1", outcome="skip", days_ago=4),
        _entry(artist="Bakey, Kasia", title="T2", outcome="skip", days_ago=3),
        _entry(artist="Bakey", title="Solo Track", outcome="liked", days_ago=1),
    ]
    result = skipped_artists(entries, min_skips=2)
    assert result == {"kasia"}


def test_skipped_artists_empty_entries_returns_empty_set():
    assert skipped_artists([], min_skips=2) == set()


# ---------------------------------------------------------------------------
# tune_report — feedback-driven per-signal/source/genre report
# ---------------------------------------------------------------------------

def test_tune_report_header_and_baseline():
    w = [
        _weekly_record(artist="A", title="1", track_no=1, source="beatport",
                       signal_codes=["known_artist"], genre_tags=["dnb"]),
        _weekly_record(artist="B", title="2", track_no=2, source="bandcamp",
                       signal_codes=["label_match"], genre_tags=["house"]),
        _weekly_record(artist="C", title="3", track_no=3, source="beatport",
                       signal_codes=["known_artist"], genre_tags=["dnb"]),
    ]
    entries = [
        _entry(artist="A", title="1", outcome="bought"),
        _entry(artist="B", title="2", outcome="skip"),
        _entry(artist="C", title="3", outcome="liked"),
    ]
    out = tune_report(w, [], entries)
    # 3 recommended, 3 marked, baseline positive = 2/3 = 66.7%
    assert "Recommended: 3" in out
    assert "Marked: 3" in out
    assert "Baseline positive rate: 66.7%" in out


def test_tune_report_lift_computation():
    w = [
        _weekly_record(artist="A", title="1", track_no=1, signal_codes=["known_artist"]),
        _weekly_record(artist="B", title="2", track_no=2, signal_codes=["label_match"]),
        _weekly_record(artist="C", title="3", track_no=3, signal_codes=["known_artist"]),
    ]
    entries = [
        _entry(artist="A", title="1", outcome="bought"),
        _entry(artist="B", title="2", outcome="skip"),
        _entry(artist="C", title="3", outcome="liked"),
    ]
    out = tune_report(w, [], entries)
    # known_artist: 2/2 positive = 100% vs baseline 66.7% -> lift 1.50
    assert "known_artist: recommended=2 marked=2 positive=2 rate=100.0% lift=1.50" in out
    # label_match: 0/1 positive = 0% -> lift 0.00
    assert "label_match: recommended=1 marked=1 positive=0 rate=0.0% lift=0.00" in out


def test_tune_report_own_excluded_from_rate():
    w = [
        _weekly_record(artist="A", title="1", track_no=1, source="beatport"),
        _weekly_record(artist="B", title="2", track_no=2, source="beatport"),
    ]
    entries = [
        _entry(artist="A", title="1", outcome="bought"),
        _entry(artist="B", title="2", outcome="own"),
    ]
    out = tune_report(w, [], entries)
    # beatport: marked=2 but non-own denominator is 1 (own excluded) -> rate 100%
    assert "beatport: recommended=2 marked=2 positive=1 rate=100.0% lift=" in out
    # Baseline also excludes own: 1 positive / 1 non-own = 100%
    assert "Baseline positive rate: 100.0%" in out


def test_tune_report_lift_dash_when_unmarked():
    w = [
        _weekly_record(artist="A", title="1", track_no=1, signal_codes=["known_artist"]),
        _weekly_record(artist="B", title="2", track_no=2, signal_codes=["scene_adjacent"]),
    ]
    entries = [_entry(artist="A", title="1", outcome="bought")]
    out = tune_report(w, [], entries)
    # scene_adjacent recommended but never marked -> rate '—', lift '—'
    assert "scene_adjacent: recommended=1 marked=0 positive=0 rate=— lift=—" in out


def _many(n, outcome="bought"):
    records = []
    entries = []
    for i in range(n):
        a, t = f"Artist{i}", f"Track{i}"
        records.append(_weekly_record(artist=a, title=t, track_no=i + 1,
                                      signal_codes=["known_artist"]))
        entries.append(_entry(artist=a, title=t, outcome=outcome))
    return records, entries


def test_tune_report_thin_data_caveat_below_threshold():
    records, entries = _many(_MIN_MARKS_FOR_CONCLUSIONS - 1)
    out = tune_report(records, [], entries)
    assert "anecdote, not evidence" in out
    # table still prints
    assert "By signal:" in out


def test_tune_report_no_caveat_at_threshold():
    records, entries = _many(_MIN_MARKS_FOR_CONCLUSIONS)
    out = tune_report(records, [], entries)
    assert "anecdote, not evidence" not in out


def test_tune_report_latest_mark_wins():
    """A later re-mark must drive the aggregation (reuses latest_marks)."""
    w = [_weekly_record(artist="A", title="1", track_no=1, signal_codes=["known_artist"])]
    entries = [
        _entry(artist="A", title="1", outcome="skip", days_ago=5),
        _entry(artist="A", title="1", outcome="bought", days_ago=0),
    ]
    out = tune_report(w, [], entries)
    assert "known_artist: recommended=1 marked=1 positive=1 rate=100.0%" in out
