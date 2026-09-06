"""The publisher's own files under data/pool/ — never TuneFinder's
data_dir stores, never data/, logs/ or fixtures/ directly.

Two kinds of file:

- a gzip snapshot per run (`pool/snapshots/<run_id>.json.gz`) holding the
  built corpus and, as the run progresses, every API acknowledgement —
  written atomically the same way `src.pipeline.storage` writes the other
  JSON stores (temp file in the target directory, then `os.replace`);
- a rolling health log (`pool/health.json`), the publisher's analogue of
  `src.pipeline.source_health`'s `source_health.json`, keeping the newest
  `_RETENTION` runs.

`prune_snapshots` reads the run's `started_at` from the file name — never the
file's mtime, which a copy, a restore or a slow disk can all disturb.
"""
import gzip
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from src.logger import get_logger
from src.pipeline.storage import atomic_write_json

logger = get_logger(__name__)

SNAPSHOTS = "snapshots"
HEALTH_FILE = "health.json"

# Matches source_health.py's _RETENTION (26 weekly runs) — this is a
# different file (pool/health.json), kept at the same depth for the same
# reason: enough history for a trend, small enough to read whole.
_RETENTION = 26

# The first 20 characters of "<run_id>.json.gz" are the started_at ISO
# timestamp the publisher minted the run with (README "Run ids and
# ordering"): "YYYY-MM-DDTHH:MM:SSZ".
_STARTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STARTED_AT_LEN = 20
_STARTED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def pool_dir(data_dir: str) -> str:
    """<data_dir>/pool, created on demand. The only path this module ever
    writes under, directly or via its subdirectories."""
    path = os.path.join(data_dir, "pool")
    os.makedirs(path, exist_ok=True)
    return path


def _snapshots_dir(pool_dir_path: str) -> str:
    path = os.path.join(pool_dir_path, SNAPSHOTS)
    os.makedirs(path, exist_ok=True)
    return path


def _snapshot_path(pool_dir_path: str, run_id: str) -> str:
    return os.path.join(pool_dir_path, SNAPSHOTS, f"{run_id}.json.gz")


def write_snapshot(pool_dir_path: str, run: dict) -> str:
    """Write `run` as pool/snapshots/<run_id>.json.gz, atomically.

    Mirrors atomic_write_json's temp-file-then-os.replace pattern (same
    directory, so the replace stays on one filesystem), with the payload
    gzip-compressed instead of written plain.
    """
    directory = _snapshots_dir(pool_dir_path)
    path = os.path.join(directory, f"{run['run_id']}.json.gz")
    payload = json.dumps(run, ensure_ascii=False).encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json.gz")
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def load_snapshot(pool_dir_path: str, run_id: str) -> dict:
    """Raises FileNotFoundError when the run has no snapshot."""
    path = _snapshot_path(pool_dir_path, run_id)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _parse_started_at(filename: str) -> datetime | None:
    if not filename.endswith(".json.gz"):
        return None
    prefix = filename[:_STARTED_AT_LEN]
    if not _STARTED_AT_RE.match(prefix):
        return None
    try:
        return datetime.strptime(prefix, _STARTED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_snapshots(pool_dir_path: str, retention_days: int, now: datetime) -> int:
    """Delete snapshots whose run_id-derived started_at is older than
    retention_days relative to `now`. Files whose name does not parse as a
    run_id snapshot are left alone. Returns the count deleted."""
    directory = os.path.join(pool_dir_path, SNAPSHOTS)
    if not os.path.isdir(directory):
        return 0
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    deleted = 0
    for name in os.listdir(directory):
        started_at = _parse_started_at(name)
        if started_at is None:
            continue
        if started_at < cutoff:
            os.unlink(os.path.join(directory, name))
            deleted += 1
    if deleted:
        logger.info(f"[publisher] pruned {deleted} snapshot(s) older than {retention_days}d")
    return deleted


def _health_path(pool_dir_path: str) -> str:
    return os.path.join(pool_dir_path, HEALTH_FILE)


def load_health(pool_dir_path: str) -> list[dict]:
    path = _health_path(pool_dir_path)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_health(pool_dir_path: str, run_id: str, started_at: str, per_source: dict) -> None:
    """Append one run's health, newest last, keeping the newest _RETENTION."""
    entries = load_health(pool_dir_path)
    entries.append({"run_id": run_id, "started_at": started_at, "per_source": per_source})
    if len(entries) > _RETENTION:
        entries = entries[-_RETENTION:]
    atomic_write_json(_health_path(pool_dir_path), entries)
