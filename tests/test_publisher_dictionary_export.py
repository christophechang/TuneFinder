"""Tests for src/publisher/dictionary/export.py — crawl.db aggregated into the
dictionary's wire shards.

Offline: an in-memory SQLite database in the crawler's schema and the vendored
taxonomy; nothing is posted.
"""
import datetime as dt
import json
import os
import socket

import pytest

from src.publisher.dictionary import crawl, export
from src.publisher.taxonomy import load_taxonomy


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def _guard(*args, **kwargs):
        raise AssertionError("test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _guard)


# The multi-tenant taxonomy's family order (src/TuneFinder.Core/Taxonomy/Taxonomy.cs
# FamilyEntries) — bit i of a mask is family i.
FAMILY_ORDER = ["house", "techno", "electro", "drum-and-bass", "breaks-uk-bass",
                "uk-garage", "electronica-downtempo", "hip-hop-rnb-soul"]
HOUSE, TECHNO, DNB, BREAKS = 1, 2, 8, 16

NOW = dt.datetime(2026, 10, 14, 5, 40, 12, tzinfo=dt.timezone.utc)


def _db(artists, tracks, credits):
    db = crawl.connect(":memory:")
    db.executemany("INSERT INTO artists (id, name, slug) VALUES (?,?,?)", [(i, n, None) for i, n in artists])
    db.executemany("INSERT INTO tracks VALUES (?,?,?)", tracks)
    db.executemany("INSERT INTO credits VALUES (?,?,?)", credits)
    db.commit()
    return db


def test_export_family_bits_follow_the_multi_tenant_order():
    taxonomy = load_taxonomy()
    assert list(taxonomy.families) == FAMILY_ORDER

    masks = export.genre_family_masks(taxonomy)

    assert sorted(masks) == crawl.genre_ids()
    assert all(mask for mask in masks.values())
    assert masks[5] == HOUSE                # house
    assert masks[6] == TECHNO               # techno-peak-time-driving
    assert masks[94] == 4                   # electro
    assert masks[1] == DNB                  # drum-bass
    assert masks[9] == BREAKS               # breaks-breakbeat-uk-bass (two fines, one family)
    assert masks[86] == 32                  # uk-garage-bassline
    assert masks[3] == 64                   # electronica
    assert masks[105] == 128                # hip-hop


def test_export_dedupes_and_maps_families():
    db = _db(
        artists=[
            (1, "Kerri Chandler"),
            (2, "  Kerri   Chandler "),     # a second Beatport id, same spelling once cleaned
            (3, "Calibré"),
            (4, "Calibre"),                 # differs only by accent: kept, the server folds
            (5, "   "),                     # blank: dropped
            (6, "x" * 101),                 # over the API's 100: dropped, never truncated
            (7, "Undated"),                 # credited only on a track with no publish date
            (8, "Burial"),
        ],
        tracks=[
            (100, 5, "2026-01-10"),   # house
            (101, 6, "2026-03-02"),   # techno
            (102, 5, "2026-03-02"),   # house, same date as 101
            (103, 1, "2025-12-31"),   # drum & bass
            (104, 9, "2024-06-01"),   # breaks / uk bass
            (105, 5, None),
            (106, 6, "2026-02-01"),
        ],
        credits=[
            (100, 1, "artist"),
            (101, 2, "remixer"),
            (102, 1, "artist"),       # 2026-03-02 again: one release date, not two
            (103, 3, "artist"),
            (103, 4, "artist"),
            (104, 8, "artist"),
            (105, 7, "artist"),
            (106, 5, "artist"),
            (106, 6, "artist"),
        ],
    )

    entries = export.aggregate(db, load_taxonomy())

    assert entries == [
        ["Burial", 1, "2024-06", BREAKS],
        ["Calibre", 1, "2025-12", DNB],
        ["Calibré", 1, "2025-12", DNB],
        ["Kerri Chandler", 2, "2026-03", HOUSE | TECHNO],
    ]
    names = [entry[0] for entry in entries]
    assert len(names) == len(set(names))


def _entries(n, name=lambda i: f"artist {i:06d}"):
    return [[name(i), 1 + i % 7, "2026-10", 1 << (i % 8)] for i in range(n)]


def _assert_shards(shards, entries, max_bytes, max_entries, version="v"):
    assert [s["index"] for s in shards] == list(range(len(shards)))
    assert all(s["total"] == len(shards) for s in shards)
    assert all(s["version"] == version and s["schema_version"] == 1 for s in shards)
    assert [e for s in shards for e in s["entries"]] == entries
    for s in shards:
        assert list(s) == ["schema_version", "version", "index", "total", "entries"]
        assert len(json.dumps(s).encode("utf-8")) <= max_bytes   # as requests sends it
        assert 1 <= len(s["entries"]) <= max_entries


def test_export_shards_under_limit(monkeypatch):
    # The real limits: 100k short names are bound by the 40,000-entry cap...
    entries = _entries(100_000)
    shards = export.shard(entries, "v")
    _assert_shards(shards, entries, 1_500_000, 40_000)
    assert [len(s["entries"]) for s in shards] == [40_000, 40_000, 20_000]

    # ...and 60k long non-ASCII names by the 1.5 MB body (escaped as \uXXXX on the wire).
    entries = _entries(60_000, name=lambda i: f"Ñoño Ünïcødé Ärtíst Námé {i:06d}")
    shards = export.shard(entries, "v")
    _assert_shards(shards, entries, 1_500_000, 40_000)
    assert len(shards) > 3 and max(len(s["entries"]) for s in shards) < 40_000

    # Tight limits, so the greedy fill is exercised at many boundaries.
    monkeypatch.setattr(export, "MAX_SHARD_BYTES", 8_000)
    monkeypatch.setattr(export, "MAX_SHARD_ENTRIES", 20)
    entries = _entries(600, name=lambda i: "é" * (i % 90 + 1))
    shards = export.shard(entries, "v")
    _assert_shards(shards, entries, 8_000, 20)
    sizes = [len(s["entries"]) for s in shards]
    assert 20 in sizes and min(sizes) < 20   # some closed by count, some by bytes


def test_export_refuses_more_than_64_shards(monkeypatch):
    monkeypatch.setattr(export, "MAX_SHARD_ENTRIES", 10)
    export.shard(_entries(640), "v")                         # 64 shards: fine
    with pytest.raises(export.ExportError, match="65 shards"):
        export.shard(_entries(641), "v")


def test_export_crawled_through_counts_back_without_a_gap():
    db = crawl.connect(":memory:")
    db.executemany("INSERT INTO slices (genre_id, start, end, state) VALUES (?,?,?,?)", [
        (5, "2026-10-01", "2026-10-14", "todo"),   # top-up window, being re-walked
        (5, "2026-09-01", "2026-09-30", "done"),
        (5, "2026-08-01", "2026-08-15", "done"),
        (5, "2026-08-16", "2026-08-31", "done"),
        (5, "2026-08-01", "2026-08-31", "split"),
        (5, "2026-07-01", "2026-07-31", "todo"),   # the backfill's frontier
        (5, "2026-06-01", "2026-06-30", "done"),
    ])

    assert export.crawled_through(db, dt.date(2026, 10, 14)) == "2026-08"


def test_export_build_and_write(tmp_path):
    db = _db(artists=[(1, "Burial")], tracks=[(100, 9, "2024-06-01")], credits=[(100, 1, "artist")])
    db.execute("INSERT INTO slices (genre_id, start, end, state) VALUES (9, '2026-10-01', '2026-10-14', 'done')")

    built = export.build_export(db, now=NOW)

    assert built.version == "2026-10-14T05:40Z"
    assert built.shards == [{"schema_version": 1, "version": "2026-10-14T05:40Z", "index": 0,
                             "total": 1, "entries": [["Burial", 1, "2024-06", BREAKS]]}]
    assert built.commit == {"schema_version": 1, "version": "2026-10-14T05:40Z", "total": 1,
                            "entry_count": 1, "crawled_through": "2026-10"}

    out = tmp_path / "export"
    out.mkdir()
    (out / "shard-007.json").write_text("{}")             # a stale file from a bigger export
    export.write_export(built, str(out))

    assert sorted(os.listdir(out)) == ["commit.json", "shard-000.json"]
    assert json.loads((out / "shard-000.json").read_text()) == built.shards[0]
    assert json.loads((out / "commit.json").read_text()) == built.commit


def test_export_refuses_an_empty_crawl():
    with pytest.raises(export.ExportError):
        export.build_export(crawl.connect(":memory:"), now=NOW)


@pytest.mark.parametrize("where", ["export", "export.tmp"])
def test_export_write_refuses_to_clear_a_directory_with_other_files(tmp_path, where):
    db = _db(artists=[(1, "Burial")], tracks=[(100, 9, "2024-06-01")], credits=[(100, 1, "artist")])
    built = export.build_export(db, now=NOW)
    target = tmp_path / where
    target.mkdir()
    (target / "shard-000.json").write_text("{}")
    (target / "notes.txt").write_text("keep me")

    with pytest.raises(export.ExportError, match="notes.txt"):
        export.write_export(built, str(tmp_path / "export"))

    assert (target / "notes.txt").read_text() == "keep me"


def test_export_write_refuses_a_file_where_the_directory_goes(tmp_path):
    db = _db(artists=[(1, "Burial")], tracks=[(100, 9, "2024-06-01")], credits=[(100, 1, "artist")])
    built = export.build_export(db, now=NOW)
    (tmp_path / "export").write_text("not a directory")

    with pytest.raises(export.ExportError):
        export.write_export(built, str(tmp_path / "export"))
    assert (tmp_path / "export").read_text() == "not a directory"
