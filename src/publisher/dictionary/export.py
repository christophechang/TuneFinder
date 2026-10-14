"""crawl.db -> the dictionary's wire shards (plan "Interfaces -> Wire").

One entry per artist name: `[name, releases, last_seen, families_mask]`, where
`releases` is the number of distinct publish dates the name is credited on (as
artist or remixer), `last_seen` the latest one as "YYYY-MM", and the mask has
bit i set for the i-th family of the taxonomy (house, techno, electro,
drum-and-bass, breaks-uk-bass, uk-garage, electronica-downtempo,
hip-hop-rnb-soul — the order of tunefinder-multi-tenant's Taxonomy.FamilyOrder,
which is the vendored taxonomy.yaml's order). A track's families come from the
Beatport genre it was crawled under, through the taxonomy's Beatport rows.

Names are grouped by their cleaned spelling (trimmed, inner whitespace
collapsed), so Beatport's duplicate artist ids under one spelling become one
entry and no two entries are exactly alike. Spellings that differ only by case
or accent stay separate: the server dedupes by fold and keeps the spelling with
most releases. No threshold — every crawled name is kept.

Entries are sorted by folded name and cut into shards whose JSON body — as
`requests` will serialise it — is at most 1.5 MB and 40,000 entries; at most 64
shards. The version is the UTC minute of the export.
"""
import datetime as dt
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass

from src.logger import get_logger
from src.publisher.dictionary import LOG
from src.publisher.dictionary.crawl import top_up_window
from src.publisher.pool_settings import _beatport_slug_to_id
from src.publisher.taxonomy import Taxonomy, families_for, load_taxonomy

logger = get_logger(__name__)

SCHEMA_VERSION = 1
MAX_SHARD_BYTES = 1_500_000
MAX_SHARD_ENTRIES = 40_000
MAX_SHARDS = 64
# TasteRules.MaxNameLength, measured as .NET measures it: UTF-16 code units.
MAX_NAME_LENGTH = 100
VERSION_FORMAT = "%Y-%m-%dT%H:%MZ"

COMMIT_FILE = "commit.json"
_EXPORT_FILE = re.compile(r"shard-\d{3}\.json|commit\.json")


class ExportError(RuntimeError):
    """The crawl cannot be exported within the wire's limits."""


def clean_name(name) -> str | None:
    """The spelling sent: trimmed, inner whitespace collapsed; None when blank
    or longer than the API's name limit (a truncated name is another artist)."""
    if not name:
        return None
    cleaned = " ".join(str(name).split())
    if not cleaned or len(cleaned.encode("utf-16-le")) // 2 > MAX_NAME_LENGTH:
        return None
    return cleaned


def fold(name: str) -> str:
    """Names.Fold: decomposed, combining marks dropped, lower-cased, whitespace
    collapsed. Used to order the entries, never sent."""
    decomposed = unicodedata.normalize("NFD", name)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


def genre_family_masks(taxonomy: Taxonomy) -> dict[int, int]:
    """Beatport genre id -> the family bits its fine genres belong to."""
    order = list(taxonomy.families)
    slug_to_id = _beatport_slug_to_id()
    masks = {}
    for slug, fines in taxonomy.beatport_genres.items():
        mask = 0
        for family in families_for(fines, taxonomy):
            mask |= 1 << order.index(family)
        masks[slug_to_id[slug]] = mask
    return masks


def aggregate(db, taxonomy: Taxonomy) -> list[list]:
    """Every artist name with at least one dated credit, fold-sorted."""
    masks = genre_family_masks(taxonomy)
    db.create_function("tf_clean_name", 1, clean_name, deterministic=True)
    db.execute("DROP TABLE IF EXISTS temp.export_names")
    db.execute("CREATE TEMP TABLE export_names (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO temp.export_names SELECT id, tf_clean_name(name) FROM artists")
    rows = db.execute(
        "SELECT n.name, COUNT(DISTINCT substr(t.publish_date, 1, 10)), "
        "       MAX(substr(t.publish_date, 1, 10)), GROUP_CONCAT(DISTINCT t.genre_id) "
        "FROM credits c "
        "JOIN temp.export_names n ON n.id = c.artist_id "
        "JOIN tracks t ON t.id = c.track_id "
        "WHERE n.name IS NOT NULL AND t.publish_date IS NOT NULL AND t.publish_date != '' "
        "GROUP BY n.name"
    )
    entries = []
    for name, releases, last_date, genres in rows:
        mask = 0
        for genre in str(genres).split(","):
            mask |= masks.get(int(genre), 0)
        entries.append([name, releases, last_date[:7], mask])
    db.execute("DROP TABLE temp.export_names")

    keyed = [(fold(entry[0]), entry) for entry in entries]
    keyed = [pair for pair in keyed if pair[0]]
    keyed.sort(key=lambda pair: (pair[0], pair[1][0]))
    return [entry for _, entry in keyed]


def _body_bytes(value) -> int:
    # Exactly what requests sends for `json=`: json.dumps with its defaults.
    return len(json.dumps(value).encode("utf-8"))


