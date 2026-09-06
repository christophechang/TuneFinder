"""Tests for the publish-pool payload builder (src/publisher/payload.py).

The headline test is `test_sample_batch_reproduced_from_source_items`: the three
items of the vendored `samples/batch.json` are rebuilt from hand-written
SourceItems, field for field. Everything else exercises one rule of CONTRACTS §7
— the identity keys, the family/fine-genre relation, one source ref per source,
the observation's charting member, the preview precedence, and the coercions
(`release_date`, `bpm`, `camelot`) that turn a fetcher's raw string into
something the schema accepts or into null.
"""
import copy
import json
import os
from datetime import date

import pytest

from src.models import SourceItem
from src.publisher import PUBLISHER_VERSION
from src.publisher.contract import CONTRACT_DIR, check_item_relations, validate
from src.publisher.identity import IDENTITY_VERSION
from src.publisher.payload import (
    KNOWN_SOURCES,
    PUBLISHER_BACKFILL_KEYS,
    SCHEMA_VERSION,
    artwork_url_for,
    batch_payload,
    batches,
    bpm_or_none,
    build_items,
    camelot_or_none,
    group_by_identity,
    manifest_payload,
    merge_facts,
    per_source_report,
    preview_for,
    release_date_or_none,
    source_ref,
    summarise_error,
)
from src.publisher.taxonomy import load_taxonomy

RUN_ID = "2026-09-06T06:00:00Z-a3f9c1"
OBSERVED_ON = date(2026, 9, 6)
SEEN_AT = "2026-09-06T06:12:03Z"

BEATPORT_LINK = (
    "https://www.beatport.com/track/make-me-franky-rizardo-extended-remix/19283746"
)
BANDCAMP_LINK = "https://hooversound.bandcamp.com/album/muscle-memory-ep"
VOLUMO_LINK = "https://volumo.com/track/c7f0a1e2"


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


def _sample(name):
    with open(os.path.join(CONTRACT_DIR, "samples", f"{name}.json")) as f:
        return json.load(f)


def _item(source, artist="A", title="T", link="https://example.test/1", **kw):
    return SourceItem(
        source=source,
        artist=artist,
        title=title,
        link=link,
        label=kw.pop("label", None),
        release_date=kw.pop("release_date", None),
        release_name=kw.pop("release_name", None),
        genre_tags=kw.pop("genre_tags", []),
        raw_metadata=kw.pop("raw_metadata", {}),
    )


# ---------------------------------------------------------------------------
# The sample batch, rebuilt from SourceItems
# ---------------------------------------------------------------------------

def _beatport_members():
    """Two chart rows for the same Beatport track.

    The sample item carries two fine genres in two families, which one chart row
    cannot produce: a Beatport row resolves to exactly one genre slug. So the
    track is seen on the Tech House chart and on the UK Garage / Bassline chart,
    both rows carrying the same `beatport_id` and link — `sources` collapses to
    one ref and the fine-genre union is real. Only the second row charts.
    """
    def row(genre_slug, chart_position):
        return _item(
            "beatport",
            artist="Borai & Denham Audio",
            title="Make Me",
            link=BEATPORT_LINK,
            label="Columbia",
            release_date="2026-08-28",
            release_name="Make Me (Franky Rizardo Remix)",
            raw_metadata={
                "beatport_id": 19283746,
                "genre_slug": genre_slug,
                "mix_name": "Franky Rizardo Extended Remix",
                "bpm": 130,
                "isrc": "GBARL2600123",
                "catalog_number": "COL0026123",
                "artwork_url": "https://geo-media.beatport.com/image_size/500x500/19283746.jpg",
                "sample_url": "https://geo-samples.beatport.com/track/19283746.LOFI.mp3",
                "chart_position": chart_position,
            },
        )

    return [row("tech-house", None), row("uk-garage-bassline", 7)]


