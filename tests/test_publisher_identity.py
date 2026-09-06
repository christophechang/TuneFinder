"""CONTRACTS §1 identity keys — the publisher's two keys per track.

The 70-row `tests/fixtures/identity/cases.json` is the cross-runtime oracle for
spike S1b (a copy of `tests/identity/cases.json` in the tunefinder-multi-tenant
repository); the TypeScript parser and the .NET engine must reproduce the same
three key columns from the same rows.
"""
import json
from pathlib import Path

import pytest

from src.models import SourceItem
from src.pipeline.dedup import make_dedup_key
from src.publisher import PUBLISHER_VERSION
from src.publisher.identity import (
    IDENTITY_VERSION,
    classify_version,
    identity_keys,
    is_domain_like,
    item_identity,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "identity" / "cases.json"
_FIXTURE_DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES = _FIXTURE_DATA["cases"]
_CASE_IDS = [f"{c['Artist']} - {c['Name']}" for c in _CASES]


def _item(source, artist="Borai & Denham Audio", title="Make Me", raw_metadata=None):
    return SourceItem(
        source=source,
        artist=artist,
        title=title,
        link=f"https://{source}.example.com/track",
        raw_metadata=raw_metadata or {},
    )


# ---------------------------------------------------------------------------
# The shared fixture — every row, every column
# ---------------------------------------------------------------------------

def test_fixture_is_the_shared_70_row_set():
    assert len(_CASES) == 70
    assert _FIXTURE_DATA["identity_version"] == IDENTITY_VERSION


@pytest.mark.parametrize("row", _CASES, ids=_CASE_IDS)
def test_cases_json_key_v1_matches(row):
    key_v1, _ = identity_keys(row["Artist"], row["Name"], row["Remixer"] or None,
                              version_is_catalogue=False)
    assert key_v1 == row["key_v1"]


@pytest.mark.parametrize("row", _CASES, ids=_CASE_IDS)
def test_cases_json_key_v2_title_only_matches(row):
    assert make_dedup_key(row["Artist"], row["Name"], remix_aware=True) == row["key_v2_title_only"]


@pytest.mark.parametrize("row", _CASES, ids=_CASE_IDS)
def test_cases_json_key_v2_with_remixer_fallback_matches(row):
    _, key_v2 = identity_keys(row["Artist"], row["Name"], row["Remixer"] or None,
                              version_is_catalogue=False)
    assert key_v2 == row["key_v2"]


# ---------------------------------------------------------------------------
# Catalogue version fields (Beatport mix_name, Volumo version) — the field wins
# ---------------------------------------------------------------------------

def test_beatport_mix_name_wins_over_title():
    assert identity_keys("Borai & Denham Audio", "Make Me (Radio Edit)",
                         "Franky Rizardo Extended Remix") == (
        "borai, denham audio||make me",
        "borai, denham audio||make me||rmx:franky rizardo",
    )


@pytest.mark.parametrize("mix_name", ["Original Mix", "Extended Mix", "Dub", "Radio Edit"])
def test_beatport_generic_mix_name_no_qualifier(mix_name):
    key_v1, key_v2 = identity_keys("Calibre", "New Dawn", mix_name)
    assert key_v1 == "calibre||new dawn"
    assert key_v2 == key_v1


def test_beatport_vip_mix_name():
    assert identity_keys("Calibre", "New Dawn", "VIP") == (
        "calibre||new dawn", "calibre||new dawn||rmx:vip")


def test_beatport_missing_mix_name_falls_back_to_the_title():
    # A catalogue source with no version field is exactly the title rule.
    assert identity_keys("Calibre", "New Dawn (Break Remix)", None) == (
        "calibre||new dawn", "calibre||new dawn||rmx:break")


# `_VERSION_RE` (frozen legacy) has no vip/flip/refix/remake alternative, so
# `normalise_title` leaves those tags in the base. `key_v2` must not inherit that:
# the same track from a catalogue source and from a title-only source has to land on
# one pool document id.
_UNSTRIPPED_NAMED_TITLES = [
    ("Track (Calibre VIP)", "Calibre VIP"),
    ("Track (Calibre Flip)", "Calibre Flip"),
    ("Track (Calibre Refix)", "Calibre Refix"),
    ("Track (Calibre Remake)", "Calibre Remake"),
]


@pytest.mark.parametrize("title,version", _UNSTRIPPED_NAMED_TITLES)
def test_catalogue_and_title_paths_share_the_key_v2_base(title, version):
    catalogue = identity_keys("A", title, version)
    from_title = identity_keys("A", title, None, version_is_catalogue=False)
    assert catalogue[1] == from_title[1] == "a||track||rmx:calibre"
    # The title path stays byte-identical to the Sunday run's remix-aware key.
    assert from_title[1] == make_dedup_key("A", title, remix_aware=True)
    # key_v1 is the legacy key and does not move.
    assert catalogue[0] == from_title[0] == f"a||{title.lower()}"


def test_catalogue_generic_field_still_excises_the_named_tag():
    # The field wins (no qualifier), and the title's named tag leaves the base anyway.
    assert identity_keys("A", "Track (Calibre VIP)", "Original Mix") == (
        "a||track (calibre vip)", "a||track")


def test_catalogue_field_wins_over_a_disagreeing_named_tag():
    assert identity_keys("A", "Track (Calibre VIP)", "Break Remix") == (
        "a||track (calibre vip)", "a||track||rmx:break")


def test_volumo_version_field_is_catalogue():
    # The catalogue's own field disagrees with the title parenthetical: the field wins
    # and the parenthetical is stripped from the base.
    item = _item("volumo", artist="Zero T", title="Refusal (Original Mix)",
                 raw_metadata={"version": "Calibre Remix"})
    key_v1, key_v2, version, granularity = item_identity(item)
    assert (key_v1, key_v2) == ("zero t||refusal", "zero t||refusal||rmx:calibre")
    assert version == "Calibre Remix"
    assert granularity == "track"


# ---------------------------------------------------------------------------
# The Rekordbox `Remixer` fallback — guarded, title-first
# ---------------------------------------------------------------------------

def test_remixer_fallback_only_when_title_has_no_named_version():
    # No named parenthetical → the hand-typed field is used.
    assert identity_keys("Zero T", "Refusal ft. Steo", "Calibre Remix",
                         version_is_catalogue=False)[1] == "zero t||refusal||rmx:calibre"
    # The title carries a named version → it wins, the disagreeing field is ignored.
    assert identity_keys("Borai & Denham Audio", "Make Me (Mani Festo Remix)", "Franky Rizardo",
                         version_is_catalogue=False)[1] == "borai, denham audio||make me||rmx:mani festo"


@pytest.mark.parametrize("remixer", [
    "Original Mix",
    "dbox.pro",
    "www.electronicfresh.com",
    "freednb.com / musikmp3.ucoz.com",
    "Extended Remix",
    "Extended Vocal",
])
def test_remixer_fallback_skips_generic_and_domain(remixer):
    key_v1, key_v2 = identity_keys("Zero T", "Refusal", remixer, version_is_catalogue=False)
    assert key_v2 == key_v1 == "zero t||refusal"


@pytest.mark.parametrize("text,expected", [
    ("dbox.pro", True),
    ("www.electronicfresh.com", True),
    ("freednb.com / musikmp3.ucoz.com", True),
    ("http://example", True),
    ("Calibre", False),
    ("Mani Festo", False),
    ("&ME; Rampa", False),
])
def test_is_domain_like(text, expected):
    assert is_domain_like(text) is expected


def test_remixer_without_keyword_is_a_name():
    # "Calibre Remix" and a bare "Calibre" are the same identity.
    with_keyword = identity_keys("Zero T", "Refusal", "Calibre Remix", version_is_catalogue=False)
    bare_name = identity_keys("Zero T", "Refusal", "Calibre", version_is_catalogue=False)
    assert with_keyword == bare_name == ("zero t||refusal", "zero t||refusal||rmx:calibre")


def test_classify_version_lowercases_and_collapses():
    assert classify_version("  Calibre   Remix ") == "rmx:calibre"
    assert classify_version("Original Mix") is None
    assert classify_version("") is None
    assert classify_version(None) is None


# ---------------------------------------------------------------------------
# item_identity — per-source version field and granularity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,expected", [
    ("beatport", "track"),
    ("volumo", "track"),
    ("soundcloud", "track"),
    ("bandcamp", "release"),
    ("traxsource", "track"),
])
def test_item_identity_granularity_per_source(source, expected):
    assert item_identity(_item(source))[3] == expected


