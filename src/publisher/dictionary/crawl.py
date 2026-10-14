"""Beatport artist crawl: walk /v4/catalog/tracks by genre and date slice, newest
first, and keep every track's artist and remixer credits in a local SQLite file.

Moved here unchanged in behaviour from the standalone crawler that ran on the
mini (night 1: 10,862 requests, 0 x 429). What it keeps:

- **Never `order_by`.** Beatport's default order (id descending) pages cleanly;
  `order_by` does not (534 of 600 distinct in the probe).
- **Resumable.** Each page's rows and its slice cursor commit in one
  transaction, and the tables dedupe on track id, so a crash or a re-fetched
  page never double-counts.
- **Polite.** One request every `interval` seconds; a 429 honours Retry-After
  and doubles the interval for the rest of the run; three 429s in a row end the
  run until the next night. Network errors and 5xx back off and retry.
- **TuneFinder's token, under TuneFinder's lock**, so a refresh never races the
  publisher's or the Sunday run's.

What is new: the genre ids come from the vendored taxonomy (the same 29 the
standalone crawler hardcoded), the database path is a parameter, and
`top_up=True` re-walks the current and previous month once the backfill has no
slice left, so the nightly run picks up new releases for good.
"""
import datetime as dt
import os
import sqlite3
import time
from dataclasses import dataclass

import requests

from src.logger import get_logger
from src.publisher.dictionary import LOG
from src.publisher.pool_settings import _beatport_slug_to_id

logger = get_logger(__name__)

API = "https://api.beatport.com/v4/catalog/tracks/"
CAP = 10_000          # Beatport stops counting (and paging) here
PER_PAGE = 200
YEARS_BACK = 10

# TuneFinder's run lock, tried every 10 s for up to 10 minutes before giving up.
LOCK_TRIES = 60
LOCK_PAUSE_S = 10

# The standalone crawler's schema — the database on the mini carries on. Columns
# added since are created here for a new file and by `_migrate` for an old one.
SCHEMA = """
CREATE TABLE IF NOT EXISTS slices (
  genre_id INTEGER, start TEXT, end TEXT,
  state TEXT DEFAULT 'todo',            -- todo | split | done
  count INTEGER, next_page INTEGER DEFAULT 1,
  PRIMARY KEY (genre_id, start, end));
CREATE TABLE IF NOT EXISTS tracks (id INTEGER PRIMARY KEY, genre_id INTEGER, publish_date TEXT);
CREATE TABLE IF NOT EXISTS credits (track_id INTEGER, artist_id INTEGER, role TEXT,
  PRIMARY KEY (track_id, artist_id, role)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS artists (id INTEGER PRIMARY KEY, name TEXT, slug TEXT,
  seen TEXT);                           -- publish date of the credit the name came from
CREATE TABLE IF NOT EXISTS runs (started TEXT, ended TEXT, requests INTEGER, note TEXT);
"""


def genre_ids() -> list[int]:
    """The Beatport genre ids that feed the taxonomy's eight families — every
    `beatport.genres` row of the vendored taxonomy (excluded genres are listed
    apart and never crawled)."""
    return sorted(_beatport_slug_to_id().values())


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db) -> None:
    """Bring a database from the standalone crawler up to this schema."""
    with db:
        if "seen" not in _columns(db, "artists"):
            db.execute("ALTER TABLE artists ADD COLUMN seen TEXT")
            # The newest credit already stored, so the backfill walking older
            # months never puts an artist's old spelling back.
            db.execute(
                "UPDATE artists SET seen = m.seen FROM ("
                "  SELECT c.artist_id AS id, MAX(t.publish_date) AS seen FROM credits c "
                "  JOIN tracks t ON t.id = c.track_id GROUP BY c.artist_id) AS m "
                "WHERE m.id = artists.id"
            )


def months(today: dt.date):
    """(first day, last day) of each of the last YEARS_BACK * 12 months, newest
    first; the current month ends today."""
    first = today.replace(day=1)
    for i in range(YEARS_BACK * 12):
        y, m = divmod(first.year * 12 + first.month - 1 - i, 12)
        start = dt.date(y, m + 1, 1)
        ny, nm = divmod(y * 12 + m + 1, 12)
        end = dt.date(ny, nm + 1, 1) - dt.timedelta(days=1)
        yield start, min(end, today)


def seed(db, genres, today: dt.date) -> None:
    if db.execute("SELECT COUNT(*) FROM slices").fetchone()[0]:
        return
    rows = [(g, s.isoformat(), e.isoformat()) for s, e in months(today) for g in genres]
    db.executemany("INSERT INTO slices (genre_id, start, end) VALUES (?,?,?)", rows)
    db.commit()
    logger.info("%s seeded %d slices", LOG, len(rows))


