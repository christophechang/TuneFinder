"""Write-safety primitives — src/pipeline/storage.py."""
import json
import os

import pytest

from src.pipeline.storage import (
    LOCK_FILENAME,
    RunLockHeldError,
    atomic_write_json,
    atomic_write_text,
    run_lock,
)


def test_atomic_write_json_creates_file_and_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "store.json")
    atomic_write_json(path, {"a": 1})
    with open(path) as f:
        assert json.load(f) == {"a": 1}


def test_atomic_write_json_replaces_existing_content(tmp_path):
    path = str(tmp_path / "store.json")
    atomic_write_json(path, [1, 2, 3])
    atomic_write_json(path, {"replaced": True})
    with open(path) as f:
        assert json.load(f) == {"replaced": True}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = str(tmp_path / "store.json")
    atomic_write_text(path, "hello")
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")]
    assert leftovers == []
    assert (tmp_path / "store.json").read_text() == "hello"


def test_atomic_write_failure_keeps_previous_content(tmp_path):
    path = str(tmp_path / "store.json")
    atomic_write_json(path, {"stable": True})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(path, {"bad": Unserialisable()})

    with open(path) as f:
        assert json.load(f) == {"stable": True}
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")]
    assert leftovers == []


def test_run_lock_contention_raises(tmp_path):
    data_dir = str(tmp_path)
    with run_lock(data_dir):
        with pytest.raises(RunLockHeldError):
            with run_lock(data_dir):
                pass  # pragma: no cover


def test_run_lock_released_after_exit(tmp_path):
    data_dir = str(tmp_path)
    with run_lock(data_dir):
        pass
    with run_lock(data_dir):
        pass  # reacquire fine — no exception


def test_run_lock_creates_lock_file_in_data_dir(tmp_path):
    data_dir = str(tmp_path / "data")
    with run_lock(data_dir):
        assert os.path.exists(os.path.join(data_dir, LOCK_FILENAME))
