"""Write-safety primitives for the JSON stores under data_dir.

Two concerns, one module:

- atomic_write_text / atomic_write_json — write to a temp file in the target
  directory then os.replace() over the destination, so a crash mid-write can
  never leave a truncated/corrupt store behind. os.replace is atomic on the
  POSIX filesystems this app runs on (macOS/Linux, same volume).

- run_lock — an inter-process advisory lock (fcntl.flock on
  data_dir/.tunefinder.lock) serialising pipeline runs and store mutations
  across the web service, the launchd weekly run, and manual CLI use. The
  JSON stores are whole-file read-modify-write with no transactions; the lock
  is what makes that safe once more than one process can touch data_dir.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator

LOCK_FILENAME = ".tunefinder.lock"


class RunLockHeldError(RuntimeError):
    """Another TuneFinder process holds the data_dir run lock."""


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write text to path atomically (temp file + os.replace)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, payload: Any, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Serialise payload as JSON and write it to path atomically."""
    atomic_write_text(path, json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii))


@contextmanager
def run_lock(data_dir: str, blocking: bool = False) -> Iterator[None]:
    """Hold the data_dir run lock for the duration of the with-block.

    blocking=False (default) raises RunLockHeldError immediately when another
    process holds the lock — the caller decides how to report contention
    (CLI: clean error; API: 409). blocking=True waits.
    """
    os.makedirs(data_dir, exist_ok=True)
    lock_path = os.path.join(data_dir, LOCK_FILENAME)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except (BlockingIOError, OSError) as exc:
            raise RunLockHeldError(
                f"another TuneFinder run is in progress (lock: {lock_path})"
            ) from exc
        try:
            os.truncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
        except OSError:
            pass  # lock metadata is best-effort diagnostics only
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock
