"""Tests for the publish-pool taxonomy resolver (src/publisher/taxonomy.py).

Loads the vendored tools/publish-pool-contract/taxonomy.yaml and resolves a
SourceItem's fine genres from the source ids/slugs/tags the fetchers store in
raw_metadata (or, for SoundCloud, genre_tags).
"""
import pytest

from src.models import SourceItem
from src.publisher.taxonomy import families_for, fine_genres_for, load_taxonomy


def _item(source, **kw):
    genre_tags = kw.pop("genre_tags", [])
    raw_metadata = kw.pop("raw_metadata", {})
    return SourceItem(
        source=source,
        artist="A",
        title="T",
        link="",
        genre_tags=genre_tags,
        raw_metadata=raw_metadata,
    )


def test_load_counts():
    taxonomy = load_taxonomy()
    assert taxonomy.version == 1
    assert len(taxonomy.families) == 8
    assert len(taxonomy.fine_genres) == 38


def test_every_fine_genre_has_a_known_family():
    taxonomy = load_taxonomy()
    for fine_id, family_id in taxonomy.fine_genres.items():
        assert family_id in taxonomy.families, f"{fine_id} points at unknown family {family_id!r}"


def test_beatport_breaks_row_maps_to_two_fines():
    taxonomy = load_taxonomy()
    assert taxonomy.beatport_genres["breaks-breakbeat-uk-bass"] == ["breaks-breakbeat", "uk-bass"]


def test_beatport_sub_genre_wins():
    taxonomy = load_taxonomy()
    item = _item(
        "beatport",
        raw_metadata={"genre_slug": "house", "sub_genre_slug": "soulful"},
    )
    assert fine_genres_for(item, taxonomy) == ["soulful-house"]


def test_beatport_unknown_slug_is_empty():
    taxonomy = load_taxonomy()
    item = _item(
        "beatport",
        raw_metadata={"genre_slug": "not-a-real-slug", "sub_genre_slug": "also-not-real"},
    )
    assert fine_genres_for(item, taxonomy) == []


def test_volumo_genre_id_resolves():
    taxonomy = load_taxonomy()
    item = _item("volumo", raw_metadata={"volumo_genre_id": 18})
    assert fine_genres_for(item, taxonomy) == ["downtempo"]


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("jungle", ["jungle"]),
        ("house", ["house"]),
        ("techno", ["techno-raw-deep"]),
        ("electronic", ["electronica"]),
    ],
)
def test_bandcamp_tag_resolves(tag, expected):
    taxonomy = load_taxonomy()
    item = _item("bandcamp", raw_metadata={"bandcamp_tag": tag})
    assert fine_genres_for(item, taxonomy) == expected


def test_soundcloud_uses_fine_tag_and_ignores_sc_genre():
    taxonomy = load_taxonomy()
    item = _item(
        "soundcloud",
        genre_tags=["house", "some-folksonomy-tag"],
        raw_metadata={"sc_genre": "House,Deep House"},
    )
    assert fine_genres_for(item, taxonomy) == ["house"]


def test_families_for_sorted_unique():
    taxonomy = load_taxonomy()
    assert families_for(["tech-house", "uk-garage-bassline", "house"], taxonomy) == [
        "house",
        "uk-garage",
    ]
