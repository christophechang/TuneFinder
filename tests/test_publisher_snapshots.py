"""Tests for the publisher's own files under data/pool/ (src/publisher/snapshots.py)."""
import os
from datetime import datetime, timedelta, timezone

from src.publisher.snapshots import (
    HEALTH_FILE,
    SNAPSHOTS,
    append_health,
    load_health,
    load_snapshot,
    pool_dir,
    prune_snapshots,
    write_snapshot,
)


def _run(run_id="2026-09-06T06:00:00Z-a3f9c1", **overrides):
    run = {
        "run_id": run_id,
        "started_at": run_id[:20],
        "taxonomy_version": 1,
        "identity_version": 1,
        "items": [{"key_v2": "a||b", "family": "dnb"}],
        "batches": 1,
        "per_source": {"beatport": {"count": 1, "error": None}},
        "targets": {
            "dev": {
                "acks": {"1": {"upserted": 1}},
                "artists": {"dnb": {"ok": True}},
                "manifest": {"ok": True},
                "error": None,
            }
        },
    }
    run.update(overrides)
    return run


def test_write_and_load_roundtrip_gzip(tmp_path):
    pd = pool_dir(str(tmp_path))
    run = _run()

    path = write_snapshot(pd, run)

    assert path == os.path.join(pd, SNAPSHOTS, f"{run['run_id']}.json.gz")
    assert os.path.exists(path)
    # Actually gzip-compressed, not a plain file with a misleading extension.
    with open(path, "rb") as f:
        assert f.read(2) == b"\x1f\x8b"

    loaded = load_snapshot(pd, run["run_id"])
    assert loaded == run


def test_load_snapshot_missing_raises_file_not_found(tmp_path):
    pd = pool_dir(str(tmp_path))
    try:
        load_snapshot(pd, "nope-not-here")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_write_is_atomic_no_tmp_left(tmp_path):
    pd = pool_dir(str(tmp_path))
    run = _run()

    write_snapshot(pd, run)

    names = os.listdir(os.path.join(pd, SNAPSHOTS))
    assert names == [f"{run['run_id']}.json.gz"]
    assert not any(name.startswith(".tmp-") for name in names)


def test_prune_by_run_id_date_not_mtime(tmp_path):
    pd = pool_dir(str(tmp_path))
    old_run = _run(run_id="2026-01-01T00:00:00Z-oldold")
    recent_run = _run(run_id="2026-09-05T00:00:00Z-recent")

    old_path = write_snapshot(pd, old_run)
    recent_path = write_snapshot(pd, recent_run)

    # Prove pruning reads the name, not mtime: touch the OLD file's mtime to
    # "now" so a mtime-based prune would (wrongly) keep it.
    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    fresh_ts = now.timestamp()
    os.utime(old_path, (fresh_ts, fresh_ts))

    deleted = prune_snapshots(pd, retention_days=45, now=now)

    assert deleted == 1
    assert not os.path.exists(old_path)
    assert os.path.exists(recent_path)


def test_prune_ignores_unparseable_names(tmp_path):
    pd = pool_dir(str(tmp_path))
    snapshots_dir = os.path.join(pd, SNAPSHOTS)
    os.makedirs(snapshots_dir, exist_ok=True)
    stray = os.path.join(snapshots_dir, "not-a-run-id.json.gz")
    with open(stray, "wb") as f:
        f.write(b"\x1f\x8b")

    deleted = prune_snapshots(pd, retention_days=1, now=datetime(2026, 9, 6, tzinfo=timezone.utc))

    assert deleted == 0
    assert os.path.exists(stray)


def test_health_appends_and_keeps_26(tmp_path):
    pd = pool_dir(str(tmp_path))
    assert load_health(pd) == []

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(30):
        run_id = (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ") + f"-r{i:02d}"
        append_health(pd, run_id, run_id[:20], {"beatport": {"count": i, "error": None}})

    entries = load_health(pd)

    assert len(entries) == 26
    # Newest last, oldest 4 dropped (30 appended, keep the newest 26 => start at i=4).
    assert entries[0]["run_id"].endswith("-r04")
    assert entries[-1]["run_id"].endswith("-r29")
    assert entries[-1]["per_source"] == {"beatport": {"count": 29, "error": None}}


def test_pool_dir_created_under_data_dir_only(tmp_path):
    data_dir = str(tmp_path)
    pd = pool_dir(data_dir)

    run = _run()
    write_snapshot(pd, run)
    append_health(pd, run["run_id"], run["run_id"][:20], run["per_source"])
    load_health(pd)
    load_snapshot(pd, run["run_id"])
    prune_snapshots(pd, retention_days=45, now=datetime(2026, 9, 6, tzinfo=timezone.utc))

    assert os.listdir(data_dir) == ["pool"]
    assert HEALTH_FILE in os.listdir(pd)
    assert SNAPSHOTS in os.listdir(pd)
