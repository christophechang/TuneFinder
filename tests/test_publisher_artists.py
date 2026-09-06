"""Tests for the publish-pool artist index (src/publisher/artists.py).

The index is the publisher's own store — thirteen weekly buckets per family,
kept because pool documents expire after 45 days and the counts must not. The
shape is `index[family][week][artist] = {"keys": [...], "labels": [...]}`, and
`artists_payloads` turns a window of it into the payload
`samples/artists.json` shows.
"""
import json
import os
from datetime import date

import pytest

from src.publisher.artists import (
    ARTIST_INDEX_FILE,
    MAX_ARTISTS,
    artists_payloads,
    load_artist_index,
    prune_artist_index,
    save_artist_index,
    split_artists,
    update_artist_index,
    week_monday,
)
from src.publisher.contract import CONTRACT_DIR, validate

RUN_ID = "2026-09-06T06:00:00Z-a3f9c1"


def _sample(name):
    with open(os.path.join(CONTRACT_DIR, "samples", f"{name}.json")) as f:
        return json.load(f)


def _built(key_v2, artist, label, families=("house",)):
    """The subset of a built contract item update_artist_index reads."""
    return {
        "key_v2": key_v2,
        "artist": artist,
        "label": label,
        "families": list(families),
    }


def test_week_monday():
    assert week_monday(date(2026, 8, 31)) == date(2026, 8, 31)   # a Monday
    assert week_monday(date(2026, 9, 6)) == date(2026, 8, 31)    # the Sunday after
    assert week_monday(date(2026, 9, 1)) == date(2026, 8, 31)
    assert week_monday(date(2026, 9, 7)) == date(2026, 9, 7)


@pytest.mark.parametrize(
    "artist,expected",
    [
        ("Borai & Denham Audio", ["Borai", "Denham Audio"]),
        ("Sustance, Flowdan", ["Sustance", "Flowdan"]),
        ("Zero T feat. Steo", ["Zero T"]),
        ("Zero T ft. Steo", ["Zero T"]),
        ("Skee Mask", ["Skee Mask"]),
        ("Overmono x Joy Orbison", ["Overmono", "Joy Orbison"]),
        ("Bicep vs. Chase & Status", ["Bicep", "Chase", "Status"]),
        ("Sully / Pearson Sound", ["Sully", "Pearson Sound"]),
        ("  Prunk  ", ["Prunk"]),
        ("", []),
        ("Max Cooper", ["Max Cooper"]),          # " x " needs its spaces
        ("Prunk & Prunk", ["Prunk"]),
    ],
)
def test_split_artists(artist, expected):
    assert split_artists(artist) == expected


def test_update_counts_distinct_keys_per_week():
    index = {}
    update_artist_index(
        index,
        [
            _built("prunk||get down", "Prunk", "PIV"),
            _built("prunk||get down", "Prunk", "PIV"),
            _built("prunk||no sleep", "Prunk", "Realm"),
        ],
        date(2026, 9, 2),
    )

    entry = index["house"]["2026-08-31"]["Prunk"]
    assert entry["keys"] == ["prunk||get down", "prunk||no sleep"]
    assert entry["labels"] == ["PIV", "Realm"]


def test_update_counts_each_artist_of_a_collaboration_under_every_family():
    index = {}
    update_artist_index(
        index,
        [
            _built(
                "borai, denham audio||make me",
                "Borai & Denham Audio",
                "Columbia",
                families=("house", "uk-garage"),
            )
        ],
        date(2026, 9, 6),
    )

    assert sorted(index) == ["house", "uk-garage"]
    for family in index:
        assert sorted(index[family]["2026-08-31"]) == ["Borai", "Denham Audio"]


