"""Tests for BPM/key-aware mix-prep helpers (issue #8, src/pipeline/harmonic.py)."""
import pytest

from src.models import Candidate
from src.pipeline.harmonic import (
    bpm_matches,
    camelot_compatible,
    candidate_bpm,
    candidate_camelot,
    partition_by_harmonic,
    to_camelot,
)


def _candidate(bpm=None, key=None, keysign=None, **kw):
    raw: dict = {}
    if bpm is not None:
        raw["bpm"] = bpm
    if keysign is not None:
        raw["keysign"] = keysign
    if key is not None:
        raw["key"] = key
    return Candidate(artist="A", title="T", link="", source="s", raw_metadata=raw, **kw)


# ---------------------------------------------------------------------------
# to_camelot — full 24-entry table
# ---------------------------------------------------------------------------

# (musical key, expected Camelot code) — one canonical spelling per wheel position.
_MINOR_TABLE = [
    ("Abm", "1A"), ("Ebm", "2A"), ("Bbm", "3A"), ("Fm", "4A"), ("Cm", "5A"),
    ("Gm", "6A"), ("Dm", "7A"), ("Am", "8A"), ("Em", "9A"), ("Bm", "10A"),
    ("F#m", "11A"), ("C#m", "12A"),
]
_MAJOR_TABLE = [
    ("B", "1B"), ("F#", "2B"), ("Db", "3B"), ("Ab", "4B"), ("Eb", "5B"),
    ("Bb", "6B"), ("F", "7B"), ("C", "8B"), ("G", "9B"), ("D", "10B"),
    ("A", "11B"), ("E", "12B"),
]


@pytest.mark.parametrize("musical,camelot", _MINOR_TABLE)
def test_to_camelot_minor_table(musical, camelot):
    assert to_camelot(musical) == camelot


@pytest.mark.parametrize("musical,camelot", _MAJOR_TABLE)
def test_to_camelot_major_table(musical, camelot):
    assert to_camelot(musical) == camelot


def test_to_camelot_all_24_codes_covered():
    codes = {c for _, c in _MINOR_TABLE} | {c for _, c in _MAJOR_TABLE}
    assert len(codes) == 24
    for n in range(1, 13):
        assert f"{n}A" in codes
        assert f"{n}B" in codes


# --- relative major/minor pairs share the same number ---

@pytest.mark.parametrize("minor,major,number", [
    ("Am", "C", 8),
    ("Ebm", "Gb", 2),
    ("F#m", "A", 11),
])
def test_relative_major_minor_share_number(minor, major, number):
    assert to_camelot(minor) == f"{number}A"
    assert to_camelot(major) == f"{number}B"


# --- mode word variants ---

@pytest.mark.parametrize("s", ["Am", "A minor", "A min", "A Minor", "a minor", "  A minor  "])
def test_to_camelot_minor_word_variants(s):
    assert to_camelot(s) == "8A"


@pytest.mark.parametrize("s", ["C", "C major", "C maj", "c major", "  C  major "])
def test_to_camelot_major_word_variants(s):
    assert to_camelot(s) == "8B"


# --- enharmonic equivalents ---

@pytest.mark.parametrize("a,b", [
    ("G#m", "Abm"),
    ("C#", "Db"),
    ("D#m", "Ebm"),
    ("Gb", "F#"),
])
def test_enharmonic_equivalents_map_to_same_code(a, b):
    assert to_camelot(a) == to_camelot(b)
    assert to_camelot(a) is not None


# --- unicode accidentals ---

def test_to_camelot_unicode_sharp():
    assert to_camelot("G♯m") == to_camelot("G#m")


def test_to_camelot_unicode_flat():
    assert to_camelot("A♭ minor") == to_camelot("Abm")
    assert to_camelot("A♭ minor") == "1A"


# --- already-Camelot input ---

@pytest.mark.parametrize("s,expected", [
    ("8A", "8A"), ("8a", "8A"), ("12B", "12B"), ("12b", "12B"), ("1A", "1A"),
])
def test_to_camelot_passthrough(s, expected):
    assert to_camelot(s) == expected


def test_to_camelot_camelot_out_of_range_number_is_none():
    assert to_camelot("13A") is None
    assert to_camelot("0B") is None


# --- junk / unparseable ---

@pytest.mark.parametrize("s", [None, "", "   ", "Xm", "Hm", "Cats", "C##", "banana", "m7#9"])
def test_to_camelot_junk_returns_none(s):
    assert to_camelot(s) is None


# ---------------------------------------------------------------------------
# camelot_compatible
# ---------------------------------------------------------------------------

def test_camelot_compatible_exact_match():
    assert camelot_compatible("8A", "8A") is True


def test_camelot_compatible_adjacent_wheel_position():
    assert camelot_compatible("8A", "9A") is True
    assert camelot_compatible("8A", "7A") is True


def test_camelot_compatible_wraps_12_to_1():
    assert camelot_compatible("12A", "1A") is True
    assert camelot_compatible("1A", "12A") is True
    assert camelot_compatible("12B", "1B") is True


def test_camelot_compatible_relative_major_minor():
    assert camelot_compatible("8A", "8B") is True
    assert camelot_compatible("8B", "8A") is True


def test_camelot_compatible_not_adjacent_or_relative():
    assert camelot_compatible("8A", "10A") is False
    assert camelot_compatible("8A", "3B") is False


def test_camelot_compatible_accepts_musical_notation():
    assert camelot_compatible("Am", "8A") is True
    assert camelot_compatible("Am", "C") is True  # relative major