def _bandcamp_member():
    return _item(
        "bandcamp",
        artist="Drumskull",
        title="Muscle Memory EP",
        link=BANDCAMP_LINK,
        label="Hooversound Recordings",
        release_date="2026-09-04",
        release_name="Muscle Memory EP",
        raw_metadata={
            "bandcamp_album_id": 3186429057,
            "item_image_id": 3186429057,
            "bandcamp_tag": "breakbeat",
            "item_type": "album",
            "catalog_number": "HVSD012",
        },
    )


def _volumo_member():
    return _item(
        "volumo",
        artist="Prunk",
        title="Get Down",
        link=VOLUMO_LINK,
        label="PIV",
        release_date="2026-08-21",
        release_name="Get Down",
        raw_metadata={
            "volumo_track_id": "c7f0a1e2",
            "volumo_album_id": "a1b2c3d4",
            "volumo_genre_id": 21,
            "version": "Original Mix",
            "bpm": 126,
            "keysign": "A Minor",
            "isrc": "NLZ542600087",
            "catalog_number": "PIV073",
            "artwork_uuid": "c7f0a1e2",
            "chart_position": 12,
        },
    )


def _sample_source_items():
    return [*_beatport_members(), _bandcamp_member(), _volumo_member()]


def _expected_sample_items():
    """samples/batch.json's items, with the values the builder cannot mint.

    Four are the run's own clock, one illustrative preview url and one
    SoundCloud-only flag (controller ruling R3); the Volumo artwork url is a
    sixth (ruling R7 — Volumo exposes an `artwork_uuid`, not a url, and the
    sample's `cdn.volumo.com` value is illustrative too). The last is the
    Bandcamp item's `preview`, which the sample writes as null while giving the
    same item a `bandcamp_album_id` in `sources[0].id` — and an album id is
    exactly what makes a `bandcamp_embed` preview. The two cannot both hold, so
    the built preview is asserted here in full instead.

    Every one of them is written out rather than skipped, so the test still
    asserts the whole item.
    """
    expected = copy.deepcopy(_sample("batch")["items"])
    beatport, bandcamp, volumo = expected

    # R3: the test fixes seen_at and checked_at; the sample stamps a different
    # wall-clock time per item and build_items is given one for the whole run.
    for item in expected:
        item["observation"]["seen_at"] = SEEN_AT
    beatport["preview"]["checked_at"] = SEEN_AT

    # R3: the sample's cdn.volumo.com preview ref is illustrative; the builder
    # emits the prelisten endpoint the SPA calls.
    volumo["preview"]["ref"] = "https://volumo.com/api/v1/tracks/c7f0a1e2/prelisten.mp3"
    volumo["preview"]["checked_at"] = SEEN_AT

    # R7: Volumo's API exposes an artwork uuid, so the builder fills in the
    # image template the site serves; the sample's cdn url is illustrative.
    volumo["artwork_url"] = "https://volumo.com/img/size/500x0/c7f0a1e2.jpg"

    # R3: free_download is a SoundCloud fact, so it is null for a Bandcamp item.
    bandcamp["free_download"] = None
    # The forced fifth, explained above.
    bandcamp["preview"] = {
        "kind": "bandcamp_embed",
        "ref": "3186429057",
        "eligible": True,
        "checked_at": SEEN_AT,
    }
    return expected


