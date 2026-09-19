"""Golden-fixture bundles — `run --dry-run --capture-bundle DIR` and
`replay --bundle DIR` (docs/ops/capture-bundle.md).

A bundle freezes one weekly run at the replay boundary so another engine (the
multi-tenant product's .NET port) can be proven against it: every input the
engine consumed, the fetched corpus, the learning state as loaded / updated /
handed to scoring, the clock, the engine commit, and the report artifact.

Capture copies each data-dir input into DIR as the run takes the lock and
then points the engine at DIR, so the bundle is exactly what was consumed and
every write the run would make (fresh profile state, source_items) lands in
DIR instead of the live data dir. The archive snapshot is skipped. The run
lock, SoundCloud/Beatport token refreshes and the log file are the only side
effects outside DIR.

Replay runs the same run_weekly code over the bundle alone, under the
bundle's clock, never fetching and never writing except to --out.

Neither mode changes a normal run: run_weekly takes a bundle only when asked.
"""
from __future__ import annotations

import datetime as _dt_module
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timezone
from typing import Optional

import yaml

from src.config import Settings
from src.pipeline.report_artifact import artifact_json
from src.pipeline.storage import atomic_write_text

BUNDLE_FORMAT = 1

# Every data_dir file the weekly engine reads (the fetch and source-health
# aside). Filenames match the live data dir so the existing load_* functions
# read a bundle unchanged.
INPUT_FILES = (
    "feedback.json",
    "label_affinity.json",
    "recommendation_history.json",
    "mix_prep_history.json",
    "candidate_pool.json",
    "learned_weights.json",
    # Consumed only on the degraded path; overwritten in DIR by a fresh build.
    "artist_profiles.json",
    "genre_affinity.json",
    "known_tracks.json",
)
SETTINGS_FILE = "settings.yaml"
ALIASES_FILE = "aliases.yaml"
SOURCE_ITEMS_FILE = "source_items.json"
FETCHER_HEALTH_FILE = "fetcher_health.json"
LEARNING_FILE = "learning.json"
MANIFEST_FILE = "manifest.json"
ARTIFACT_FILE = "report_artifact.json"

_REAL_DATETIME = _dt_module.datetime


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

class _FrozenMeta(type):
    # Real datetimes stay instances of the patched name, so isinstance checks
    # in patched modules keep working.
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL_DATETIME)


def _frozen_datetime(instant):
    class FrozenDateTime(_REAL_DATETIME, metaclass=_FrozenMeta):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return instant.astimezone(timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    return FrozenDateTime


def _is_own_module(name: str) -> bool:
    return name in ("src", "tunefinder") or name.startswith(("src.", "tunefinder."))


@contextmanager
def frozen_clock(instant):
    """Every datetime.now() in this codebase returns `instant` inside the block.

    Covers module-level `from datetime import datetime` (rebinds the name in
    each loaded src.*/tunefinder.* module) and function-local imports
    (rebinds datetime.datetime itself). Only bundle capture and replay use it.
    """
    instant = _REAL_DATETIME.fromisoformat(instant.isoformat())  # a plain datetime
    frozen = _frozen_datetime(instant)
    previous = _dt_module.datetime
    saved = []
    for name, mod in list(sys.modules.items()):
        if mod is None or not _is_own_module(name):
            continue
        current = getattr(mod, "datetime", None)
        if isinstance(current, type) and issubclass(current, _REAL_DATETIME):
            saved.append((mod, current))
    _dt_module.datetime = frozen
    for mod, _ in saved:
        mod.datetime = frozen
    try:
        yield
    finally:
        _dt_module.datetime = previous
        for mod, original in saved:
            mod.datetime = original
        # A module first imported inside the block bound the frozen class.
        for name, mod in list(sys.modules.items()):
            if mod is not None and _is_own_module(name) and getattr(mod, "datetime", None) is frozen:
                mod.datetime = previous


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

def bundle_settings(bundle_dir: str) -> Settings:
    """The bundle's settings, with the engine's data dir pointed at the bundle."""
    with open(os.path.join(bundle_dir, SETTINGS_FILE), "r") as f:
        data = yaml.safe_load(f) or {}
    data["data_dir"] = bundle_dir
    return Settings(data, aliases_path=os.path.join(bundle_dir, ALIASES_FILE))


def _json_normal(value):
    return json.loads(json.dumps(value))


def _write_json(path: str, value) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))


