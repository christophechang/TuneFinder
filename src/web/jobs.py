"""In-process job runner for on-demand pipeline runs.

One pipeline run at a time: the manager enforces it in-process (submit()
raises JobConflictError while a job is queued/running), and the data_dir
run lock in src/services/runs.py enforces it across processes — a
web-triggered run colliding with the Sunday launchd run fails cleanly and
the job records the lock error.

Job history (sans in-memory artifact and full log) persists to
data/web_jobs.json so the SPA's runs page survives service restarts. Any
job found "running" at load time was interrupted by a restart and is marked
failed.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.logger import get_logger
from src.pipeline.storage import atomic_write_json
from src.services.runs import (
    MIX_PREP_GENRES,
    MixPrepOptions,
    WeeklyRunOptions,
    run_mix_prep,
    run_weekly,
)

logger = get_logger(__name__)

_JOBS_FILE = "web_jobs.json"
_JOBS_RETAIN = 50
_LOG_TAIL_MAX = 400
_PERSISTED_LOG_LINES = 60


class JobConflictError(RuntimeError):
    """A run is already queued or running."""


class JobValidationError(ValueError):
    """Bad run parameters (unknown genre, malformed BPM range/key)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    mode: str                 # "weekly" | "mix-prep"
    params: dict
    dry_run: bool
    created_at: str
    status: str = "queued"    # queued | running | succeeded | failed
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    report_id: Optional[str] = None
    recommended_count: Optional[int] = None
    no_candidates: bool = False
    error: Optional[str] = None
    stages: list = field(default_factory=list)
    log_tail: deque = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_MAX))
    artifact: Optional[dict] = None   # in-memory only, dry runs — never persisted

    def summary(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "dry_run": self.dry_run,
            "params": self.params,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "report_id": self.report_id,
            "recommended_count": self.recommended_count,
            "no_candidates": self.no_candidates,
            "error": self.error,
        }

    def detail(self) -> dict:
        d = self.summary()
        d["stages"] = list(self.stages)
        d["log_tail"] = list(self.log_tail)
        d["artifact"] = self.artifact if (self.dry_run and self.status == "succeeded") else None
        return d


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self._job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._job.log_tail.append(self.format(record))
        except Exception:  # never let log capture break a run
            pass


def build_options(request: dict):
    """Validate a run request dict into (mode, options). Raises JobValidationError."""
    from src.pipeline.harmonic import to_camelot

    mode = request.get("mode")
    dry_run = bool(request.get("dry_run", False))

    if mode == "weekly":
        return "weekly", WeeklyRunOptions(dry_run=dry_run)

    if mode in ("mix-prep", "free-downloads"):
        genre = request.get("genre")
        if genre not in MIX_PREP_GENRES:
            raise JobValidationError(
                f"unknown genre {genre!r} — valid: {', '.join(MIX_PREP_GENRES)}"
            )
        bpm_min, bpm_max = request.get("bpm_min"), request.get("bpm_max")
        bpm_range = None
        if bpm_min is not None or bpm_max is not None:
            if bpm_min is None or bpm_max is None:
                raise JobValidationError("bpm_min and bpm_max must be provided together")
            if bpm_min < 0 or bpm_min > bpm_max:
                raise JobValidationError(
                    f"invalid BPM range {bpm_min:g}-{bpm_max:g} — min must be <= max and non-negative"
                )
            bpm_range = (float(bpm_min), float(bpm_max))
        key_camelot = None
        key_arg = request.get("key")
        if key_arg:
            key_camelot = to_camelot(key_arg)
            if key_camelot is None:
                raise JobValidationError(
                    f"could not parse key {key_arg!r} — use Camelot notation (e.g. 8A) "
                    "or a musical key (e.g. Am, C major)"
                )
        return mode, MixPrepOptions(
            genre=genre, bpm_range=bpm_range, key_camelot=key_camelot,
            bpm_flex=bool(request.get("bpm_flex", True)), dry_run=dry_run,
            free_only=(mode == "free-downloads"),
        )

    raise JobValidationError(f"unknown mode {mode!r} — valid: weekly, mix-prep, free-downloads")