def test_camelot_compatible_unparseable_is_false():
    assert camelot_compatible("garbage", "8A") is False
    assert camelot_compatible("8A", None) is False
    assert camelot_compatible(None, None) is False


# ---------------------------------------------------------------------------
# bpm_matches
# ---------------------------------------------------------------------------

def test_bpm_matches_in_range():
    assert bpm_matches(175, 170, 180) is True


def test_bpm_matches_out_of_range_no_flex():
    assert bpm_matches(85, 170, 180, flex=False) is False


def test_bpm_matches_half_time_with_flex():
    assert bpm_matches(85, 170, 180, flex=True) is True


def test_bpm_matches_double_time_with_flex():
    assert bpm_matches(350, 170, 180, flex=True) is True


def test_bpm_matches_flex_off_rejects_half_double():
    assert bpm_matches(85, 170, 180, flex=False) is False
    assert bpm_matches(350, 170, 180, flex=False) is False


def test_bpm_matches_none_is_false():
    assert bpm_matches(None, 170, 180) is False


def test_bpm_matches_boundary_inclusive():
    assert bpm_matches(170, 170, 180) is True
    assert bpm_matches(180, 170, 180) is True


# ---------------------------------------------------------------------------
# candidate_bpm / candidate_camelot
# ---------------------------------------------------------------------------

def test_candidate_bpm_str_int_float():
    assert candidate_bpm(_candidate(bpm="174")) == 174.0
    assert candidate_bpm(_candidate(bpm=174)) == 174.0
    assert candidate_bpm(_candidate(bpm=174.0)) == 174.0


def test_candidate_bpm_missing_is_none():
    assert candidate_bpm(_candidate()) is None


def test_candidate_bpm_junk_is_none():
    assert candidate_bpm(_candidate(bpm="fast")) is None


def test_candidate_camelot_prefers_keysign_over_key():
    c = _candidate(keysign="A minor", key="C major")
    assert candidate_camelot(c) == "8A"


def test_candidate_camelot_falls_back_to_key():
    c = _candidate(key="Cm")
    assert candidate_camelot(c) == "5A"


def test_candidate_camelot_missing_is_none():
    assert candidate_camelot(_candidate()) is None


def test_candidate_camelot_junk_is_none():
    assert candidate_camelot(_candidate(keysign="nonsense")) is None


# ---------------------------------------------------------------------------
# partition_by_harmonic
# ---------------------------------------------------------------------------

def test_partition_no_filters_everything_matches():
    cands = [_candidate(bpm=174, keysign="Am"), _candidate()]
    matches, unknowns = partition_by_harmonic(cands, None, None)
    assert matches == cands
    assert unknowns == []


def test_partition_bpm_known_pass_is_match():
    c = _candidate(bpm=175)
    matches, unknowns = partition_by_harmonic([c], (170, 180), None)
    assert matches == [c]
    assert unknowns == []


def test_partition_bpm_known_fail_is_dropped():
    c = _candidate(bpm=100)
    matches, unknowns = partition_by_harmonic([c], (170, 180), None, flex=False)
    assert matches == []
    assert unknowns == []


def test_partition_bpm_unknown_is_demoted_not_dropped():
    c = _candidate()  # no bpm
    matches, unknowns = partition_by_harmonic([c], (170, 180), None)
    assert matches == []
    assert unknowns == [c]


def test_partition_key_known_pass_is_match():
    c = _candidate(keysign="Am")
    matches, unknowns = partition_by_harmonic([c], None, "8A")
    assert matches == [c]
    assert unknowns == []


def test_partition_key_known_fail_is_dropped():
    c = _candidate(keysign="Am")  # 8A, incompatible with 3B
    matches, unknowns = partition_by_harmonic([c], None, "3B")
    assert matches == []
    assert unknowns == []


def test_partition_key_unknown_is_demoted_not_dropped():
    c = _candidate()  # no key
    matches, unknowns = partition_by_harmonic([c], None, "8A")
    assert matches == []
    assert unknowns == [c]


def test_partition_both_filters_must_pass_both():
    good = _candidate(bpm=175, keysign="Am")
    bad_bpm = _candidate(bpm=100, keysign="Am")
    bad_key = _candidate(bpm=175, keysign="C#")  # 3B, incompatible with 8A
    matches, unknowns = partition_by_harmonic(
        [good, bad_bpm, bad_key], (170, 180), "8A", flex=False,
    )
    assert matches == [good]
    assert unknowns == []


def test_partition_unknown_on_either_filter_demotes():
    unknown_bpm = _candidate(keysign="Am")       # bpm missing, key passes
    unknown_key = _candidate(bpm=175)            # key missing, bpm passes
    matches, unknowns = partition_by_harmonic(
        [unknown_bpm, unknown_key], (170, 180), "8A",
    )
    assert matches == []
    assert set(id(c) for c in unknowns) == {id(unknown_bpm), id(unknown_key)}


def test_partition_known_failure_drops_even_if_other_filter_unknown():
    # BPM unknown (would demote), but key known and fails -> dropped, not demoted.
    c = _candidate(keysign="C#")  # 3B, incompatible with 8A
    matches, unknowns = partition_by_harmonic([c], (170, 180), "8A")
    assert matches == []
    assert unknowns == []


def test_partition_preserves_relative_order_within_each_list():
    a = _candidate(bpm=171)
    b = _candidate(bpm=179)
    c = _candidate()  # unknown
    d = _candidate()  # unknown
    matches, unknowns = partition_by_harmonic([a, c, b, d], (170, 180), None)
    assert matches == [a, b]
    assert unknowns == [c, d]