def _engine_commit() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {"sha": None, "dirty": None}
    return {"sha": sha, "dirty": bool(status)}


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class CaptureBundle:
    capture = True
    replay = False

    def __init__(self, bundle_dir: str):
        if os.path.exists(bundle_dir) and (not os.path.isdir(bundle_dir) or os.listdir(bundle_dir)):
            raise ValueError(f"capture bundle dir must be empty or absent: {bundle_dir}")
        self.dir = bundle_dir
        self.clock = _dt_module.datetime.now(timezone.utc)
        self.notes: dict = {}
        self.settings: Optional[Settings] = None

    def begin(self, live_settings) -> Settings:
        """Under the run lock: copy config and every data input into DIR and
        return settings that point the engine at DIR."""
        from src import config

        os.makedirs(self.dir, exist_ok=True)
        settings_copy = os.path.join(self.dir, SETTINGS_FILE)
        shutil.copyfile(config._CONFIG_PATH, settings_copy)
        with open(settings_copy, "r") as f:
            if (yaml.safe_load(f) or {}) != live_settings._data:
                raise ValueError(f"loaded settings differ from {config._CONFIG_PATH} — cannot bundle them")
        if os.path.exists(config._ALIASES_PATH):
            shutil.copyfile(config._ALIASES_PATH, os.path.join(self.dir, ALIASES_FILE))
        for name in INPUT_FILES:
            src = os.path.join(live_settings.data_dir, name)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(self.dir, name))
        self.settings = bundle_settings(self.dir)
        return self.settings

    def note(self, **values) -> None:
        self.notes.update(values)

    def record_learning(self, updated, multipliers, tune, adjustments) -> None:
        from src.pipeline.learning import load_learned_weights

        _write_json(os.path.join(self.dir, LEARNING_FILE), {
            "loaded": load_learned_weights(self.dir),   # the bundled file, as run_weekly read it
            "updated": updated,
            "multipliers": multipliers,
            "tune_data": tune,
            "adjustments": adjustments,
        })

    def record_sources(self, source_items, fetcher_health) -> None:
        from src.fetchers import save_source_items

        save_source_items(source_items, self.dir)
        _write_json(os.path.join(self.dir, FETCHER_HEALTH_FILE), fetcher_health)

    def finish(self, outcome) -> None:
        if outcome.artifact is not None:
            atomic_write_text(os.path.join(self.dir, ARTIFACT_FILE), artifact_json(outcome.artifact))
        manifest = {
            "format": BUNDLE_FORMAT,
            "kind": "weekly",
            "clock": self.clock.isoformat(),
            "report_id": outcome.report_id,
            "engine_commit": _engine_commit(),
            "no_candidates": outcome.no_candidates,
            **self.notes,
        }
        manifest["files"] = sorted(os.listdir(self.dir)) + [MANIFEST_FILE]
        _write_json(os.path.join(self.dir, MANIFEST_FILE), manifest)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class ReplayBundle:
    capture = False
    replay = True

    def __init__(self, bundle_dir: str):
        self.dir = bundle_dir
        with open(os.path.join(bundle_dir, MANIFEST_FILE), "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        if self.manifest.get("format") != BUNDLE_FORMAT or self.manifest.get("kind") != "weekly":
            raise ValueError(f"not a format-{BUNDLE_FORMAT} weekly bundle: {bundle_dir}")
        self.clock = _REAL_DATETIME.fromisoformat(self.manifest["clock"])
        self.settings = bundle_settings(bundle_dir)
        self.mismatches: list[str] = []

    def begin(self, live_settings) -> Settings:
        return self.settings

    def note(self, **values) -> None:
        return None

    def load_profile_state(self, remix_aware):
        from src.pipeline.feedback import feedback_known_keys, load_feedback
        from src.pipeline.profile import load_artist_profiles, load_genre_affinity, load_known_tracks

        fb_keys = feedback_known_keys(load_feedback(self.dir), remix_aware)
        return (
            load_artist_profiles(self.dir),
            load_genre_affinity(self.dir),
            load_known_tracks(self.dir) | fb_keys,
            bool(self.manifest.get("used_fallback")),
        )

    def load_sources(self):
        from src.fetchers import load_source_items

        with open(os.path.join(self.dir, FETCHER_HEALTH_FILE), "r", encoding="utf-8") as f:
            fetcher_health = json.load(f)
        return load_source_items(self.dir), fetcher_health

    def record_sources(self, source_items, fetcher_health) -> None:
        return None

    def record_learning(self, updated, multipliers, tune, adjustments) -> None:
        """Replay recomputes the learning update; it must equal what was captured."""
        from src.pipeline.learning import load_learned_weights

        with open(os.path.join(self.dir, LEARNING_FILE), "r", encoding="utf-8") as f:
            captured = json.load(f)
        replayed = {
            "loaded": load_learned_weights(self.dir),
            "updated": updated,
            "multipliers": multipliers,
            "tune_data": tune,
            "adjustments": adjustments,
        }
        for key, value in replayed.items():
            if _json_normal(value) != captured.get(key):
                self.mismatches.append(f"{LEARNING_FILE}: '{key}' differs from the captured value")


@dataclass
class ReplayResult:
    report_id: str
    artifact_bytes: bytes
    mismatches: list[str] = field(default_factory=list)

    @property
    def match(self) -> bool:
        return not self.mismatches

    @property
    def artifact_sha256(self) -> str:
        return hashlib.sha256(self.artifact_bytes).hexdigest()


def _first_difference(a: bytes, b: bytes) -> str:
    a_lines, b_lines = a.decode("utf-8").splitlines(), b.decode("utf-8").splitlines()
    for i, (x, y) in enumerate(zip(a_lines, b_lines), start=1):
        if x != y:
            return f"line {i}: captured {y.strip()!r}, replayed {x.strip()!r}"
    return f"lengths differ: captured {len(b_lines)} lines, replayed {len(a_lines)}"


def replay_bundle(bundle_dir: str, out_path: Optional[str] = None) -> ReplayResult:
    """Replay a captured weekly bundle and compare the artifact byte for byte.

    Reads only `bundle_dir`; writes only `out_path` when given.
    """
    from src.services.runs import replay_weekly

    bundle = ReplayBundle(bundle_dir)
    outcome = replay_weekly(bundle)
    mismatches = list(bundle.mismatches)

    if outcome.report_id != bundle.manifest.get("report_id"):
        mismatches.append(
            f"report_id: captured {bundle.manifest.get('report_id')!r}, replayed {outcome.report_id!r}"
        )
    artifact_bytes = artifact_json(outcome.artifact).encode("utf-8") if outcome.artifact else b""
    captured_path = os.path.join(bundle_dir, ARTIFACT_FILE)
    if not os.path.exists(captured_path):
        mismatches.append(f"{ARTIFACT_FILE}: none captured (the captured run had no candidates)")
    else:
        with open(captured_path, "rb") as f:
            captured = f.read()
        if captured != artifact_bytes:
            mismatches.append(f"{ARTIFACT_FILE}: differs — {_first_difference(artifact_bytes, captured)}")

    if out_path and outcome.artifact is not None:
        with open(out_path, "wb") as f:
            f.write(artifact_bytes)
    return ReplayResult(report_id=outcome.report_id, artifact_bytes=artifact_bytes, mismatches=mismatches)
