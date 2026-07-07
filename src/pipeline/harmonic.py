"""
BPM/key-aware harmonic mixing helpers (issue #8).

Pure functions only — no IO, no network. Normalises the musical-key notations
the fetchers actually emit (Volumo `keysign` e.g. "C major", "A♭ minor";
Mixupload `key` e.g. "Cm") to Camelot Wheel codes, and provides the
BPM/key compatibility checks `mix-prep --bpm` / `--key` are built on.

Camelot wheel reference (Mixed In Key notation): 12 wheel positions, each
with an "A" (minor) and "B" (major) key sharing the same number when they are
relative major/minor pairs (e.g. 8A = A minor, 8B = C major).
"""
import re

from src.models import Candidate

# ---------------------------------------------------------------------------
# Camelot table
# ---------------------------------------------------------------------------

# Pitch class (0=C .. 11=B) for the seven natural note letters.
_NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# pitch class -> Camelot code, one table per mode. Built from the standard
# Camelot wheel: relative major/minor pairs share the same number.
_MAJOR_BY_PC = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
_MINOR_BY_PC = {
    0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
    6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A",
}

_MINOR_MODE_WORDS = {"m", "min", "minor"}
_MAJOR_MODE_WORDS = {"", "maj", "major"}

_UNICODE_ACCIDENTALS = {"♯": "#", "♭": "b"}

# Already-Camelot input, e.g. "8A", "12b" (case-insensitive letter, optional space).
_CAMELOT_RE = re.compile(r"(\d{1,2})\s*([AaBb])")
# Musical notation: note letter, optional accidental, then a mode word (or nothing = major).
_NOTE_RE = re.compile(r"([A-Ga-g])([#b]?)(.*)")


def to_camelot(key_str: str | None) -> str | None:
    """Normalise a musical key string (or an already-Camelot code) to Camelot
    notation, e.g. "Am" / "A minor" / "A min" -> "8A"; "C" / "C major" -> "8B";
    "Abm" / "G#m" -> "1A" (enharmonic equivalents map to the same code);
    "8a" -> "8A" (already-Camelot input, case/space tolerant).

    Handles unicode ♯/♭ accidentals, case and whitespace variance. Returns
    None for anything that doesn't parse (junk input, unknown note letters,
    unrecognised mode words) — never raises.
    """
    if not key_str:
        return None
    s = str(key_str).strip()
    if not s:
        return None
    for uni, ascii_eq in _UNICODE_ACCIDENTALS.items():
        s = s.replace(uni, ascii_eq)

    camelot_match = _CAMELOT_RE.fullmatch(s)
    if camelot_match:
        num = int(camelot_match.group(1))
        if 1 <= num <= 12:
            return f"{num}{camelot_match.group(2).upper()}"
        return None

    note_match = _NOTE_RE.fullmatch(s)
    if not note_match:
        return None
    letter, accidental, remainder = note_match.groups()
    letter = letter.upper()
    mode_text = remainder.strip().lower()

    pc = _NOTE_PC[letter]
    if accidental == "#":
        pc = (pc + 1) % 12
    elif accidental == "b":
        pc = (pc - 1) % 12

    if mode_text in _MINOR_MODE_WORDS:
        return _MINOR_BY_PC[pc]
    if mode_text in _MAJOR_MODE_WORDS:
        return _MAJOR_BY_PC[pc]
    return None


def camelot_compatible(a: str, b: str) -> bool:
    """True if Camelot codes (or raw musical/Camelot strings — both are run
    through `to_camelot` first) are harmonically compatible for mixing:
    exact match, adjacent wheel position (±1, wrapping 12<->1), or the same
    number's relative major/minor switch. Unparseable input -> False.
    """
    ca = to_camelot(a)
    cb = to_camelot(b)
    if ca is None or cb is None:
        return False
    if ca == cb:
        return True

    num_a, letter_a = int(ca[:-1]), ca[-1]
    num_b, letter_b = int(cb[:-1]), cb[-1]

    if letter_a == letter_b:
        diff = abs(num_a - num_b) % 12
        if diff in (1, 11):  # 11 covers the 12<->1 wrap
            return True

    if num_a == num_b and letter_a != letter_b:
        return True

    return False


def bpm_matches(bpm: float | int, lo: float, hi: float, flex: bool = True) -> bool:
    """True if bpm falls in [lo, hi], or — when flex is on — its half-time or
    double-time value does (e.g. 85 matches a 170-180 range)."""
    if bpm is None:
        return False
    try:
        bpm = float(bpm)
    except (TypeError, ValueError):
        return False
    if lo <= bpm <= hi:
        return True
    if flex:
        if lo <= bpm * 2 <= hi:
            return True
        if lo <= bpm / 2 <= hi:
            return True
    return False


def candidate_bpm(c: Candidate) -> float | None:
    """Read BPM from raw_metadata["bpm"], tolerant of str/int/float and junk."""
    raw = c.raw_metadata.get("bpm")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def candidate_camelot(c: Candidate) -> str | None:
    """Read a key from raw_metadata (Volumo: "keysign"; Mixupload: "key") and
    normalise it to Camelot notation. None if absent or unparseable."""
    raw = c.raw_metadata.get("keysign") or c.raw_metadata.get("key")
    if not raw:
        return None
    return to_camelot(str(raw))


def partition_by_harmonic(
    candidates: list[Candidate],
    bpm_range: tuple[float, float] | None,
    key: str | None,
    flex: bool = True,
) -> tuple[list[Candidate], list[Candidate]]:
    """Split candidates into (matches, unknowns) for the active BPM/key
    filter(s); candidates that fail a KNOWN value against a specified filter
    are dropped entirely (present in neither list).

    Semantics per candidate, for each filter that's specified:
      - value unknown (missing/unparseable)  -> candidate goes to `unknowns`
        (kept, demoted), unless another specified filter drops it outright.
      - value known and fails the filter     -> dropped (neither list).
      - value known and passes the filter    -> no effect on this candidate's
        fate from this filter.
    A candidate lands in `matches` only if every specified filter it has a
    known value for passes, and none are unknown. It lands in `unknowns` if
    it isn't dropped but at least one specified filter's value is unknown.

    bpm_range=None and key=None -> everything is a match, nothing dropped or
    demoted (no filters active — zero behaviour change for callers).
    """
    if bpm_range is None and key is None:
        return list(candidates), []

    lo, hi = bpm_range if bpm_range is not None else (None, None)
    matches: list[Candidate] = []
    unknowns: list[Candidate] = []

    for c in candidates:
        dropped = False
        unknown = False

        if bpm_range is not None:
            bpm = candidate_bpm(c)
            if bpm is None:
                unknown = True
            elif not bpm_matches(bpm, lo, hi, flex):
                dropped = True

        if key is not None and not dropped:
            camelot = candidate_camelot(c)
            if camelot is None:
                unknown = True
            elif not camelot_compatible(camelot, key):
                dropped = True

        if dropped:
            continue
        if unknown:
            unknowns.append(c)
        else:
            matches.append(c)

    return matches, unknowns
