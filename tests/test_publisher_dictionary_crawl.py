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


def _named(track_id, date, artist_id, name):
    return {"id": track_id, "publish_date": date,
            "artists": [{"id": artist_id, "name": name, "slug": name.lower().replace(" ", "-")}]}


def _one_page(*tracks):
    client = MagicMock()
    client.get.return_value = {"count": len(tracks), "next": None, "results": list(tracks)}
    return client


def test_crawl_artist_seen_again_takes_the_newest_spelling():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1),
                 (5, "2026-08-01", "2026-08-31", "todo", None, 1),
                 (5, "2026-10-01", "2026-10-14", "todo", None, 1)])

    crawl.crawl_slice(db, _one_page(_named(1, "2026-09-10", 7, "DJ Old")),
                      5, "2026-09-01", "2026-09-30", 1, None)
    # the backfill reaching an older month never puts an older spelling back
    crawl.crawl_slice(db, _one_page(_named(2, "2026-08-10", 7, "DJ Older")),
                      5, "2026-08-01", "2026-08-31", 1, None)
    assert db.execute("SELECT name, slug FROM artists").fetchall() == [("DJ Old", "dj-old")]

    # a newer credit renames the artist
    crawl.crawl_slice(db, _one_page(_named(3, "2026-10-02", 7, "DJ New")),
                      5, "2026-10-01", "2026-10-14", 1, None)
    assert db.execute("SELECT name, slug, seen FROM artists").fetchall() == [
        ("DJ New", "dj-new", "2026-10-02")]


def test_crawl_connect_migrates_a_standalone_database(tmp_path):
    import sqlite3

    path = str(tmp_path / "crawl.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT, slug TEXT);"
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY, genre_id INTEGER, publish_date TEXT);"
        "CREATE TABLE credits (track_id INTEGER, artist_id INTEGER, role TEXT,"
        "  PRIMARY KEY (track_id, artist_id, role)) WITHOUT ROWID;"
        "INSERT INTO artists VALUES (7, 'DJ Old', 'dj-old');"
        "INSERT INTO tracks VALUES (1, 5, '2026-09-10'), (2, 5, '2026-07-01');"
        "INSERT INTO credits VALUES (1, 7, 'artist'), (2, 7, 'remixer');"
    )
    old.commit()
    old.close()

    db = crawl.connect(path)
    assert db.execute("SELECT name, seen FROM artists").fetchall() == [("DJ Old", "2026-09-10")]
    db.execute("INSERT INTO slices (genre_id, start, end) VALUES (5, '2026-08-01', '2026-08-31')")
    crawl.crawl_slice(db, _one_page(_named(3, "2026-08-10", 7, "DJ Older")),
                      5, "2026-08-01", "2026-08-31", 1, None)
    assert db.execute("SELECT name FROM artists").fetchall() == [("DJ Old",)]


# ---------------------------------------------------------------------------
# Failing slices never stall the crawl
# ---------------------------------------------------------------------------

class ScriptedClient:
    """Answers per (publish_date, page) from a script: a dict is the page, an
    exception is raised; anything unscripted is an empty last page."""

    def __init__(self, script):
        self.script, self.params, self.requests = script, [], 0

    def get(self, params):
        self.params.append(dict(params))
        self.requests += 1
        answer = self.script.get((params["publish_date"], params["page"]),
                                 {"count": 0, "results": [], "next": None})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _state(db, start):
    return db.execute("SELECT state, error, failed_night FROM slices WHERE start=?", (start,)).fetchone()


def test_crawl_page_past_the_first_answering_404_finishes_the_slice():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1)])
    client = ScriptedClient({
        ("2026-09-01:2026-09-30", 1): {"count": 500, "next": "p2", "results": [_track(1, "2026-09-02", [10])]},
        ("2026-09-01:2026-09-30", 2): crawl.PageError(404, "not found"),
    })

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], today=TODAY)

    assert _slices(db) == [(5, "2026-09-01", "2026-09-30", "done", 2)]
    assert outcome.failed == [] and outcome.error is False and outcome.pages == 1


def test_crawl_failing_slice_is_marked_and_the_crawl_moves_on():
    db = _db()
    _insert(db, [(5, "2026-10-01", "2026-10-14", "todo", None, 1),     # newest: Beatport refuses it
                 (5, "2026-09-01", "2026-09-30", "todo", None, 1),     # page 1 is not a page
                 (5, "2026-08-01", "2026-08-31", "todo", None, 1)])
    client = ScriptedClient({
        ("2026-10-01:2026-10-14", 1): crawl.PageError(400, "bad request"),
        ("2026-09-01:2026-09-30", 1): {"count": "lots", "results": None},
    })
    night = dt.datetime(2026, 10, 14, 22, 0)

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], today=TODAY, now=lambda: night)

    assert _state(db, "2026-10-01") == ("failed", "page 1: HTTP 400: bad request", "2026-10-14")
    assert _state(db, "2026-09-01")[:2] == ("failed", "page 1: not a page of tracks")
    assert _state(db, "2026-08-01")[0] == "done"                         # walked past the failures
    assert outcome.error is False and outcome.note == "finished"
    assert len(outcome.failed) == 2 and "HTTP 400" in outcome.failed[0]
    assert "failed 2 (2 tonight" in outcome.summary_line(db)


