"""Golden-fixture bundles — `run --dry-run --capture-bundle DIR` and
`replay --bundle DIR` (src/pipeline/bundle.py, docs/ops/capture-bundle.md).

The contract under test:
- a capture run writes only into DIR — the live data dir and config are
  byte-identical afterwards (the run lock file aside);
- the bundle holds every input the engine consumed, the learning state as
  loaded / updated / passed to scoring, and the report artifact;
- a replay reads only the bundle, takes its clock from the bundle, never
  fetches, writes only to --out, and reproduces the artifact byte for byte.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.config import load_settings
from src.models import PoolRecord, SourceItem, Track
from src.pipeline.bundle import frozen_clock, replay_bundle
from src.pipeline.storage import LOCK_FILENAME
from src.services.runs import WeeklyRunOptions, run_weekly
from tests.test_services_runs import _seed_learning_data

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real Settings over a temp config dir and a seeded temp data dir."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    with open(os.path.join(REPO_ROOT, "config", "settings.yaml")) as f:
        data = yaml.safe_load(f)
    data_dir = tmp_path / "data"
    data["data_dir"] = str(data_dir)
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(data))
    (config_dir / "aliases.yaml").write_text("Sully:\n  - Sully Bass\n")
    monkeypatch.setattr("src.config._CONFIG_PATH", str(config_dir / "settings.yaml"))
    monkeypatch.setattr("src.config._ALIASES_PATH", str(config_dir / "aliases.yaml"))

    data_dir.mkdir()
    _seed_learning_data(str(data_dir))
    _seed_pool(str(data_dir))
    # Stale cached profile state — a capture must not overwrite it.
    (data_dir / "artist_profiles.json").write_text(json.dumps({
        "Old Artist": {"name": "Old Artist", "play_count": 3, "genres_seen": ["dnb"],
                       "track_titles": ["x"], "recency_weighted_play_count": 1.5},
    }))
    (data_dir / "genre_affinity.json").write_text(json.dumps({"dnb": 1.0}))
    (data_dir / "known_tracks.json").write_text(json.dumps(["old artist - x"]))
    return {"settings": load_settings(), "data_dir": str(data_dir), "config_dir": str(config_dir),
            "tmp": tmp_path}


def _seed_pool(data_dir):
    from src.pipeline.pool import save_pool
    added = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    save_pool([
        PoolRecord(artist="Pool Artist", title="Held Over", link="https://example.com/p",
                   source="bandcamp", label="Hospital", release_date=None, release_name=None,
                   genre_tags=["dnb"], raw_metadata={}, added_at=added, last_score=1.2),
    ], data_dir)


def _source_items():
    recent = (date.today() - timedelta(days=5)).isoformat()
    older = (date.today() - timedelta(days=21)).isoformat()
    return [
        SourceItem(source="beatport", artist="Sully", title="New Track", link="https://example.com/1",
                   label="Astrophonica", release_date=recent, genre_tags=["breaks"],
                   raw_metadata={"beatport_id": 42, "bpm": 140}),
        SourceItem(source="beatport", artist="LmArtist0", title="Fresh One", link="https://example.com/2",
                   label="Hospital", release_date=older, genre_tags=["dnb"],
                   raw_metadata={"beatport_id": 43, "bpm": 174}),
        SourceItem(source="bandcamp", artist="Somebody New", title="Café Dub", link="https://example.com/3",
                   label=None, release_date=recent, genre_tags=["dub"], raw_metadata={}),
        SourceItem(source="bandcamp", artist="Another", title="Tie A", link="https://example.com/4",
                   label="Tie", release_date=recent, genre_tags=["breaks"], raw_metadata={}),
        SourceItem(source="bandcamp", artist="Another", title="Tie B", link="https://example.com/5",
                   label="Tie", release_date=recent, genre_tags=["breaks"], raw_metadata={}),
    ]


_HEALTH = {"beatport": {"count": 2, "error": None}, "bandcamp": {"count": 3, "error": None}}


def _fetch_patches(catalog_fails=False):
    tracks_kwargs = ({"side_effect": RuntimeError("catalog down")} if catalog_fails
                     else {"return_value": [Track(artist="Sully", title="Old Track", recurrence_count=2,
                                                  genres_seen=["breaks"])]})
    return [
        patch("src.fetchers.catalog.fetch_all_tracks", **tracks_kwargs),
        patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]),
        patch("src.fetchers.fetch_all_sources", return_value=(_source_items(), _HEALTH)),
        patch("src.output.discord.make_discord_client", return_value=MagicMock()),
    ]


def _capture(live, bundle_dir, catalog_fails=False):
    patches = _fetch_patches(catalog_fails)
    for p in patches:
        p.start()
    try:
        return run_weekly(live["settings"], WeeklyRunOptions(dry_run=True, capture_dir=str(bundle_dir)))
    finally:
        for p in patches:
            p.stop()


def _snapshot(root, skip=()):
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name in skip:
                continue
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _no_network():
    """Every fetch entry point raises — a replay must not reach any of them."""
    boom = RuntimeError("replay must not fetch")
    return [
        patch("src.fetchers.catalog.fetch_all_tracks", side_effect=boom),
        patch("src.fetchers.catalog.fetch_all_mixes", side_effect=boom),
        patch("src.fetchers.fetch_all_sources", side_effect=boom),
    ]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def test_capture_writes_only_into_the_bundle(live):
    before_data = _snapshot(live["data_dir"], skip={LOCK_FILENAME})
    before_config = _snapshot(live["config_dir"])
    bundle_dir = live["tmp"] / "bundle"

    outcome = _capture(live, bundle_dir)

    assert outcome.artifact is not None
    assert _snapshot(live["data_dir"], skip={LOCK_FILENAME}) == before_data
    assert _snapshot(live["config_dir"]) == before_config
    assert not os.path.exists(os.path.join(live["data_dir"], "archive"))
    assert not os.path.exists(os.path.join(live["data_dir"], "source_items.json"))
    # Nothing outside the live dirs and the bundle appeared either.
    assert sorted(os.listdir(live["tmp"])) == ["bundle", "config", "data"]


def test_capture_bundle_holds_every_input(live):
    bundle_dir = live["tmp"] / "bundle"
    outcome = _capture(live, bundle_dir)
    names = set(os.listdir(bundle_dir))

    for name in (
        "settings.yaml", "aliases.yaml", "feedback.json", "recommendation_history.json",
        "candidate_pool.json", "known_tracks.json",
        "artist_profiles.json", "genre_affinity.json", "source_items.json",
        "fetcher_health.json", "learning.json", "manifest.json", "report_artifact.json",
    ):
        assert name in names, name
    # An input absent from the live dir is absent from the bundle too.
    assert "mix_prep_history.json" not in names
    assert "learned_weights.json" not in names
    # Fresh profile state (catalog succeeded) is what landed in the bundle.
    profiles = json.loads((bundle_dir / "artist_profiles.json").read_text())
    assert "Sully" in profiles and "Old Artist" not in profiles

    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["kind"] == "weekly"
    assert manifest["report_id"] == outcome.report_id
    assert manifest["used_fallback"] is False
    assert datetime.fromisoformat(manifest["clock"]).tzinfo is not None
    assert "sha" in manifest["engine_commit"] and "dirty" in manifest["engine_commit"]

    learning = json.loads((bundle_dir / "learning.json").read_text())
    assert set(learning) >= {"loaded", "updated", "multipliers", "tune_data", "adjustments"}
    assert learning["loaded"] == {}               # no learned_weights.json on disk yet
    assert learning["updated"]                    # the seeded marks moved something
    assert learning["multipliers"]                # ... and scoring was handed it

    stored = json.loads((bundle_dir / "report_artifact.json").read_text())
    assert stored == outcome.artifact
    assert stored["generated_at"] == manifest["clock"]


def test_capture_refuses_a_live_run(live):
    with pytest.raises(ValueError, match="dry run"):
        run_weekly(live["settings"], WeeklyRunOptions(dry_run=False, capture_dir=str(live["tmp"] / "b")))


def test_capture_refuses_a_non_empty_dir(live):
    bundle_dir = live["tmp"] / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "leftover").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        run_weekly(live["settings"], WeeklyRunOptions(dry_run=True, capture_dir=str(bundle_dir)))


def test_capture_degraded_profile_path_bundles_the_cached_state(live):
    bundle_dir = live["tmp"] / "bundle"
    before_data = _snapshot(live["data_dir"], skip={LOCK_FILENAME})

    _capture(live, bundle_dir, catalog_fails=True)

    assert _snapshot(live["data_dir"], skip={LOCK_FILENAME}) == before_data
    profiles = json.loads((bundle_dir / "artist_profiles.json").read_text())
    assert list(profiles) == ["Old Artist"]
    assert json.loads((bundle_dir / "manifest.json").read_text())["used_fallback"] is True
    assert replay_bundle(str(bundle_dir)).match


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def test_replay_reproduces_the_artifact_byte_for_byte(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)

    result = replay_bundle(str(bundle_dir))

    assert result.match, result.mismatches
    assert result.artifact_bytes == (bundle_dir / "report_artifact.json").read_bytes()


def test_replay_takes_its_clock_from_the_bundle(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)

    # Ten days on, the release window, pool age and generated_at would all move.
    with frozen_clock(datetime.now(timezone.utc) + timedelta(days=10)):
        result = replay_bundle(str(bundle_dir))

    assert result.match, result.mismatches


def test_replay_never_fetches_and_writes_only_to_out(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)
    shutil.rmtree(live["data_dir"])      # the live dir is not needed, and must not reappear
    before = _snapshot(bundle_dir)
    out = live["tmp"] / "replayed.json"

    patches = _no_network()
    for p in patches:
        p.start()
    try:
        result = replay_bundle(str(bundle_dir), out_path=str(out))
    finally:
        for p in patches:
            p.stop()

    assert result.match, result.mismatches
    assert _snapshot(bundle_dir) == before
    assert not os.path.exists(live["data_dir"])
    assert out.read_bytes() == result.artifact_bytes


def test_replay_reports_a_tampered_bundle(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)
    items = json.loads((bundle_dir / "source_items.json").read_text())
    items[0]["title"] = "Something Else"
    (bundle_dir / "source_items.json").write_text(json.dumps(items))

    result = replay_bundle(str(bundle_dir))

    assert not result.match
    assert any("report_artifact" in m for m in result.mismatches)


def test_replay_reports_a_learning_mismatch(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)
    learning = json.loads((bundle_dir / "learning.json").read_text())
    learning["multipliers"] = {"label_match": 9.0}
    (bundle_dir / "learning.json").write_text(json.dumps(learning))

    result = replay_bundle(str(bundle_dir))

    assert not result.match
    assert any("multipliers" in m for m in result.mismatches)


def test_cli_replay_in_a_fresh_process_matches(live):
    """A separate interpreter with a different hash seed — catches any output
    ordering that leans on set iteration, and proves the CLI reads nothing
    but the bundle (no config patching reaches the child)."""
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)
    env = dict(os.environ, PYTHONHASHSEED="12345")

    proc = subprocess.run(
        [sys.executable, "-m", "tunefinder", "replay", "--bundle", str(bundle_dir)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MATCH" in proc.stdout


# ---------------------------------------------------------------------------
# CLI argument rules
# ---------------------------------------------------------------------------

def _cli(*argv):
    return subprocess.run(
        [sys.executable, "-m", "tunefinder", *argv],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )


def test_cli_capture_bundle_requires_dry_run():
    proc = _cli("run", "--capture-bundle", "/nonexistent/bundle")
    assert proc.returncode != 0
    assert "--dry-run" in proc.stderr


def test_cli_replay_week_and_bundle_are_exclusive():
    proc = _cli("replay", "--week", "2026-W23", "--bundle", "/nonexistent/bundle")
    assert proc.returncode != 0
    assert "not allowed with" in proc.stderr


def test_cli_replay_bundle_rejects_overrides():
    proc = _cli("replay", "--bundle", "/nonexistent/bundle", "--set", "scoring.w_known_artist=2.0")
    assert proc.returncode != 0
    assert "--set" in proc.stderr


def test_cli_replay_mismatch_exits_non_zero(live):
    bundle_dir = live["tmp"] / "bundle"
    _capture(live, bundle_dir)
    artifact = json.loads((bundle_dir / "report_artifact.json").read_text())
    artifact["track_count"] += 1
    (bundle_dir / "report_artifact.json").write_text(json.dumps(artifact, indent=2))

    proc = _cli("replay", "--bundle", str(bundle_dir))

    assert proc.returncode == 1
    assert "MISMATCH" in proc.stdout


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

def test_frozen_clock_covers_module_and_function_local_imports():
    from src.pipeline import history
    instant = datetime(2026, 3, 4, 5, 6, 7, 890123, tzinfo=timezone.utc)

    with frozen_clock(instant):
        assert history.make_report_id() == "2026-W10"
        from datetime import datetime as local_dt   # a function-local import
        assert local_dt.now(timezone.utc) == instant
        assert isinstance(datetime(2020, 1, 1), local_dt)

    assert datetime.now(timezone.utc) > instant
    assert history.datetime is datetime
