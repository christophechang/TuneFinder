"""Tests for archive_source_items (src/fetchers/__init__.py)."""
import gzip
import json
import os
import time

from src.fetchers import archive_source_items, _item_to_dict
from src.models import SourceItem


def _item(n: int) -> SourceItem:
    return SourceItem(
        source="beatport",
        artist=f"Artist{n}",
        title=f"Track{n}",
        link=f"https://example.com/{n}",
        genre_tags=["dnb"],
    )


def test_archive_writes_gzip_file(tmp_path):
    items = [_item(1), _item(2)]
    archive_source_items(items, str(tmp_path), "2026-W24")
    expected = tmp_path / "archive" / "source_items_2026-W24.json.gz"
    assert expected.exists()


def test_archive_gzip_round_trips(tmp_path):
    items = [_item(1), _item(2)]
    archive_source_items(items, str(tmp_path), "2026-W24")
    path = tmp_path / "archive" / "source_items_2026-W24.json.gz"
    with gzip.open(str(path), "rb") as f:
        loaded = json.loads(f.read().decode("utf-8"))
    assert loaded == [_item_to_dict(i) for i in items]


def test_archive_prunes_oldest_beyond_26(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # Pre-create 30 files with distinct mtimes
    for i in range(30):
        p = archive_dir / f"source_items_2026-W{i:02d}.json.gz"
        with gzip.open(str(p), "wb") as f:
            f.write(b"[]")
        # Set distinct mtime: older files get lower mtime
        os.utime(str(p), (time.time() - (30 - i) * 100, time.time() - (30 - i) * 100))

    # Now archive a new one — should prune to 26 total
    archive_source_items([_item(1)], str(tmp_path), "2026-W30")
    remaining = list((archive_dir).glob("*.json.gz"))
    assert len(remaining) == 26


def test_archive_same_week_overwrites(tmp_path):
    items_v1 = [_item(1)]
    items_v2 = [_item(1), _item(2)]
    archive_source_items(items_v1, str(tmp_path), "2026-W24")
    archive_source_items(items_v2, str(tmp_path), "2026-W24")
    path = tmp_path / "archive" / "source_items_2026-W24.json.gz"
    with gzip.open(str(path), "rb") as f:
        loaded = json.loads(f.read().decode("utf-8"))
    assert len(loaded) == 2