class JobManager:
    def __init__(self, settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # newest first
        self._load_persisted()

    # --- public API ---

    def submit(self, request: dict) -> Job:
        mode, options = build_options(request)
        with self._lock:
            active = self._active_job_locked()
            if active is not None:
                raise JobConflictError(
                    f"job {active.id} is already {active.status} — one run at a time"
                )
            job = Job(
                id=f"{mode}-{uuid.uuid4().hex[:8]}",
                mode=mode,
                params={k: v for k, v in request.items() if k not in ("mode", "dry_run") and v is not None},
                dry_run=options.dry_run,
                created_at=_now_iso(),
            )
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
            self._trim_locked()
        thread = threading.Thread(target=self._execute, args=(job, options), daemon=True,
                                  name=f"tunefinder-job-{job.id}")
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[Job]:
        with self._lock:
            return [self._jobs[jid] for jid in self._order[:limit]]

    def active_job_id(self) -> Optional[str]:
        with self._lock:
            active = self._active_job_locked()
            return active.id if active else None

    # --- internals ---

    def _active_job_locked(self) -> Optional[Job]:
        for jid in self._order:
            if self._jobs[jid].status in ("queued", "running"):
                return self._jobs[jid]
        return None

    def _execute(self, job: Job, options) -> None:
        job.status = "running"
        job.started_at = _now_iso()

        def progress(stage: str, detail: str) -> None:
            job.stages.append({"stage": stage, "detail": detail, "at": _now_iso()})

        handler = _JobLogHandler(job)
        root = logging.getLogger()
        root.addHandler(handler)
        # Pipeline modules log at INFO; when the service runs without
        # setup_logging (tests, embedded use) the root gate would swallow
        # them before the handler sees anything.
        prev_level = root.level
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        try:
            if job.mode == "weekly":
                outcome = run_weekly(self._settings, options, progress=progress)
            else:
                outcome = run_mix_prep(self._settings, options, progress=progress)
            job.report_id = outcome.report_id
            job.recommended_count = outcome.recommended_count
            job.no_candidates = outcome.no_candidates
            job.artifact = outcome.artifact if job.dry_run else None
            job.status = "succeeded"
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            logger.error(f"[jobs] Job {job.id} failed: {job.error}")
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
            job.finished_at = _now_iso()
            self._persist()

    def _jobs_path(self) -> str:
        return os.path.join(self._settings.data_dir, _JOBS_FILE)

    def _trim_locked(self) -> None:
        for jid in self._order[_JOBS_RETAIN:]:
            self._jobs.pop(jid, None)
        self._order = self._order[:_JOBS_RETAIN]

    def _persist(self) -> None:
        with self._lock:
            payload = []
            for jid in self._order:
                job = self._jobs[jid]
                d = job.summary()
                d["stages"] = list(job.stages)
                d["log_tail"] = list(job.log_tail)[-_PERSISTED_LOG_LINES:]
                payload.append(d)
        try:
            atomic_write_json(self._jobs_path(), payload)
        except OSError as exc:
            logger.warning(f"[jobs] Could not persist job history: {exc}")

    def _load_persisted(self) -> None:
        path = self._jobs_path()
        if not os.path.exists(path):
            return
        try:
            import json
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning(f"[jobs] Could not load persisted jobs: {exc}")
            return
        for d in payload:
            job = Job(
                id=d["id"], mode=d["mode"], params=d.get("params", {}),
                dry_run=d.get("dry_run", False), created_at=d.get("created_at", ""),
                status=d.get("status", "failed"), started_at=d.get("started_at"),
                finished_at=d.get("finished_at"), report_id=d.get("report_id"),
                recommended_count=d.get("recommended_count"),
                no_candidates=d.get("no_candidates", False), error=d.get("error"),
                stages=d.get("stages", []),
            )
            job.log_tail.extend(d.get("log_tail", []))
            if job.status in ("queued", "running"):
                job.status = "failed"
                job.error = "interrupted by service restart"
                job.finished_at = job.finished_at or _now_iso()
            self._jobs[job.id] = job
            self._order.append(job.id)
