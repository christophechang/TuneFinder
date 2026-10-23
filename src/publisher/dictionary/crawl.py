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
- **Never stalls on one slice.** A page Beatport will not serve (a status that
  is not retried, a 5xx that outlasts its retries, a body that is not a page)
  marks its slice `failed` with the reason and the walk moves on; a failed
  slice gets one more try per later night. A 404 past page 1 ends a slice, and
  a single malformed record is skipped and counted.
- **TuneFinder's token, under TuneFinder's lock**, so a refresh never races the
  publisher's or the Sunday run's.

What is new: the genre ids come from the vendored taxonomy (the same 29 the
standalone crawler hardcoded), the database path is a parameter, and
`top_up=True` re-walks the current and previous month — and every month since
the last top-up that finished, so downtime leaves no gap — once the backfill has
no slice left, so the nightly run picks up new releases for good.
"""
import datetime as dt
import os
import sqlite3
import time
from dataclasses import dataclass, field

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
  state TEXT DEFAULT 'todo',            -- todo | split | done | failed
  count INTEGER, next_page INTEGER DEFAULT 1,
  error TEXT, failed_night TEXT,        -- why and when a slice last failed
  PRIMARY KEY (genre_id, start, end));
CREATE TABLE IF NOT EXISTS tracks (id INTEGER PRIMARY KEY, genre_id INTEGER, publish_date TEXT);
CREATE TABLE IF NOT EXISTS credits (track_id INTEGER, artist_id INTEGER, role TEXT,
  PRIMARY KEY (track_id, artist_id, role)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS artists (id INTEGER PRIMARY KEY, name TEXT, slug TEXT,
  seen TEXT);                           -- publish date of the credit the name came from
CREATE TABLE IF NOT EXISTS runs (started TEXT, ended TEXT, requests INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def get_meta(db, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key: str, value: str) -> None:
    with db:
        db.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def genre_ids() -> list[int]:
    """The Beatport genre ids that feed the taxonomy's nine families — every
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
        slice_columns = _columns(db, "slices")
        for column in ("error", "failed_night"):
            if column not in slice_columns:
                db.execute(f"ALTER TABLE slices ADD COLUMN {column} TEXT")
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


def top_up_window(today: dt.date, since: dt.date | None = None) -> list[tuple[dt.date, dt.date]]:
    """Whole months from the previous one — or from `since`'s month when that is
    older — through the current month up to today."""
    current = today.replace(day=1)
    first = (current - dt.timedelta(days=1)).replace(day=1)
    if since is not None:
        first = min(first, since.replace(day=1))
    window, month = [], first
    while month <= current:
        following = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        window.append((month, min(following - dt.timedelta(days=1), today)))
        month = following
    return window


def top_up_since(db) -> dt.date | None:
    """The day the crawl last covered completely: the last top-up that finished,
    else (no top-up has yet) the newest day any slice reaches — the seed's today."""
    finished = get_meta(db, "top_up_finished")
    if finished:
        return dt.date.fromisoformat(finished)
    newest = db.execute("SELECT MAX(end) FROM slices").fetchone()[0]
    return dt.date.fromisoformat(newest) if newest else None


def top_up(db, genres, today: dt.date) -> int:
    """Reset the top-up window to one fresh `todo` slice per genre and month.

    The window runs from the month of the last finished top-up (`top_up_since`),
    never less than the previous month, so nights the job did not run are
    walked too. Every slice starting in the window goes — done, split, failed or
    a child of a split, and the current month's old slice that ended on an
    earlier "today" — and the month slices come back from page 1. Tracks and
    credits stay: the tables dedupe on track id, so re-walking only adds what is
    new.
    """
    window = top_up_window(today, top_up_since(db))
    rows = [(g, s.isoformat(), e.isoformat()) for s, e in window for g in genres]
    with db:
        db.execute("DELETE FROM slices WHERE start >= ?", (window[0][0].isoformat(),))
        db.executemany("INSERT INTO slices (genre_id, start, end) VALUES (?,?,?)", rows)
    set_meta(db, "top_up_from", window[0][0].isoformat())
    set_meta(db, "top_up_to", today.isoformat())
    logger.info("%s top-up: %d slices from %s to %s", LOG, len(rows), window[0][0], today)
    return len(rows)