def test_item_identity_version_text():
    # Beatport: the catalogue field, verbatim, even when it is generic.
    assert item_identity(_item("beatport", raw_metadata={"mix_name": "Extended Mix"}))[2] == "Extended Mix"
    # SoundCloud: the inner text of the title's last named parenthetical, original casing.
    assert item_identity(_item("soundcloud", title="Make Me (Mani Festo Remix)"))[2] == "Mani Festo Remix"
    # A generic parenthetical is not a version.
    assert item_identity(_item("soundcloud", title="Make Me (Original Mix)"))[2] is None
    # Nothing to report.
    assert item_identity(_item("soundcloud"))[2] is None
    assert item_identity(_item("beatport", raw_metadata={"mix_name": None}))[2] is None


def test_item_identity_matches_identity_keys_for_a_bandcamp_release():
    item = _item("bandcamp", artist="Sully", title="Blue (Om Unit Remix)")
    assert item_identity(item)[:2] == identity_keys("Sully", "Blue (Om Unit Remix)",
                                                    version_is_catalogue=False)


# ---------------------------------------------------------------------------
# Wire-visible version constants
# ---------------------------------------------------------------------------

def test_identity_version_is_2():
    assert IDENTITY_VERSION == 2


def test_publisher_version_constant():
    assert PUBLISHER_VERSION == "1"
