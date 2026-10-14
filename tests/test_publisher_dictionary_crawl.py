"""Tests for src/publisher/dictionary/crawl.py — the Beatport artist crawl moved
from the mini's standalone script, plus its nightly top-up.

Offline: every test runs on an in-memory SQLite database and a fake client or a
MagicMock session; the autouse `_no_sockets` guard fails any test that reaches
for the network, and no test asks for a Beatport token.
"""
import datetime as dt
import socket
from unittest.mock import MagicMock

import pytest

from src.publisher.dictionary import crawl


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def _guard(*args, **kwargs):
        raise AssertionError("test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _guard)


TODAY = dt.date(2026, 10, 14)


class FakeClient:
    """Answers every slice with an empty page and records the params sent."""

    def __init__(self):
        self.params = []
        self.requests = 0

    def get(self, params):
        self.params.append(dict(params))
        self.requests += 1
        return {"count": 0, "results": [], "next": None}


def _db():
    return crawl.connect(":memory:")


def _slices(db, since="0000"):
    return sorted(db.execute(
        "SELECT genre_id, start, end, state, next_page FROM slices WHERE start >= ?", (since,)
    ).fetchall())


def _insert(db, rows):
    db.executemany(
        "INSERT INTO slices (genre_id, start, end, state, count, next_page) VALUES (?,?,?,?,?,?)", rows
    )
    db.commit()


# ---------------------------------------------------------------------------
# Genres
# ---------------------------------------------------------------------------

def test_crawl_genres_come_from_taxonomy():
    # The 29 ids the standalone crawler hardcoded — now read from taxonomy.yaml.
    assert crawl.genre_ids() == [1, 2, 3, 5, 6, 9, 11, 12, 14, 15, 18, 37, 38, 50, 63, 81, 85, 86,
                                 89, 90, 91, 92, 93, 94, 95, 97, 100, 105, 108]


# ---------------------------------------------------------------------------
# Top-up
# ---------------------------------------------------------------------------

def test_crawl_top_up_slices():
    db = _db()
    _insert(db, [
        # an old month, done — untouched by a top-up
        (5, "2026-08-01", "2026-08-31", "done", 120, 2),
        # last month, split over the cap, both halves done
        (5, "2026-09-01", "2026-09-30", "split", 10_000, 2),
        (5, "2026-09-01", "2026-09-15", "done", 6_000, 31),
        (5, "2026-09-16", "2026-09-30", "done", 5_000, 26),
        (12, "2026-09-01", "2026-09-30", "done", 900, 6),
        # this month, walked up to an earlier "today"
        (5, "2026-10-01", "2026-10-09", "done", 400, 3),
        (12, "2026-10-01", "2026-10-09", "done", 100, 2),
    ])
    db.execute("INSERT INTO tracks VALUES (1, 5, '2026-09-03')")
    db.commit()

    added = crawl.top_up(db, [5, 12], TODAY)

    assert added == 4
    assert _slices(db, "2026-09-01") == [
        (5, "2026-09-01", "2026-09-30", "todo", 1),
        (5, "2026-10-01", "2026-10-14", "todo", 1),
        (12, "2026-09-01", "2026-09-30", "todo", 1),
        (12, "2026-10-01", "2026-10-14", "todo", 1),
    ]
    assert _slices(db, "2026-08-01")[0] == (5, "2026-08-01", "2026-08-31", "done", 2)
    assert db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1  # crawled data kept


def test_crawl_top_up_window_spans_a_year_boundary():
    assert crawl.top_up_window(dt.date(2027, 1, 3)) == [
        (dt.date(2026, 12, 1), dt.date(2026, 12, 31)),
        (dt.date(2027, 1, 1), dt.date(2027, 1, 3)),
    ]


def test_crawl_run_tops_up_only_once_the_backfill_is_done():
    db = _db()
    _insert(db, [
        (5, "2016-11-01", "2016-11-30", "todo", None, 1),          # the last backfill slice
        (5, "2026-09-01", "2026-09-30", "done", 900, 6),
        (5, "2026-10-01", "2026-10-09", "done", 400, 3),
    ])
    client = FakeClient()

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], top_up_when_done=True, today=TODAY)

    walked = [p["publish_date"] for p in client.params]
    # backfill first, then the window — re-walked once, newest first
    assert walked == ["2016-11-01:2016-11-30", "2026-10-01:2026-10-14", "2026-09-01:2026-09-30"]
    assert all("order_by" not in p for p in client.params)
    assert outcome.topped_up is True and outcome.error is False
    assert outcome.requests == 3
    assert db.execute("SELECT COUNT(*) FROM slices WHERE state='todo'").fetchone()[0] == 0
    assert db.execute("SELECT requests, note FROM runs").fetchall() == [(3, "finished")]