def finish_top_up(db) -> bool:
    """Once no `todo` slice is left, record the last top-up's day as finished — on
    whichever night its walk ended — unless a slice in its window failed, so the
    next window still reaches back over it."""
    since, through = get_meta(db, "top_up_from"), get_meta(db, "top_up_to")
    if since is None or through is None:
        return False
    failed = db.execute("SELECT COUNT(*) FROM slices WHERE state='failed' AND start >= ?",
                        (since,)).fetchone()[0]
    if failed:
        logger.warning("%s top-up left %d failed slices; the next one starts from %s again",
                       LOG, failed, since)
        return False
    set_meta(db, "top_up_finished", through)
    return True


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


class PageError(RuntimeError):
    """A page Beatport will not serve: a status that is never retried, a 5xx that
    outlasted every retry, or a 200 whose body is not JSON. The slice's problem,
    not the run's — unlike a network that stays down, which ends the run."""

    def __init__(self, status: int, reason: str):
        self.status = status
        super().__init__(f"HTTP {status}: {reason}")


class SliceFailed(Exception):
    """This slice cannot be walked tonight: the crawl marks it `failed` and moves on."""


class Client:
    def __init__(self, interval, max_requests, token_fn=beatport_token,
                 session=None, sleep=time.sleep, clock=time.time, deadline=None):
        self.s = session or requests.Session()
        self.interval, self.max = interval, max_requests
        self.deadline = deadline  # epoch seconds on `clock`; no wait ever runs past it
        self.requests, self.streak, self.last = 0, 0, 0.0
        self.token_fn, self.sleep, self.clock = token_fn, sleep, clock
        self.auth()

    def auth(self):
        tok, ua = self.token_fn()
        self.s.headers.update({"Authorization": f"Bearer {tok}", "User-Agent": ua,
                               "Accept": "application/json"})

    def pause(self, seconds):
        """Sleep, but never across the deadline: a wait that would reach it sleeps
        only the time left and ends the run cleanly."""
        if self.deadline is not None:
            left = self.deadline - self.clock()
            if seconds >= left:
                if left > 0:
                    self.sleep(left)
                raise Stop("deadline")
        self.sleep(seconds)

    def get(self, params):
        last_5xx = None
        for attempt in range(6):
            if self.max and self.requests >= self.max:
                raise Stop("max requests")
            wait = self.last + self.interval - self.clock()
            if wait > 0:
                self.pause(wait)
            self.last = self.clock()
            self.requests += 1
            try:
                r = self.s.get(API, params=params, timeout=60)
            except Exception as exc:
                last_5xx = None
                logger.warning("%s network error %s; backing off", LOG, type(exc).__name__)
                self.pause(30 * (attempt + 1))
                continue
            if r.status_code == 200:
                self.streak = 0
                try:
                    return r.json()
                except ValueError:
                    raise PageError(200, "body is not JSON") from None
            if r.status_code == 429:
                self.streak += 1
                ra = r.headers.get("Retry-After")
                pause = int(ra) if ra and ra.isdigit() else 60
                self.interval *= 2
                logger.warning("%s 429 #%d: Retry-After=%s, interval now %.1fs",
                               LOG, self.streak, ra, self.interval)
                if self.streak >= 3:
                    raise Stop("three 429s in a row")
                self.pause(pause)
                continue
            if r.status_code == 401:
                logger.info("%s 401; refreshing token", LOG)
                self.auth()
                continue
            if r.status_code >= 500:
                last_5xx = r.status_code
                logger.warning("%s %d; backing off", LOG, r.status_code)
                self.pause(30 * (attempt + 1))
                continue
            raise PageError(r.status_code, r.text[:200])
        if last_5xx is not None:
            raise PageError(last_5xx, "still failing after six attempts")
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


def _parse_track(t):
    """(track id, publish date, [(artist id, name, slug, role)]) — or None when the
    record is not the shape the crawl stores, so one bad row costs one row."""
    if not isinstance(t, dict) or type(t.get("id")) is not int:
        return None
    date = t.get("publish_date")
    if date is not None and not isinstance(date, str):
        return None
    credits = []
    for role in ("artists", "remixers"):
        people = t.get(role) or []
        if not isinstance(people, list):
            return None
        for a in people:
            if not isinstance(a, dict) or type(a.get("id")) is not int or not isinstance(a.get("name"), str):
                return None
            slug = a.get("slug")
            credits.append((a["id"], a["name"], slug if isinstance(slug, str) else None, role[:-1]))
    return t["id"], date, credits