def test_prune_keeps_13_weeks():
    today = date(2026, 9, 6)
    this_week = week_monday(today).isoformat()          # 2026-08-31
    twelve_back = "2026-06-08"
    thirteen_back = "2026-06-01"
    index = {
        "house": {
            this_week: {"Prunk": {"keys": ["prunk||get down"], "labels": ["PIV"]}},
            twelve_back: {"Prunk": {"keys": ["prunk||old"], "labels": []}},
            thirteen_back: {"Prunk": {"keys": ["prunk||older"], "labels": []}},
        },
        "techno": {
            thirteen_back: {"Blawan": {"keys": ["blawan||gone"], "labels": []}},
        },
    }

    pruned = prune_artist_index(index, 13, today)

    assert sorted(pruned["house"]) == [twelve_back, this_week]
    assert "techno" not in pruned


def test_payloads_match_sample_shape():
    index = {}
    # Week of 2026-08-24: Prunk twice, Franky Rizardo once on two labels.
    update_artist_index(
        index,
        [
            _built("prunk||get down", "Prunk", "PIV"),
            _built("prunk||no sleep", "Prunk", "PIV"),
            _built("franky rizardo||lost", "Franky Rizardo", "Columbia"),
            _built("franky rizardo||lost", "Franky Rizardo", "Realm"),
        ],
        date(2026, 8, 26),
    )
    # Week of 2026-08-31: Prunk three times, Sofia Kourtesis once.
    update_artist_index(
        index,
        [
            _built("prunk||get down", "Prunk", "PIV"),
            _built("prunk||no sleep", "Prunk", "PIV"),
            _built("prunk||bounce", "Prunk", "PIV"),
            _built("sofia kourtesis||estación esperanza", "Sofia Kourtesis", "Ninja Tune"),
        ],
        date(2026, 9, 4),
    )

    payloads = artists_payloads(index, RUN_ID, 1, weeks=2, today=date(2026, 9, 6))

    assert payloads == [_sample("artists")]
    validate("artists", payloads[0])


def test_payload_weeks_are_mondays_ascending():
    index = {}
    update_artist_index(index, [_built("prunk||get down", "Prunk", "PIV")], date(2026, 9, 2))

    payload = artists_payloads(index, RUN_ID, 1, weeks=13, today=date(2026, 9, 2))[0]

    weeks = [date.fromisoformat(week) for week in payload["weeks"]]
    assert len(weeks) == 13
    assert weeks == sorted(weeks)
    assert all(week.weekday() == 0 for week in weeks)
    assert weeks[-1] == week_monday(date(2026, 9, 2))
    assert payload["artists"][0]["counts"] == [0] * 12 + [1]
    validate("artists", payload)


def test_cap_5000_by_total_count():
    week = week_monday(date(2026, 9, 6)).isoformat()
    bucket = {}
    for number in range(5100):
        keys = ["a", "b"] if number < MAX_ARTISTS else ["a"]
        bucket[f"Artist {number:04d}"] = {"keys": keys, "labels": []}
    index = {"house": {week: bucket}}

    payload = artists_payloads(index, RUN_ID, 1, weeks=1, today=date(2026, 9, 6))[0]

    assert MAX_ARTISTS == 5000
    assert len(payload["artists"]) == 5000
    assert all(artist["counts"] == [2] for artist in payload["artists"])
    assert payload["artists"][0]["name"] == "Artist 0000"
    validate("artists", payload)


def test_family_without_artists_omitted():
    index = {
        "house": {"2026-08-31": {"Prunk": {"keys": ["prunk||get down"], "labels": ["PIV"]}}},
        "techno": {"2026-01-05": {"Blawan": {"keys": ["blawan||gone"], "labels": []}}},
        "electro": {},
    }

    payloads = artists_payloads(index, RUN_ID, 1, weeks=2, today=date(2026, 9, 6))

    assert [payload["family"] for payload in payloads] == ["house"]


def test_index_roundtrip_atomic(tmp_path):
    pool_dir = str(tmp_path)
    assert load_artist_index(pool_dir) == {}

    index = {}
    update_artist_index(index, [_built("prunk||get down", "Prunk", "PIV")], date(2026, 9, 2))
    save_artist_index(index, pool_dir)

    assert load_artist_index(pool_dir) == index
    assert os.listdir(pool_dir) == [ARTIST_INDEX_FILE]
    assert not [name for name in os.listdir(pool_dir) if name.startswith(".tmp-")]