def test_crawl_5xx_that_outlasts_its_retries_fails_only_its_slice():
    session = MagicMock()
    session.headers = {}
    session.get.return_value = MagicMock(status_code=503, headers={}, text="unavailable")
    client = crawl.Client(0, 0, token_fn=lambda: ("tok", "ua"), session=session,
                          sleep=lambda s: None, clock=lambda: 1000.0)
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1)])

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], today=TODAY)

    assert session.get.call_count == 6
    assert _state(db, "2026-09-01")[:2] == ("failed", "page 1: HTTP 503: still failing after six attempts")
    assert outcome.error is False


def test_crawl_malformed_records_are_skipped_and_counted():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1)])
    good = _track(1, "2026-09-02", [10])
    client = ScriptedClient({("2026-09-01:2026-09-30", 1): {"count": 4, "next": None, "results": [
        good,
        {"id": "two", "artists": []},                                    # id not a number
        {"id": 3, "publish_date": "2026-09-03", "artists": [{"name": "No Id"}]},
        "not a record",
    ]}})

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], today=TODAY)

    assert outcome.skipped == 3 and outcome.failed == []
    assert db.execute("SELECT id FROM tracks").fetchall() == [(1,)]
    assert _state(db, "2026-09-01")[0] == "done"


def test_crawl_failed_slice_is_retried_once_per_later_night():
    db = _db()
    _insert(db, [(5, "2026-09-01", "2026-09-30", "todo", None, 1)])
    refuse = ScriptedClient({("2026-09-01:2026-09-30", 1): crawl.PageError(400, "bad request")})
    evening, morning = dt.datetime(2026, 10, 14, 22, 0), dt.datetime(2026, 10, 15, 4, 0)

    crawl.run_crawl(db, lambda: refuse, genres=[5], today=TODAY, now=lambda: evening)
    again = crawl.run_crawl(db, lambda: refuse, genres=[5], today=TODAY, now=lambda: morning)
    assert refuse.requests == 1 and again.retried == 0           # same night: not retried

    next_night = dt.datetime(2026, 10, 15, 22, 0)
    retried = crawl.run_crawl(db, lambda: refuse, genres=[5], today=TODAY, now=lambda: next_night)
    assert refuse.requests == 2 and retried.retried == 1         # one more try, still failing
    assert _state(db, "2026-09-01") == ("failed", "page 1: HTTP 400: bad request", "2026-10-15")

    fine = ScriptedClient({})
    crawl.run_crawl(db, lambda: fine, genres=[5], today=TODAY, now=lambda: dt.datetime(2026, 10, 16, 22, 0))
    assert _state(db, "2026-09-01")[0] == "done"


def test_crawl_top_up_runs_despite_failed_slices():
    db = _db()
    _insert(db, [(5, "2016-11-01", "2016-11-30", "todo", None, 1),
                 (5, "2026-09-01", "2026-09-30", "done", 900, 6),
                 (5, "2026-10-01", "2026-10-09", "done", 400, 3)])
    client = ScriptedClient({("2016-11-01:2016-11-30", 1): crawl.PageError(403, "forbidden")})

    outcome = crawl.run_crawl(db, lambda: client, genres=[5], top_up_when_done=True, today=TODAY)

    assert outcome.topped_up is True
    assert [p["publish_date"] for p in client.params] == [
        "2016-11-01:2016-11-30", "2026-10-01:2026-10-14", "2026-09-01:2026-09-30"]
    assert _state(db, "2016-11-01")[0] == "failed"


# ---------------------------------------------------------------------------
# The deadline caps every wait
# ---------------------------------------------------------------------------

def _deadline_client(response=None, error=None, left=100.0):
    session = MagicMock()
    session.headers = {}
    if error is not None:
        session.get.side_effect = error
    else:
        session.get.return_value = response
    sleeps = []
    client = crawl.Client(2.0, 0, token_fn=lambda: ("tok", "ua"), session=session,
                          sleep=sleeps.append, clock=lambda: 1000.0, deadline=1000.0 + left)
    return client, sleeps


