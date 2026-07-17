"""FastAPI application — the TuneFinder web API.

create_app(settings) wires the whole surface; `tunefinder serve` runs it with
uvicorn. All state lives in settings.data_dir next to the pipeline — this
service runs where the data lives (see docs/architecture/tunefinder-web.md).

Auth: Bearer secret on everything under /api except /api/health.
CORS: settings.web_allowed_origins (empty default — static mount and
same-origin deployments need none).
Static SPA: optional mount of a built tunefinder-web bundle
(TUNEFINDER_WEB_STATIC_DIR) with SPA fallback routing.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.logger import get_logger
from src.pipeline.dedup import make_dedup_key
from src.services.runs import MIX_PREP_GENRES
from src.web import schemas
from src.web.auth import check_auth_config, make_auth_dependency
from src.web.jobs import JobConflictError, JobManager, JobValidationError
from src.web.reportdata import get_report_detail, list_reports, resolve_feedback_target

logger = get_logger(__name__)

API_VERSION = "1.0.0"

_SOURCE_NAMES = (
    "beatport", "traxsource", "bandcamp", "boomkat", "bleep",
    "resident_advisor", "mixupload", "volumo",
)


def create_app(settings=None, job_manager: JobManager | None = None) -> FastAPI:
    if settings is None:
        from src.config import load_settings
        settings = load_settings()

    check_auth_config(settings)
    require_auth = make_auth_dependency(settings)
    jobs = job_manager or JobManager(settings)

    app = FastAPI(
        title="TuneFinder API",
        version=API_VERSION,
        description="Web API over the TuneFinder discovery pipeline — reports, feedback, runs, insights.",
    )

    origins = settings.web_allowed_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
            max_age=43200,
        )

    # --- health (no auth — used as the SPA's connection test) ---

    @app.get("/api/health", response_model=schemas.HealthResponse)
    def health():
        from src.pipeline.source_health import load_run_health

        latest_report_id = None
        latest_run_at = None
        source_health = None
        try:
            reports = list_reports(settings, limit=1)
            if reports:
                latest_report_id = reports[0]["report_id"]
                latest_run_at = reports[0]["generated_at"]
            runs = load_run_health(settings.data_dir)
            if runs:
                source_health = runs[-1]
        except Exception as exc:  # health must not 500 on a corrupt store
            logger.warning(f"[web] health probe degraded: {exc}")
        return {
            "status": "ok",
            "version": API_VERSION,
            "auth_required": bool(settings.web_api_secret),
            "latest_report_id": latest_report_id,
            "latest_run_at": latest_run_at,
            "active_job_id": jobs.active_job_id(),
            "source_health": source_health,
        }

    # --- reports ---

    @app.get("/api/reports", response_model=schemas.ReportListResponse,
             dependencies=[Depends(require_auth)])
    def reports_list(kind: str | None = Query(default=None, pattern="^(weekly|mix-prep|free-downloads)$"),
                     limit: int = Query(default=50, ge=1, le=500)):
        return {"reports": list_reports(settings, kind=kind, limit=limit)}

    @app.get("/api/reports/{report_id}", response_model=schemas.ReportDetail,
             dependencies=[Depends(require_auth)])
    def report_detail(report_id: str):
        detail = get_report_detail(settings, report_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Unknown report {report_id!r}")
        return detail

    # --- feedback ---

    @app.post("/api/feedback", response_model=schemas.FeedbackResponse,
              dependencies=[Depends(require_auth)])
    def mark_feedback(body: schemas.FeedbackRequest):
        from src.pipeline.feedback import FeedbackEntry, append_feedback, load_feedback

        try:
            record, history_name = resolve_feedback_target(
                settings, body.report_id, body.track_no, body.selector,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        entry_key = make_dedup_key(record.artist, record.title)
        existing = load_feedback(settings.data_dir)
        prev = [e for e in existing if e.history == history_name and e.key == entry_key]
        previous_outcome = max(prev, key=lambda e: e.marked_at).outcome if prev else None

        entry = FeedbackEntry(
            key=entry_key,
            artist=record.artist,
            title=record.title,
            outcome=body.outcome,
            marked_at=datetime.now(timezone.utc).isoformat(),
            report_id=record.report_id,
            track_no=record.track_no,
            history=history_name,
        )
        append_feedback(entry, settings.data_dir)
        logger.info(f"[web] mark {history_name}/{entry_key} → {body.outcome}")
        return {
            "key": entry.key, "artist": entry.artist, "title": entry.title,
            "outcome": entry.outcome, "marked_at": entry.marked_at,
            "report_id": entry.report_id, "track_no": entry.track_no,
            "history": history_name, "previous_outcome": previous_outcome,
        }

    @app.get("/api/feedback/stats", response_model=schemas.FeedbackStatsResponse,
             dependencies=[Depends(require_auth)])
    def feedback_stats():
        from src.pipeline.feedback import load_feedback, summarise_feedback, tune_data
        from src.pipeline.history import load_history, load_mix_prep_history

        entries = load_feedback(settings.data_dir)
        weekly = load_history(settings.data_dir)
        mix_prep = load_mix_prep_history(settings.data_dir)
        return {
            "stats": summarise_feedback(weekly, mix_prep, entries) if entries else {},
            "tune": tune_data(weekly, mix_prep, entries),
        }

    # --- explain ---

    @app.get("/api/explain", response_model=schemas.ExplainResponse,
             dependencies=[Depends(require_auth)])
    def explain(selector: str = Query(min_length=1)):
        from src.pipeline.explain import explain_track

        return {"selector": selector, "text": explain_track(selector, settings)}

    # --- profile / pool / source health / config ---

    @app.get("/api/profile", response_model=schemas.ProfileResponse,
             dependencies=[Depends(require_auth)])
    def profile(top: int = Query(default=50, ge=1, le=500)):
        from src.pipeline.labels import load_label_affinity
        from src.pipeline.profile import (
            load_artist_profiles, load_genre_affinity, load_known_tracks,
        )

        profiles = load_artist_profiles(settings.data_dir)
        top_artists = sorted(
            profiles.values(),
            key=lambda p: (p.recency_weighted_play_count or p.play_count, p.play_count),
            reverse=True,
        )[:top]
        store = load_label_affinity(settings.data_dir)
        labels = []
        for label_key, entry in store.items():
            artists = entry.get("artists", {})
            labels.append({
                "label": label_key,
                "display_name": entry.get("display_name", label_key),
                "artist_count": len(artists),
                "artists": sorted(a.get("name", k) for k, a in artists.items()),
                "last_seen": entry.get("last_seen"),
            })
        labels.sort(key=lambda l: (l["artist_count"], l["last_seen"] or ""), reverse=True)
        return {
            "artist_count": len(profiles),
            "known_track_count": len(load_known_tracks(settings.data_dir)),
            "top_artists": [
                {
                    "name": p.name, "play_count": p.play_count,
                    "recency_weighted_play_count": p.recency_weighted_play_count,
                    "genres_seen": p.genres_seen,
                }
                for p in top_artists
            ],
            "genre_affinity": load_genre_affinity(settings.data_dir),
            "labels": labels[:top],
        }

    @app.get("/api/pool", response_model=schemas.PoolResponse,
             dependencies=[Depends(require_auth)])
    def pool(limit: int = Query(default=100, ge=1, le=500)):
        from src.pipeline.pool import POOL_CAP, load_pool

        records = sorted(load_pool(settings.data_dir), key=lambda r: r.last_score, reverse=True)
        return {
            "count": len(records),
            "cap": POOL_CAP,
            "tracks": [
                {
                    "key": make_dedup_key(r.artist, r.title),
                    "artist": r.artist, "title": r.title, "link": r.link,
                    "source": r.source, "label": r.label, "release_date": r.release_date,
                    "genre_tags": r.genre_tags, "added_at": r.added_at, "last_score": r.last_score,
                }
                for r in records[:limit]
            ],
        }

    @app.get("/api/sources/health", response_model=schemas.SourceHealthResponse,
             dependencies=[Depends(require_auth)])
    def sources_health():
        from src.pipeline.source_health import load_run_health

        return {"runs": load_run_health(settings.data_dir)}

    @app.get("/api/config", response_model=schemas.ConfigResponse,
             dependencies=[Depends(require_auth)])
    def config_view():
        weights = settings.scoring_weights()
        return {
            "sources": {name: settings.source_enabled(name) for name in _SOURCE_NAMES},
            "pipeline": {
                "release_date_window_days": settings.pipeline_release_date_window_days,
                "section_min_score": settings.pipeline_section_min_score,
                "remix_aware_identity": settings.pipeline_remix_aware_identity,
                "top_picks_count": settings.pipeline_top_picks_count,
                "label_watch_count": settings.pipeline_label_watch_count,
                "artist_watch_count": settings.pipeline_artist_watch_count,
                "wildcard_count": settings.pipeline_wildcard_count,
                "mix_prep_top_picks_count": settings.pipeline_mix_prep_top_picks_count,
                "mix_prep_deep_cuts_count": settings.pipeline_mix_prep_deep_cuts_count,
                "genre_exclusions": settings.pipeline_genre_exclusions,
            },
            "scoring": asdict(weights),
            "genres": list(MIX_PREP_GENRES),
            "data_dir": settings.data_dir,
        }

    # --- runs (jobs) ---

    @app.post("/api/runs", response_model=schemas.RunAccepted, status_code=202,
              dependencies=[Depends(require_auth)])
    def start_run(body: schemas.RunRequest):
        try:
            job = jobs.submit(body.model_dump())
        except JobValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"job_id": job.id}

    @app.get("/api/runs", response_model=schemas.JobListResponse,
             dependencies=[Depends(require_auth)])
    def list_runs(limit: int = Query(default=20, ge=1, le=50)):
        return {"jobs": [job.summary() for job in jobs.list(limit=limit)]}

    @app.get("/api/runs/{job_id}", response_model=schemas.JobDetail,
             dependencies=[Depends(require_auth)])
    def run_detail(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job {job_id!r}")
        return job.detail()

    # --- optional static SPA (zero-CORS LAN mode) ---

    static_dir = settings.web_static_dir
    if static_dir and os.path.isdir(static_dir):
        index_path = os.path.join(static_dir, "index.html")

        root = os.path.abspath(static_dir)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = os.path.abspath(os.path.normpath(os.path.join(root, full_path)))
            if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_path)  # SPA fallback for client-side routes

        logger.info(f"[web] Serving SPA from {static_dir}")

    return app