def shard(entries: list[list], version: str) -> list[dict]:
    """Greedy, in order: a shard closes when the next entry would take its body
    over MAX_SHARD_BYTES or its entry count over MAX_SHARD_ENTRIES."""
    # The envelope at its widest (two-digit index and total), so the real body
    # never exceeds what was counted.
    envelope = _body_bytes({"schema_version": SCHEMA_VERSION, "version": version,
                            "index": MAX_SHARDS - 1, "total": MAX_SHARDS, "entries": []})
    groups, current, size = [], [], envelope
    for entry in entries:
        cost = _body_bytes(entry) + (2 if current else 0)  # ", " between items
        if current and (size + cost > MAX_SHARD_BYTES or len(current) >= MAX_SHARD_ENTRIES):
            groups.append(current)
            current, size, cost = [], envelope, _body_bytes(entry)
        current.append(entry)
        size += cost
    if current:
        groups.append(current)
    if len(groups) > MAX_SHARDS:
        raise ExportError(f"{len(entries)} entries need {len(groups)} shards; the limit is {MAX_SHARDS}")

    shards = [
        {"schema_version": SCHEMA_VERSION, "version": version, "index": index,
         "total": len(groups), "entries": group}
        for index, group in enumerate(groups)
    ]
    for payload in shards:
        if _body_bytes(payload) > MAX_SHARD_BYTES:  # a bug here, never a data problem
            raise ExportError(f"shard {payload['index']} is over {MAX_SHARD_BYTES} bytes")
    return shards


def crawled_through(db, today: dt.date) -> str:
    """The oldest month the crawl covers without a gap, counting back from the
    newest: months with no `todo` or `failed` slice left, stopping at the first
    one that still has work. The top-up window is always being re-walked, so its months
    never stop the count. Falls back to the current month before any month is
    complete."""
    window = {start.strftime("%Y-%m") for start, _ in top_up_window(today)}
    oldest = None
    for month, todo in db.execute(
        "SELECT substr(start, 1, 7) AS month, SUM(state IN ('todo', 'failed')) FROM slices "
        "GROUP BY month ORDER BY month DESC"
    ):
        if todo:
            if month in window:
                continue
            break
        oldest = month
    return oldest or today.strftime("%Y-%m")


@dataclass
class Export:
    version: str
    shards: list[dict]
    commit: dict

    def summary_line(self) -> str:
        total_bytes = sum(_body_bytes(payload) for payload in self.shards)
        return (f"export: version {self.version}, {self.commit['entry_count']} entries "
                f"in {len(self.shards)} shards ({total_bytes / 1e6:.1f} MB), "
                f"crawled through {self.commit['crawled_through']}")


def build_export(db, taxonomy: Taxonomy | None = None, now: dt.datetime | None = None) -> Export:
    taxonomy = taxonomy or load_taxonomy()
    now = now or dt.datetime.now(dt.timezone.utc)
    version = now.astimezone(dt.timezone.utc).strftime(VERSION_FORMAT)
    entries = aggregate(db, taxonomy)
    if not entries:
        raise ExportError("crawl.db holds no dated artist credits")
    shards = shard(entries, version)
    commit = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "total": len(shards),
        "entry_count": len(entries),
        "crawled_through": crawled_through(db, now.astimezone().date()),
    }
    return Export(version=version, shards=shards, commit=commit)


def shard_file(index: int) -> str:
    return f"shard-{index:03d}.json"


def _check_clearable(path: str) -> None:
    """Refuse to delete `path` unless it holds nothing but an export's own files —
    a mistyped --export-dir must never cost anyone a directory."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        raise ExportError(f"{path} is not an export directory; not replacing it")
    strangers = [name for name in os.listdir(path)
                 if not _EXPORT_FILE.fullmatch(name)
                 or os.path.islink(os.path.join(path, name))
                 or not os.path.isfile(os.path.join(path, name))]
    if strangers:
        raise ExportError(f"{path} holds files that are not an export's "
                          f"({', '.join(sorted(strangers)[:5])}); not replacing it")


def write_export(export: Export, out_dir: str) -> None:
    """Replace `out_dir` with this export's shard files and commit.json, written
    beside it first so a crash never leaves a mixed export behind. Either
    directory is cleared only when it holds nothing but shard-NNN.json and
    commit.json files."""
    tmp = out_dir.rstrip(os.sep) + ".tmp"
    _check_clearable(out_dir)
    _check_clearable(tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for payload in export.shards:
        with open(os.path.join(tmp, shard_file(payload["index"])), "w", encoding="utf-8") as f:
            f.write(json.dumps(payload))
    with open(os.path.join(tmp, COMMIT_FILE), "w", encoding="utf-8") as f:
        f.write(json.dumps(export.commit))
    shutil.rmtree(out_dir, ignore_errors=True)
    os.rename(tmp, out_dir)
    logger.info("%s wrote %d shards to %s", LOG, len(export.shards), out_dir)