def test_crawl_retry_after_never_sleeps_past_the_deadline():
    client, sleeps = _deadline_client(MagicMock(status_code=429, headers={"Retry-After": "300"}))

    with pytest.raises(crawl.Stop, match="deadline"):
        client.get({"genre_id": 5})
    assert sleeps == [100.0]


def test_crawl_network_backoff_never_sleeps_past_the_deadline():
    client, sleeps = _deadline_client(error=ConnectionError("down"), left=10.0)

    with pytest.raises(crawl.Stop, match="deadline"):
        client.get({"genre_id": 5})
    assert sleeps == [10.0]


def test_crawl_wait_past_the_deadline_stops_without_sleeping():
    client, sleeps = _deadline_client(MagicMock(status_code=503, headers={}), left=0.0)

    with pytest.raises(crawl.Stop, match="deadline"):
        client.get({"genre_id": 5})
    assert sleeps == []


def test_crawl_plist_stops_the_crawl_at_05_15():
    import os
    import plistlib

    path = os.path.join(os.path.dirname(__file__), "..", "com.openclaw.tunefinder-dictionary.plist")
    with open(path, "rb") as f:
        args = plistlib.load(f)["ProgramArguments"]
    assert args[args.index("--until") + 1] == "05:15"


# ---------------------------------------------------------------------------
# Top-up after downtime
# ---------------------------------------------------------------------------

def test_crawl_top_up_window_reaches_back_to_the_last_finished_top_up():
    assert crawl.top_up_window(TODAY, since=dt.date(2026, 7, 20)) == [
        (dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        (dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
        (dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
        (dt.date(2026, 10, 1), dt.date(2026, 10, 14)),
    ]
    # a recent top-up never narrows the window below the previous month
    assert crawl.top_up_window(TODAY, since=dt.date(2026, 10, 13))[0][0] == dt.date(2026, 9, 1)


def test_crawl_top_up_after_downtime_rewalks_every_month_since():
    db = _db()
    _insert(db, [(5, "2026-07-01", "2026-07-20", "done", 10, 2),
                 (5, "2026-06-01", "2026-06-30", "done", 10, 2)])
    crawl.set_meta(db, "top_up_finished", "2026-07-20")
    client = FakeClient()

    crawl.run_crawl(db, lambda: client, genres=[5], top_up_when_done=True, today=TODAY)

    assert [p["publish_date"] for p in client.params] == [
        "2026-10-01:2026-10-14", "2026-09-01:2026-09-30",
        "2026-08-01:2026-08-31", "2026-07-01:2026-07-31"]
    assert crawl.get_meta(db, "top_up_finished") == "2026-10-14"


def test_crawl_first_top_up_reaches_back_to_the_seed():
    db = _db()
    # seeded on 20 August; the backfill finished in October
    _insert(db, [(5, "2026-08-01", "2026-08-20", "done", 10, 2),
                 (5, "2026-07-01", "2026-07-31", "done", 10, 2)])
    client = FakeClient()

    crawl.run_crawl(db, lambda: client, genres=[5], top_up_when_done=True, today=TODAY)

    assert [p["publish_date"] for p in client.params][-1] == "2026-08-01:2026-08-31"


def test_crawl_top_up_cut_short_finishes_on_a_later_night_and_failures_hold_it_open():
    db = _db()
    _insert(db, [(5, "2026-07-01", "2026-07-31", "done", 10, 2)])
    crawl.set_meta(db, "top_up_finished", "2026-07-31")
    refuse_august = ScriptedClient({("2026-08-01:2026-08-31", 1): crawl.PageError(400, "bad request")})

    crawl.run_crawl(db, lambda: refuse_august, genres=[5], top_up_when_done=True, today=TODAY)
    assert crawl.get_meta(db, "top_up_finished") == "2026-07-31"   # August failed: not finished

    tomorrow = TODAY + dt.timedelta(days=1)
    crawl.run_crawl(db, lambda: FakeClient(), genres=[5], top_up_when_done=False, today=tomorrow,
                    now=lambda: dt.datetime(2026, 10, 15, 22, 0))
    assert _state(db, "2026-08-01")[0] == "done"                   # retried on the next night
    assert crawl.get_meta(db, "top_up_finished") == "2026-10-14"   # the top-up's own day


def test_crawl_plist_install_notes_create_the_log_directory():
    import os
    import plistlib
    import re

    path = os.path.join(os.path.dirname(__file__), "..", "com.openclaw.tunefinder-dictionary.plist")
    with open(path, "rb") as f:
        raw = f.read()
    plist = plistlib.loads(raw)
    log_dirs = {os.path.dirname(plist["StandardOutPath"]), os.path.dirname(plist["StandardErrorPath"])}
    made = set(re.findall(r"mkdir -p (\S+)", raw.decode()))
    assert log_dirs <= made