def test_sample_batch_reproduced_from_source_items(taxonomy):
    built = build_items(
        _sample_source_items(), taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    assert built.skipped == []
    assert built.items == _expected_sample_items()


def test_built_batch_validates_against_schema_and_relations(taxonomy):
    built = build_items(
        _sample_source_items(), taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )
    payload = batch_payload(RUN_ID, 1, built.items, taxonomy.version)

    validate("batch", payload)
    for item in payload["items"]:
        assert check_item_relations(item) is None


# ---------------------------------------------------------------------------
# Grouping, merging and source refs
# ---------------------------------------------------------------------------

def _rich_soundcloud():
    return _item(
        "soundcloud",
        artist="Sully",
        title="Swandive",
        link="https://soundcloud.com/sully/swandive",
        label="Astrophonica",
        release_date="2026-08-14",
        release_name="Swandive",
        genre_tags=["breaks-breakbeat"],
        raw_metadata={
            "soundcloud_id": 998877,
            "download_count": 42,
            "reposts_count": 7,
            "free_download": True,
            "acquisition_url": "https://sully.bandcamp.com/track/swandive",
            "bpm": 168,
            "key": "A Minor",
        },
    )


def _poor_beatport():
    return _item(
        "beatport",
        artist="Sully",
        title="Swandive",
        link="https://www.beatport.com/track/swandive/555",
        raw_metadata={"beatport_id": 555, "genre_slug": "breaks-breakbeat-uk-bass"},
    )


def test_group_by_identity_groups_on_key_v2_and_keeps_insertion_order():
    items = [_poor_beatport(), _volumo_member(), _rich_soundcloud()]
    groups = group_by_identity(items)

    assert list(groups) == ["sully||swandive", "prunk||get down"]
    assert [member.source for member in groups["sully||swandive"]] == [
        "beatport",
        "soundcloud",
    ]


def test_merge_facts_backfills_publisher_keys_without_mutating_the_fetchers_items():
    beatport = _poor_beatport()
    beatport.raw_metadata["sample_url"] = "https://geo-samples.beatport.com/track/555.LOFI.mp3"
    soundcloud = _rich_soundcloud()

    merged = merge_facts([beatport, soundcloud])

    assert merged.source == "soundcloud"
    # `sample_url` is one of the keys the publisher adds to dedup.py's tuple.
    assert "sample_url" in PUBLISHER_BACKFILL_KEYS
    assert merged.raw_metadata["sample_url"].endswith("555.LOFI.mp3")
    assert merged.raw_metadata["seen_on_sources"] == ["beatport", "soundcloud"]
    # The fetchers' own items are untouched.
    assert "sample_url" not in soundcloud.raw_metadata
    assert "seen_on_sources" not in beatport.raw_metadata
    assert "seen_on_sources" not in soundcloud.raw_metadata


def test_cross_source_group_has_one_ref_per_source_and_primary_is_richest(taxonomy):
    built = build_items(
        [_poor_beatport(), _rich_soundcloud()],
        taxonomy,
        observed_on=OBSERVED_ON,
        seen_at=SEEN_AT,
    )

    assert len(built.items) == 1
    item = built.items[0]
    assert item["sources"] == [
        {
            "source": "beatport",
            "id": "555",
            "url": "https://www.beatport.com/track/swandive/555",
        },
        {
            "source": "soundcloud",
            "id": "998877",
            "url": "https://soundcloud.com/sully/swandive",
        },
    ]
    assert item["primary_source"] == "soundcloud"
    assert item["download_count"] == 42
    assert item["reposts_count"] == 7
    assert item["free_download"] is True
    assert item["acquisition_url"] == "https://sully.bandcamp.com/track/swandive"
    assert check_item_relations(item) is None


def test_same_source_twice_keeps_one_ref(taxonomy):
    poor = _poor_beatport()
    rich = _item(
        "beatport",
        artist="Sully",
        title="Swandive",
        link="https://www.beatport.com/track/swandive/666",
        label="Astrophonica",
        release_date="2026-08-14",
        release_name="Swandive",
        raw_metadata={"beatport_id": 666, "genre_slug": "breaks-breakbeat-uk-bass"},
    )

    # Whichever order the charts answered in, the richer row is the one posted:
    # a pool item keeps one {id, url} per source and the README says post that one.
    for members in ([poor, rich], [rich, poor]):
        built = build_items(members, taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT)

        item = built.items[0]
        assert [ref["source"] for ref in item["sources"]] == ["beatport"]
        assert item["sources"][0]["id"] == "666"
        assert check_item_relations(item) is None


def test_source_ref_falls_back_to_the_link_and_refuses_an_empty_one():
    with_id = _item("bandcamp", raw_metadata={"bandcamp_album_id": 42})
    assert source_ref(with_id) == {
        "source": "bandcamp",
        "id": "42",
        "url": "https://example.test/1",
    }

    without_id = _item("boomkat", link="https://boomkat.com/products/x")
    assert source_ref(without_id) == {
        "source": "boomkat",
        "id": "https://boomkat.com/products/x",
        "url": "https://boomkat.com/products/x",
    }

    assert source_ref(_item("bleep", link="")) is None


# ---------------------------------------------------------------------------
# Genres, skips and the observation
# ---------------------------------------------------------------------------

def test_families_are_derived_from_fine_genres(taxonomy):
    built = build_items(
        _beatport_members(), taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    item = built.items[0]
    assert item["fine_genres"] == ["tech-house", "uk-garage-bassline"]
    assert item["families"] == ["house", "uk-garage"]


def test_artwork_uuid_survives_a_merge_the_volumo_row_loses(taxonomy):
    """A Volumo row that loses the richness contest still carries the artwork.

    `artwork_url_for` reads the merged row, so a key the merge does not backfill
    is a key the item publishes as null.
    """
    volumo = _item(
        "volumo",
        artist="Prunk",
        title="Get Down",
        link=VOLUMO_LINK,
        label="PIV",
        raw_metadata={
            "volumo_track_id": "c7f0a1e2",
            "volumo_genre_id": 21,
            "artwork_uuid": "c7f0a1e2",
        },
    )
    beatport = _item(
        "beatport",
        artist="Prunk",
        title="Get Down",
        link="https://www.beatport.com/track/get-down/321",
        label="PIV",
        release_date="2026-08-21",
        release_name="Get Down",
        raw_metadata={"beatport_id": 321, "genre_slug": "tech-house"},
    )

    assert "artwork_uuid" in PUBLISHER_BACKFILL_KEYS

    built = build_items(
        [volumo, beatport], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    item = built.items[0]
    assert item["primary_source"] == "beatport"
    assert item["artwork_url"] == "https://volumo.com/img/size/500x0/c7f0a1e2.jpg"


def test_primary_source_is_the_richest_member_that_produced_a_ref(taxonomy):
    """A linkless winner must not name a source the item does not carry.

    Bandcamp sends `item_url: ""` for a row it could not resolve, and the
    sources archive defaults `link` to "" — and a Bandcamp row is typically the
    richest member of a cross-source group, so this is the common shape, not a
    corner. Naming it `primary_source` would fail `bad_primary_source` and drop
    an item that is otherwise perfectly good.
    """
    linkless = _item(
        "volumo",
        artist="Drumskull",
        title="Muscle Memory EP",
        link="",
        label="Hooversound Recordings",
        release_date="2026-09-04",
        release_name="Muscle Memory EP",
        raw_metadata={"volumo_track_id": "zz", "volumo_genre_id": 3},
    )
    beatport = _item(
        "beatport",
        artist="Drumskull",
        title="Muscle Memory EP",
        link="https://www.beatport.com/track/muscle-memory/777",
        raw_metadata={"beatport_id": 777, "genre_slug": "breaks-breakbeat-uk-bass"},
    )

    built = build_items(
        [linkless, beatport], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    assert built.skipped == []
    item = built.items[0]
    assert [ref["source"] for ref in item["sources"]] == ["beatport"]
    assert item["primary_source"] == "beatport"
    assert item["observation"]["source"] == "beatport"
    # The facts still come from the richest member, linkless or not.
    assert item["label"] == "Hooversound Recordings"
    assert item["release_date"] == "2026-09-04"
    assert check_item_relations(item) is None


def test_richness_ties_are_broken_by_source_name(taxonomy):
    """A tie settled by fetch order would flip a stored pool document daily.

    Each run replaces the document's facts, so the same tied pair arriving in a
    different order tomorrow would change its granularity, its primary source
    and its release facts for no reason at all.
    """
    bandcamp = _item(
        "bandcamp",
        artist="Drumskull",
        title="Muscle Memory EP",
        link=BANDCAMP_LINK,
        label="Hooversound Recordings",
        release_date="2026-09-04",
        release_name="Muscle Memory EP",
        raw_metadata={"bandcamp_album_id": 3186429057, "bandcamp_tag": "breakbeat"},
    )
    beatport = _item(
        "beatport",
        artist="Drumskull",
        title="Muscle Memory EP",
        link="https://www.beatport.com/track/muscle-memory/777",
        label="Hooversound",
        release_date="2026-09-03",
        release_name="Muscle Memory",
        raw_metadata={"beatport_id": 777, "genre_slug": "breaks-breakbeat-uk-bass"},
    )

    forwards, backwards = (
        build_items(members, taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT).items[0]
        for members in ([bandcamp, beatport], [beatport, bandcamp])
    )

    assert forwards == backwards
    assert forwards["granularity"] == "release"
    assert forwards["primary_source"] == "bandcamp"
    assert forwards["label"] == "Hooversound Recordings"
    assert forwards["release_date"] == "2026-09-04"
    assert merge_facts([bandcamp, beatport]).source == "bandcamp"
    assert merge_facts([beatport, bandcamp]).source == "bandcamp"


def test_blank_artist_or_title_is_skipped_and_names_are_stripped(taxonomy):
    """One blank name would otherwise cost the whole batch, and with it the
    manifest — `minLength: 1` is a document-wide schema failure."""
    def row(number, artist, title):
        return _item(
            "beatport",
            artist=artist,
            title=title,
            link=f"https://www.beatport.com/track/get-down/{number}",
            raw_metadata={"beatport_id": number, "genre_slug": "tech-house"},
        )

    built = build_items(
        [row(1, "   ", "Get Down"), row(2, "Prunk", " "), row(3, "  Prunk ", " Get Down  ")],
        taxonomy,
        observed_on=OBSERVED_ON,
        seen_at=SEEN_AT,
    )

    assert [reason for _, reason in built.skipped] == [
        "missing_artist_title",
        "missing_artist_title",
    ]
    assert len(built.items) == 1
    assert built.items[0]["artist"] == "Prunk"
    assert built.items[0]["title"] == "Get Down"
    validate("batch", batch_payload(RUN_ID, 1, built.items, taxonomy.version))


def test_item_without_fine_genre_is_skipped_with_reason(taxonomy):
    orphan = _item("beatport", raw_metadata={"beatport_id": 1, "genre_slug": "polka"})

    built = build_items([orphan], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT)

    assert built.items == []
    assert built.skipped == [("a||t", "no_fine_genre")]


def test_item_without_link_is_skipped(taxonomy):
    linkless = _item(
        "beatport", link="", raw_metadata={"beatport_id": 1, "genre_slug": "tech-house"}
    )

    built = build_items([linkless], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT)

    assert built.items == []
    assert built.skipped == [("a||t", "no_sources")]


def test_key_over_512_is_skipped(taxonomy):
    huge = _item(
        "beatport",
        title="Muscle " * 100,
        raw_metadata={"beatport_id": 1, "genre_slug": "tech-house"},
    )

    built = build_items([huge], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT)

    assert built.items == []
    assert [reason for _, reason in built.skipped] == ["key_too_long"]


def test_observation_source_is_the_charting_member(taxonomy):
    charting = _item(
        "beatport",
        artist="Drumskull",
        title="Muscle Memory EP",
        link="https://www.beatport.com/track/muscle-memory/777",
        raw_metadata={
            "beatport_id": 777,
            "genre_slug": "breaks-breakbeat-uk-bass",
            "chart_position": 3,
        },
    )
    richest = _bandcamp_member()

    built = build_items(
        [richest, charting], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    item = built.items[0]
    assert item["primary_source"] == "bandcamp"
    assert item["observation"] == {
        "date": "2026-09-06",
        "source": "beatport",
        "chart_position": 3,
        "seen_at": SEEN_AT,
    }
    assert check_item_relations(item) is None


def test_bandcamp_release_granularity_and_null_bpm(taxonomy):
    built = build_items(
        [_bandcamp_member()], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    item = built.items[0]
    assert item["granularity"] == "release"
    assert item["version"] is None
    assert item["bpm"] is None
    assert item["observation"]["chart_position"] is None
    assert item["observation"]["source"] == "bandcamp"


# ---------------------------------------------------------------------------
# Coercions
# ---------------------------------------------------------------------------

def test_release_date_blank_becomes_null_and_bad_bpm_becomes_null(taxonomy):
    assert release_date_or_none("") is None
    assert release_date_or_none(None) is None
    assert release_date_or_none("2026-08-28") == "2026-08-28"
    assert release_date_or_none("28 August 2026") is None
    assert release_date_or_none("2026-13-01") is None

    assert bpm_or_none(None) is None
    assert bpm_or_none("") is None
    assert bpm_or_none("not a number") is None
    assert bpm_or_none(0) is None
    assert bpm_or_none(900) is None
    assert bpm_or_none("128") == 128.0

    blank = _item(
        "beatport",
        release_date="",
        raw_metadata={"beatport_id": 1, "genre_slug": "tech-house", "bpm": "fast"},
    )
    item = build_items([blank], taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT).items[0]
    assert item["release_date"] is None
    assert item["bpm"] is None


def test_camelot_from_beatport_key_name_and_volumo_keysign():
    beatport = _item("beatport", raw_metadata={"key": "A Minor"})
    volumo = _item("volumo", raw_metadata={"keysign": "C major"})
    junk = _item("volumo", raw_metadata={"keysign": "Hurdy Gurdy"})

    assert camelot_or_none(beatport) == "8A"
    assert camelot_or_none(volumo) == "8B"
    assert camelot_or_none(junk) is None
    assert camelot_or_none(_item("bandcamp")) is None


def test_preview_precedence():
    beatport, _charting = _beatport_members()
    volumo = _volumo_member()
    soundcloud = _rich_soundcloud()
    bandcamp = _bandcamp_member()
    members = [bandcamp, soundcloud, volumo, beatport]

    assert preview_for(beatport, members, SEEN_AT) == {
        "kind": "beatport_sample",
        "ref": "https://geo-samples.beatport.com/track/19283746.LOFI.mp3",
        "eligible": True,
        "checked_at": SEEN_AT,
    }
    assert preview_for(volumo, members[:-1], SEEN_AT) == {
        "kind": "volumo_prelisten",
        "ref": "https://volumo.com/api/v1/tracks/c7f0a1e2/prelisten.mp3",
        "eligible": True,
        "checked_at": SEEN_AT,
    }
    assert preview_for(soundcloud, [bandcamp, soundcloud], SEEN_AT) == {
        "kind": "soundcloud_widget",
        "ref": "https://soundcloud.com/sully/swandive",
        "eligible": True,
        "checked_at": SEEN_AT,
    }
    assert preview_for(bandcamp, [bandcamp], SEEN_AT) == {
        "kind": "bandcamp_embed",
        "ref": "3186429057",
        "eligible": True,
        "checked_at": SEEN_AT,
    }
    bare = _item("bandcamp")
    assert preview_for(bare, [bare], SEEN_AT) is None


def test_artwork_url_bandcamp_built_from_item_image_id():
    bandcamp = _bandcamp_member()
    assert artwork_url_for(bandcamp) == "https://f4.bcbits.com/img/a3186429057_16.jpg"

    with_own = _item("volumo", raw_metadata={"artwork_url": "https://cdn.volumo.com/a.jpg"})
    assert artwork_url_for(with_own) == "https://cdn.volumo.com/a.jpg"

    junk = _item("volumo", raw_metadata={"artwork_url": "/tmp/not-a-url.jpg"})
    assert artwork_url_for(junk) is None
    assert artwork_url_for(_item("bleep")) is None


def test_artwork_url_volumo_built_from_artwork_uuid():
    volumo = _volumo_member()
    assert artwork_url_for(volumo) == "https://volumo.com/img/size/500x0/c7f0a1e2.jpg"

    assert artwork_url_for(_item("volumo", raw_metadata={"artwork_uuid": ""})) is None


# ---------------------------------------------------------------------------
# Batches, manifest and the per-source report
# ---------------------------------------------------------------------------

def test_items_sorted_by_key_v2_and_batches_of_200(taxonomy):
    items = [
        _item(
            "beatport",
            title=f"Track {index:04d}",
            link=f"https://www.beatport.com/track/t/{index}",
            raw_metadata={"beatport_id": index, "genre_slug": "tech-house"},
        )
        for index in reversed(range(401))
    ]

    built = build_items(items, taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT)

    keys = [item["key_v2"] for item in built.items]
    assert keys == sorted(keys)
    assert len(keys) == 401

    chunks = batches(built.items, 200)
    assert [len(chunk) for chunk in chunks] == [200, 200, 1]
    assert chunks[0][0] is built.items[0]
    assert batches([], 200) == []


def test_batch_payload_stamps_versions(taxonomy):
    built = build_items(
        _sample_source_items(), taxonomy, observed_on=OBSERVED_ON, seen_at=SEEN_AT
    )

    payload = batch_payload(RUN_ID, 2, built.items, taxonomy.version)

    assert payload["schema_version"] == SCHEMA_VERSION == 1
    assert payload["identity_version"] == IDENTITY_VERSION == 2
    assert payload["taxonomy_version"] == 1
    assert payload["batch_no"] == 2
    assert payload["run_id"] == RUN_ID
    validate("batch", payload)


def test_per_source_report_lists_all_nine():
    health = {
        "beatport": {"count": 120, "error": None},
        "bandcamp": {"count": 30, "error": None},
        "volumo": {
            "count": 0,
            "error": "429 Client Error: Too Many Requests for url: https://api.volumo.com/v1/albums?x=1",
        },
    }
    fetch_switches = {"soundcloud": False}
    configured_enabled = {"beatport", "bandcamp", "volumo", "soundcloud"}

    report = per_source_report(health, fetch_switches, configured_enabled)

    assert set(report) == set(KNOWN_SOURCES)
    assert len(report) == 9
    assert report["beatport"] == {"count": 120, "error": None, "enabled": True}
    assert report["volumo"] == {
        "count": 0,
        "error": "429 Client Error: Too Many Requests for url: <url>",
        "enabled": True,
    }
    assert report["soundcloud"] == {"count": 0, "error": None, "enabled": False}
    for name in ("traxsource", "boomkat", "bleep", "resident_advisor", "mixupload"):
        assert report[name] == {"count": 0, "error": None, "enabled": False}


def test_summarise_error_strips_urls_paths_and_truncates():
    assert summarise_error(None) is None
    assert summarise_error("") is None
    assert summarise_error("   ") is None

    assert (
        summarise_error(
            "401 Client Error: Unauthorized for url: https://api.beatport.com/v4/auth/o/token/"
        )
        == "401 Client Error: Unauthorized for url: <url>"
    )
    assert (
        summarise_error("could not read /Users/christophe/Development/TuneFinder/.env")
        == "could not read <path>"
    )
    assert summarise_error("temp file /var/folders/zz/T/x.json vanished") == (
        "temp file <path> vanished"
    )
    assert summarise_error("line one\n  line two\t\tline three") == (
        "line one line two line three"
    )

    long_error = summarise_error("beatport failed. " * 40)
    assert len(long_error) == 200


def test_manifest_payload_validates():
    per_source = per_source_report(
        {"beatport": {"count": 1, "error": None}}, {}, {"beatport"}
    )

    payload = manifest_payload(
        RUN_ID, "2026-09-06T06:00:00Z", "2026-09-06T06:41:22Z", 3, per_source
    )

    assert payload["schema_version"] == 1
    assert payload["batches"] == 3
    assert payload["publisher_version"] == PUBLISHER_VERSION
    validate("manifest", payload)
