"""Tests for the Bandcamp fetcher — focused on metadata capture."""
from unittest.mock import MagicMock, patch

from src.fetchers import bandcamp


def _settings(enabled=True, tags=None, count=5):
    s = MagicMock()
    s.get_source_config.return_value = {
        "enabled": enabled,
        "tags": tags or ["drum-and-bass"],
        "count_per_tag": count,
    }
    return s


def _discover_item(item_id=2697521627, band_name="Sully", title="Skyline", item_type="a"):
    return {
        "item_id": item_id,
        "item_type": item_type,
        "title": title,
        "album_artist": band_name,
        "band_name": band_name,
        "item_url": f"https://sully.bandcamp.com/album/skyline",
        "release_date": "2026-05-01",
    }


def test_bandcamp_album_id_captured():
    with patch("src.fetchers.bandcamp._fetch_tag", return_value=[_discover_item(item_id=12345678)]):
        items = bandcamp.fetch(_settings())
    assert len(items) == 1
    assert items[0].raw_metadata["bandcamp_album_id"] == 12345678


def test_bandcamp_album_id_none_when_missing():
    item = _discover_item()
    del item["item_id"]
    with patch("src.fetchers.bandcamp._fetch_tag", return_value=[item]):
        items = bandcamp.fetch(_settings())
    assert len(items) == 1
    assert items[0].raw_metadata["bandcamp_album_id"] is None


def test_bandcamp_tag_and_item_type_still_present():
    with patch("src.fetchers.bandcamp._fetch_tag", return_value=[_discover_item(item_type="a")]):
        items = bandcamp.fetch(_settings(tags=["drum-and-bass"]))
    assert items[0].raw_metadata["bandcamp_tag"] == "drum-and-bass"
    assert items[0].raw_metadata["item_type"] == "a"


# ---------------------------------------------------------------------------
# publish-pool raw_metadata (M1d Task 3)
# ---------------------------------------------------------------------------

def test_raw_metadata_carries_item_image_id():
    """Verified live on 2026-09-06 with `_fetch_tag("house", 1)`: the
    discover_web result has NO top-level `item_image_id`. The artwork id lives
    at `primary_image.image_id` — an int such as 436449664, alongside
    `is_art: True` (`band_image` is the label's avatar, not the release)."""
    item = {**_discover_item(), "primary_image": {"image_id": 436449664, "is_art": True}}
    with patch("src.fetchers.bandcamp._fetch_tag", return_value=[item]):
        items = bandcamp.fetch(_settings())
    assert items[0].raw_metadata["item_image_id"] == 436449664


def test_raw_metadata_item_image_id_none_when_missing():
    with patch("src.fetchers.bandcamp._fetch_tag", return_value=[_discover_item()]):
        items = bandcamp.fetch(_settings())
    assert items[0].raw_metadata["item_image_id"] is None