def crawl_slice(db, c, g, start, end, page, deadline, now=dt.datetime.now, stats=None):
    """Walk one slice from `page` until it is done, split or the deadline passes.

    A page past the first that answers 404 ends the slice (Beatport ran out of
    pages before `next` said so). Any other page Beatport will not serve, or a
    page that is not the expected shape, raises SliceFailed. A single malformed
    track is skipped and counted in `stats["skipped"]`; every committed page
    counts in `stats["pages"]`."""
    stats = stats if stats is not None else {}
    where = (g, start, end)
    while True:
        if deadline and now() >= deadline:
            raise Stop("deadline")
        # Default order is id-descending and pages cleanly; order_by does not.
        try:
            d = c.get({"genre_id": g, "publish_date": f"{start}:{end}", "per_page": PER_PAGE,
                       "page": page})
        except PageError as exc:
            if exc.status == 404 and page > 1:
                logger.info("%s genre %s %s:%s page %d is 404; the slice is done", LOG, g, start, end, page)
                with db:
                    db.execute("UPDATE slices SET next_page=?, state='done' "
                               "WHERE genre_id=? AND start=? AND end=?", (page, *where))
                return
            raise SliceFailed(f"page {page}: {exc}") from exc
        count = d.get("count") if isinstance(d, dict) else None
        results = d.get("results") if isinstance(d, dict) else None
        if type(count or 0) is not int or not isinstance(results or [], list):
            raise SliceFailed(f"page {page}: not a page of tracks")
        count, results = count or 0, results or []
        if page == 1 and count >= CAP and split(db, g, start, end):
            return
        tracks = [_parse_track(t) for t in results]
        skipped = tracks.count(None)
        if skipped:
            stats["skipped"] = stats.get("skipped", 0) + skipped
            logger.warning("%s genre %s %s:%s page %d: skipped %d malformed records",
                           LOG, g, start, end, page, skipped)
        with db:
            for track in tracks:
                if track is None:
                    continue
                track_id, date, credits = track
                db.execute("INSERT OR IGNORE INTO tracks VALUES (?,?,?)", (track_id, g, date))
                for artist_id, name, slug, role in credits:
                    db.execute(UPSERT_ARTIST, (artist_id, name, slug, date))
                    db.execute("INSERT OR IGNORE INTO credits VALUES (?,?,?)", (track_id, artist_id, role))
            more = bool(d.get("next")) and results and page * PER_PAGE < min(count, CAP)
            db.execute("UPDATE slices SET count=?, next_page=?, state=? WHERE genre_id=? AND start=? AND end=?",
                       (count, page + 1, "todo" if more else "done", *where))
        stats["pages"] = stats.get("pages", 0) + 1
        if not more:
            return
        page += 1


def night_of(moment: dt.datetime) -> str:
    """The night a local moment belongs to, named by its evening's date: noon to
    noon, so 21:30 and the 05:15 after it are one night."""
    return (moment - dt.timedelta(hours=12)).date().isoformat()


def mark_failed(db, g, start, end, reason: str, night: str) -> None:
    with db:
        db.execute("UPDATE slices SET state='failed', error=?, failed_night=? "
                   "WHERE genre_id=? AND start=? AND end=?", (reason[:500], night, g, start, end))
    logger.error("%s genre %s %s:%s failed: %s", LOG, g, start, end, reason)


def retry_failed(db, night: str) -> int:
    """Put the slices that failed on an earlier night back in the queue — each
    failed slice gets one more try per night, never two."""
    with db:
        n = db.execute("UPDATE slices SET state='todo' WHERE state='failed' "
                       "AND (failed_night IS NULL OR failed_night < ?)", (night,)).rowcount
    if n:
        logger.info("%s retrying %d slices that failed on an earlier night", LOG, n)
    return n


def counts(db) -> dict:
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "slices_done": q("SELECT COUNT(*) FROM slices WHERE state='done'"),
        "slices_todo": q("SELECT COUNT(*) FROM slices WHERE state='todo'"),
        "slices_failed": q("SELECT COUNT(*) FROM slices WHERE state='failed'"),
        "tracks": q("SELECT COUNT(*) FROM tracks"),
        "artists": q("SELECT COUNT(*) FROM artists"),
    }