def top_up_window(today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """The previous month, whole, and the current month up to today."""
    current = today.replace(day=1)
    previous_end = current - dt.timedelta(days=1)
    return [(previous_end.replace(day=1), previous_end), (current, today)]


def top_up(db, genres, today: dt.date) -> int:
    """Reset the top-up window to one fresh `todo` slice per genre and month.

    Every slice starting in the window goes — done, split or a child of a split,
    and the current month's old slice that ended on an earlier "today" — and the
    month slices come back from page 1. Tracks and credits stay: the tables
    dedupe on track id, so re-walking only adds what is new.
    """
    window = top_up_window(today)
    rows = [(g, s.isoformat(), e.isoformat()) for s, e in window for g in genres]
    with db:
        db.execute("DELETE FROM slices WHERE start >= ?", (window[0][0].isoformat(),))
        db.executemany("INSERT INTO slices (genre_id, start, end) VALUES (?,?,?)", rows)
    logger.info("%s top-up: %d slices from %s to %s", LOG, len(rows), window[0][0], today)
    return len(rows)


def beatport_token() -> tuple[str, str]:
    """TuneFinder's own Beatport token and user agent, taken under its run lock."""
    from src.config import load_settings
    from src.fetchers.beatport_auth import _UA, get_access_token
    from src.pipeline.storage import RunLockHeldError, run_lock

    settings = load_settings()
    for attempt in range(LOCK_TRIES):
        try:
            with run_lock(settings.data_dir):
                return get_access_token(settings), _UA
        except RunLockHeldError:
            if attempt + 1 < LOCK_TRIES:
                time.sleep(LOCK_PAUSE_S)
    raise RuntimeError("TuneFinder's lock stayed held for 10 minutes; not refreshing")


class Stop(Exception):
    """A clean end to the run: deadline, request budget, or Beatport asking us to stop."""


class Client:
    def __init__(self, interval, max_requests, token_fn=beatport_token,
                 session=None, sleep=time.sleep, clock=time.time):
        self.s = session or requests.Session()
        self.interval, self.max = interval, max_requests
        self.requests, self.streak, self.last = 0, 0, 0.0
        self.token_fn, self.sleep, self.clock = token_fn, sleep, clock
        self.auth()

    def auth(self):
        tok, ua = self.token_fn()
        self.s.headers.update({"Authorization": f"Bearer {tok}", "User-Agent": ua,
                               "Accept": "application/json"})

    def get(self, params):
        for attempt in range(6):
            if self.max and self.requests >= self.max:
                raise Stop("max requests")
            wait = self.last + self.interval - self.clock()
            if wait > 0:
                self.sleep(wait)
            self.last = self.clock()
            self.requests += 1
            try:
                r = self.s.get(API, params=params, timeout=60)
            except Exception as exc:
                logger.warning("%s network error %s; backing off", LOG, type(exc).__name__)
                self.sleep(30 * (attempt + 1))
                continue
            if r.status_code == 200:
                self.streak = 0
                return r.json()
            if r.status_code == 429:
                self.streak += 1
                ra = r.headers.get("Retry-After")
                pause = int(ra) if ra and ra.isdigit() else 60
                self.interval *= 2
                logger.warning("%s 429 #%d: Retry-After=%s, interval now %.1fs",
                               LOG, self.streak, ra, self.interval)
                if self.streak >= 3:
                    raise Stop("three 429s in a row")
                self.sleep(pause)
                continue
            if r.status_code == 401:
                logger.info("%s 401; refreshing token", LOG)
                self.auth()
                continue
            if r.status_code >= 500:
                logger.warning("%s %d; backing off", LOG, r.status_code)
                self.sleep(30 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        raise RuntimeError("gave up after six attempts")


# An artist seen again keeps the spelling from its newest credit: Beatport renames
# artists, and the backfill walks months newest first, so "last written" is not
# "newest". A credit with no date never overwrites a dated one.
UPSERT_ARTIST = (
    "INSERT INTO artists (id, name, slug, seen) VALUES (?,?,?,?) "
    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, slug = excluded.slug, seen = excluded.seen "
    "WHERE artists.seen IS NULL OR excluded.seen >= artists.seen"
)


def split(db, g, start, end) -> bool:
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if s >= e:
        logger.info("%s genre %s %s: one day over the cap; taking the first %d", LOG, g, start, CAP)
        return False
    mid = s + (e - s) // 2
    with db:
        db.execute("UPDATE slices SET state='split' WHERE genre_id=? AND start=? AND end=?", (g, start, end))
        db.executemany("INSERT OR IGNORE INTO slices (genre_id, start, end) VALUES (?,?,?)",
                       [(g, start, mid.isoformat()), (g, (mid + dt.timedelta(days=1)).isoformat(), end)])
    return True


def crawl_slice(db, c, g, start, end, page, deadline, now=dt.datetime.now):
    while True:
        if deadline and now() >= deadline:
            raise Stop("deadline")
        # Default order is id-descending and pages cleanly; order_by does not.
        d = c.get({"genre_id": g, "publish_date": f"{start}:{end}", "per_page": PER_PAGE,
                   "page": page})
        count = d.get("count") or 0
        if page == 1 and count >= CAP and split(db, g, start, end):
            return
        results = d.get("results") or []
        with db:
            for t in results:
                db.execute("INSERT OR IGNORE INTO tracks VALUES (?,?,?)", (t["id"], g, t.get("publish_date")))
                for role in ("artists", "remixers"):
                    for a in t.get(role) or []:
                        db.execute(UPSERT_ARTIST, (a["id"], a["name"], a.get("slug"), t.get("publish_date")))
                        db.execute("INSERT OR IGNORE INTO credits VALUES (?,?,?)", (t["id"], a["id"], role[:-1]))
            more = bool(d.get("next")) and results and page * PER_PAGE < min(count, CAP)
            db.execute("UPDATE slices SET count=?, next_page=?, state=? WHERE genre_id=? AND start=? AND end=?",
                       (count, page + 1, "todo" if more else "done", g, start, end))
        if not more:
            return
        page += 1


def counts(db) -> dict:
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "slices_done": q("SELECT COUNT(*) FROM slices WHERE state='done'"),
        "slices_todo": q("SELECT COUNT(*) FROM slices WHERE state='todo'"),
        "tracks": q("SELECT COUNT(*) FROM tracks"),
        "artists": q("SELECT COUNT(*) FROM artists"),
    }


def status(db) -> None:
    c = counts(db)
    two_plus = db.execute(
        "SELECT COUNT(*) FROM (SELECT artist_id FROM credits GROUP BY artist_id "
        "HAVING COUNT(DISTINCT track_id) >= 2)"
    ).fetchone()[0]
    print(f"slices done {c['slices_done']}, todo {c['slices_todo']}; tracks {c['tracks']}; "
          f"artists {c['artists']}; artists with 2+ tracks {two_plus}")
    for r in db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 5"):
        print(" run", r)


def deadline_for(until: str | None, now: dt.datetime) -> dt.datetime | None:
    """`HH:MM` local time as the next such moment after `now`."""
    if not until:
        return None
    h, m = map(int, until.split(":"))
    deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if deadline <= now:
        deadline += dt.timedelta(days=1)
    return deadline


@dataclass
class CrawlOutcome:
    requests: int
    note: str
    topped_up: bool
    error: bool

    def summary_line(self, db) -> str:
        c = counts(db)
        return (f"crawl: {self.note} after {self.requests} requests"
                f"{' (top-up)' if self.topped_up else ''}; slices done {c['slices_done']}, "
                f"todo {c['slices_todo']}; tracks {c['tracks']}; artists {c['artists']}")


def run_crawl(db, make_client, genres=None, deadline=None, top_up_when_done=False,
              today=None, now=dt.datetime.now) -> CrawlOutcome:
    """Walk `todo` slices newest first until none is left, the deadline passes or
    Beatport asks us to stop. With `top_up_when_done`, the first time no slice is
    left the top-up window is reset and walked too — once per run."""
    genres = genre_ids() if genres is None else genres
    today = today or dt.date.today()
    seed(db, genres, today)
    started, note, c, topped, error = now().isoformat(timespec="seconds"), "finished", None, False, False
    try:
        c = make_client()
        while True:
            row = db.execute("SELECT genre_id, start, end, next_page FROM slices WHERE state='todo' "
                             "ORDER BY start DESC, genre_id LIMIT 1").fetchone()
            if row is None:
                if top_up_when_done and not topped:
                    top_up(db, genres, today)
                    topped = True
                    continue
                logger.info("%s every slice done", LOG)
                break
            crawl_slice(db, c, *row, deadline, now=now)
    except Stop as stop:
        note = str(stop)
    except Exception as exc:
        note, error = f"error: {exc!r}", True
        logger.error("%s %s", LOG, note)
    finally:
        n = c.requests if c else 0
        db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                   (started, now().isoformat(timespec="seconds"), n, note))
        db.commit()
        logger.info("%s run ended (%s) after %d requests", LOG, note, n)
    return CrawlOutcome(requests=n, note=note, topped_up=topped, error=error)