def test_crawl_run_without_top_up_leaves_the_window_alone():
    db = _db()
    _insert(db, [(5, "2026-10-01", "2026-10-09", "done", 400, 3)])
    client = FakeClient()

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], top_up_when_done=False, today=TODAY)

    assert client.params == []
    assert outcome.topped_up is False
    assert _slices(db) == [(5, "2026-10-01", "2026-10-09", "done", 3)]


# ---------------------------------------------------------------------------
# Behaviour kept from the standalone crawler
# ---------------------------------------------------------------------------

def _track(track_id, date, artists, remixers=()):
    return {
        "id": track_id,
        "publish_date": date,
        "artists": [{"id": a, "name": f"Artist {a}", "slug": f"artist-{a}"} for a in artists],
        "remixers": [{"id": r, "name": f"Artist {r}", "slug": f"artist-{r}"} for r in remixers],
    }


def test_crawl_slice_pages_stores_credits_and_splits_over_the_cap():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1),
                 (12, "2026-09-01", "2026-09-30", "todo", None, 1)])

    pages = iter([
        {"count": 201, "next": "p2", "results": [_track(1, "2026-09-02", [10], [11])] * 200},
        {"count": 201, "next": None, "results": [_track(2, "2026-09-03", [10])]},
    ])
    client = MagicMock()
    client.get.side_effect = lambda params: next(pages)
    crawl.crawl_slice(db, client, 5, "2026-09-01", "2026-09-30", 1, None)

    assert [c.args[0]["page"] for c in client.get.call_args_list] == [1, 2]
    assert all(c.args[0]["per_page"] == 200 for c in client.get.call_args_list)
    assert db.execute("SELECT * FROM tracks ORDER BY id").fetchall() == [
        (1, 5, "2026-09-02"), (2, 5, "2026-09-03")]
    assert db.execute("SELECT * FROM credits ORDER BY track_id, artist_id").fetchall() == [
        (1, 10, "artist"), (1, 11, "remixer"), (2, 10, "artist")]
    assert db.execute(
        "SELECT state, count, next_page FROM slices WHERE genre_id=5").fetchone() == ("done", 201, 3)

    over = MagicMock()
    over.get.return_value = {"count": 10_000, "next": "p2", "results": []}
    crawl.crawl_slice(db, over, 12, "2026-09-01", "2026-09-30", 1, None)
    assert _slices(db) == [
        (5, "2026-09-01", "2026-09-30", "done", 3),
        (12, "2026-09-01", "2026-09-15", "todo", 1),
        (12, "2026-09-01", "2026-09-30", "split", 1),
        (12, "2026-09-16", "2026-09-30", "todo", 1),
    ]


def test_crawl_client_backs_off_on_429_and_stops_after_three():
    session = MagicMock()
    session.headers = {}
    resp = MagicMock(status_code=429, headers={"Retry-After": "7"})
    session.get.return_value = resp
    sleeps = []
    client = crawl.Client(2.0, 0, token_fn=lambda: ("tok", "ua"), session=session,
                          sleep=sleeps.append, clock=lambda: 1000.0)

    with pytest.raises(crawl.Stop, match="three 429s"):
        client.get({"genre_id": 5})

    assert client.interval == 16.0          # doubled on each 429
    assert sleeps.count(7) == 2              # Retry-After honoured between them
    assert session.headers["Authorization"] == "Bearer tok"
    assert session.get.call_count == 3


def test_crawl_deadline_stops_the_run_cleanly():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1)])
    client = FakeClient()
    now = dt.datetime(2026, 10, 15, 5, 31)

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], deadline=dt.datetime(2026, 10, 15, 5, 30),
                              today=TODAY, now=lambda: now)

    assert outcome.note == "deadline" and outcome.error is False
    assert client.params == []