def status(db) -> None:
    c = counts(db)
    two_plus = db.execute(
        "SELECT COUNT(*) FROM (SELECT artist_id FROM credits GROUP BY artist_id "
        "HAVING COUNT(DISTINCT track_id) >= 2)"
    ).fetchone()[0]
    print(f"slices done {c['slices_done']}, todo {c['slices_todo']}, failed {c['slices_failed']}; "
          f"tracks {c['tracks']}; artists {c['artists']}; artists with 2+ tracks {two_plus}")
    for r in db.execute("SELECT genre_id, start, end, failed_night, error FROM slices "
                        "WHERE state='failed' ORDER BY start DESC, genre_id LIMIT 20"):
        print(" failed", r)
    for r in db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 5"):
        print(" run", r)


# The nightly window is 21:30 to 05:15; a deadline further off than this is a
# daytime run that would crawl all day.
MAX_DEADLINE_AHEAD = dt.timedelta(hours=9)


class DeadlineError(ValueError):
    """--until is malformed or further away than MAX_DEADLINE_AHEAD."""


def deadline_for(until: str | None, now: dt.datetime) -> dt.datetime | None:
    """`HH:MM` local time as the next such moment after `now` — refused when that
    is more than nine hours away, so a manual daytime run must name its own
    --until within nine hours rather than inherit the nightly 05:15."""
    if not until:
        return None
    try:
        h, m = map(int, until.split(":"))
        deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except ValueError:
        raise DeadlineError(f"--until {until!r} is not HH:MM") from None
    if deadline <= now:
        deadline += dt.timedelta(days=1)
    if deadline - now > MAX_DEADLINE_AHEAD:
        hours = (deadline - now).total_seconds() / 3600
        raise DeadlineError(f"--until {until} is {hours:.1f} h away; give an --until within "
                            f"{MAX_DEADLINE_AHEAD.total_seconds() / 3600:.0f} h of now")
    return deadline


@dataclass
class CrawlOutcome:
    requests: int
    note: str
    topped_up: bool
    error: bool
    pages: int = 0                                    # pages committed this run
    skipped: int = 0                                  # malformed records skipped
    failed: list[str] = field(default_factory=list)   # slices that failed this run, with why
    retried: int = 0                                  # failed slices given another try

    def summary_line(self, db) -> str:
        c = counts(db)
        return (f"crawl: {self.note} after {self.requests} requests"
                f"{' (top-up)' if self.topped_up else ''}; {self.pages} pages; slices done {c['slices_done']}, "
                f"todo {c['slices_todo']}, failed {c['slices_failed']} ({len(self.failed)} tonight, "
                f"{self.retried} retried); skipped records {self.skipped}; "
                f"tracks {c['tracks']}; artists {c['artists']}")


def run_crawl(db, make_client, genres=None, deadline=None, top_up_when_done=False,
              today=None, now=dt.datetime.now) -> CrawlOutcome:
    """Walk `todo` slices newest first until none is left, the deadline passes or
    Beatport asks us to stop. A slice that fails is marked `failed` and the walk
    moves on; slices that failed on an earlier night get one more try first.
    With `top_up_when_done`, the first time no `todo` slice is left — failed ones
    never hold it back — the top-up window is reset and walked too, once per run."""
    genres = genre_ids() if genres is None else genres
    today = today or dt.date.today()
    seed(db, genres, today)
    started, note, c, topped, error = now().isoformat(timespec="seconds"), "finished", None, False, False
    night = night_of(now())
    retried = retry_failed(db, night)
    stats, failed = {"pages": 0, "skipped": 0}, []
    try:
        c = make_client()
        while True:
            row = db.execute("SELECT genre_id, start, end, next_page FROM slices WHERE state='todo' "
                             "ORDER BY start DESC, genre_id LIMIT 1").fetchone()
            if row is None:
                finish_top_up(db)
                if top_up_when_done and not topped:
                    top_up(db, genres, today)
                    topped = True
                    continue
                logger.info("%s every slice done", LOG)
                break
            try:
                crawl_slice(db, c, *row, deadline, now=now, stats=stats)
            except SliceFailed as exc:
                mark_failed(db, *row[:3], str(exc), night)
                failed.append(f"genre {row[0]} {row[1]}:{row[2]} {exc}")
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
    return CrawlOutcome(requests=n, note=note, topped_up=topped, error=error, pages=stats["pages"],
                        skipped=stats["skipped"], failed=failed, retried=retried)
