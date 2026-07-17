"""Pydantic response/request models for the web API.

These define the OpenAPI contract that tunefinder-web generates its TypeScript
types from — change deliberately, the SPA regenerates against this schema.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Outcome = Literal["bought", "liked", "skip", "own", "heard"]


class ApiModel(BaseModel):
    """Response base: defaulted fields are still marked required in the
    serialization schema, so the generated TypeScript types are non-optional
    for fields the API always sends."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Signal(ApiModel):
    code: str
    explanation: str


class Embed(ApiModel):
    type: Literal["bandcamp", "beatport", "soundcloud"]
    album_id: Optional[int] = None
    track_id: Optional[int] = None
    url: Optional[str] = None  # soundcloud: track permalink for the widget player


class TrackFeedback(ApiModel):
    outcome: Outcome
    marked_at: str


class ReportTrack(ApiModel):
    track_no: int
    key: str
    artist: str
    title: str
    link: str = ""
    source: str = ""
    seen_on_sources: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    release_date: Optional[str] = None
    release_name: Optional[str] = None
    genre_tags: list[str] = Field(default_factory=list)
    score: Optional[float] = None
    familiarity_score: Optional[float] = None
    discovery_score: Optional[float] = None
    signals: list[Signal] = Field(default_factory=list)
    signal_codes: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    bpm: Optional[float] = None
    camelot: Optional[str] = None
    key_raw: Optional[str] = None
    chart_position: Optional[int] = None
    embed: Optional[Embed] = None
    pool_added_at: Optional[str] = None
    feedback: Optional[TrackFeedback] = None


class ReportSection(ApiModel):
    key: str
    label: str
    tracks: list[ReportTrack]


class ReportSummary(ApiModel):
    report_id: str
    kind: Literal["weekly", "mix-prep"]
    genre: Optional[str] = None
    generated_at: Optional[str] = None
    track_count: int
    marked_count: int = 0
    has_artifact: bool = False


class ReportListResponse(ApiModel):
    reports: list[ReportSummary]


class ReportDetail(ApiModel):
    report_id: str
    kind: Literal["weekly", "mix-prep"]
    genre: Optional[str] = None
    generated_at: Optional[str] = None
    degraded: bool = False
    dry_run: bool = False
    filters: Optional[dict] = None
    stats: Optional[dict] = None
    label_artists: dict[str, list[str]] = Field(default_factory=dict)
    sections: list[ReportSection]
    track_count: int


class FeedbackRequest(BaseModel):
    outcome: Outcome
    report_id: Optional[str] = None
    track_no: Optional[int] = None
    selector: Optional[str] = None


class FeedbackResponse(ApiModel):
    key: str
    artist: str
    title: str
    outcome: Outcome
    marked_at: str
    report_id: str
    track_no: Optional[int] = None
    history: Literal["weekly", "mix-prep"]
    previous_outcome: Optional[str] = None


class FeedbackStatsResponse(ApiModel):
    stats: dict
    tune: dict


class ExplainResponse(ApiModel):
    selector: str
    text: str


class ArtistSummary(ApiModel):
    name: str
    play_count: int
    recency_weighted_play_count: float = 0.0
    genres_seen: list[str] = Field(default_factory=list)


class LabelAffinitySummary(ApiModel):
    label: str
    display_name: str
    artist_count: int
    artists: list[str] = Field(default_factory=list)
    last_seen: Optional[str] = None


class ProfileResponse(ApiModel):
    artist_count: int
    known_track_count: int
    top_artists: list[ArtistSummary]
    genre_affinity: dict[str, float] = Field(default_factory=dict)
    labels: list[LabelAffinitySummary] = Field(default_factory=list)


class PoolTrack(ApiModel):
    key: str
    artist: str
    title: str
    link: str = ""
    source: str = ""
    label: Optional[str] = None
    release_date: Optional[str] = None
    genre_tags: list[str] = Field(default_factory=list)
    added_at: str
    last_score: float = 0.0


class PoolResponse(ApiModel):
    count: int
    cap: int
    tracks: list[PoolTrack]


class SourceHealthResponse(ApiModel):
    runs: list[dict]


class ConfigResponse(ApiModel):
    sources: dict[str, bool]
    pipeline: dict
    scoring: dict
    genres: list[str]
    data_dir: str


class HealthResponse(ApiModel):
    status: str
    version: str
    auth_required: bool
    latest_report_id: Optional[str] = None
    latest_run_at: Optional[str] = None
    active_job_id: Optional[str] = None
    source_health: Optional[dict] = None


# --- Jobs (on-demand runs) ---

class RunRequest(BaseModel):
    mode: Literal["weekly", "mix-prep"]
    genre: Optional[str] = None
    bpm_min: Optional[float] = None
    bpm_max: Optional[float] = None
    key: Optional[str] = None
    bpm_flex: bool = True
    dry_run: bool = False


class JobStage(ApiModel):
    stage: str
    detail: str
    at: str


class JobSummary(ApiModel):
    id: str
    mode: Literal["weekly", "mix-prep"]
    status: Literal["queued", "running", "succeeded", "failed"]
    dry_run: bool = False
    params: dict = Field(default_factory=dict)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    report_id: Optional[str] = None
    recommended_count: Optional[int] = None
    no_candidates: bool = False
    error: Optional[str] = None


class JobDetail(JobSummary):
    stages: list[JobStage] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)
    artifact: Optional[dict] = None


class JobListResponse(ApiModel):
    jobs: list[JobSummary]


class RunAccepted(ApiModel):
    job_id: str
