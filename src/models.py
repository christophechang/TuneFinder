from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackRef:
    """A track as it appears within a single mix tracklist — no aggregate data."""
    artist: str
    title: str


@dataclass
class Mix:
    id: str
    title: str
    genre: str
    url: str = ""
    description: str = ""
    energy: str = ""          # "peak" | "journey"
    bpm_min: int = 0
    bpm_max: int = 0
    moods: list[str] = field(default_factory=list)
    published_at: str = ""    # ISO date string
    tracklist: list[TrackRef] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        return len(self.tracklist)


@dataclass
class Track:
    """Aggregate representation of a unique track across all mixes."""
    artist: str
    title: str
    genres_seen: list[str] = field(default_factory=list)
    recurrence_count: int = 1
    source_mix_ids: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Normalised dedup key for known-track exclusion."""
        return f"{self.artist.lower().strip()}||{self.title.lower().strip()}"


@dataclass
class ArtistProfile:
    name: str
    play_count: int = 0
    genres_seen: list[str] = field(default_factory=list)
    associated_labels: list[str] = field(default_factory=list)
    track_titles: list[str] = field(default_factory=list)


@dataclass
class LabelRelevance:
    name: str
    score: float = 0.0
    associated_artists: list[str] = field(default_factory=list)
    recent_release_count: int = 0
    source_evidence: list[str] = field(default_factory=list)


@dataclass
class RecommendationSignal:
    code: str         # e.g. "known_artist", "label_match", "adjacent_scene"
    explanation: str  # human-readable, used verbatim in the report


@dataclass
class SourceItem:
    """Raw item as ingested from any external source, before normalisation."""
    source: str        # e.g. "beatport", "juno", "bandcamp"
    artist: str
    title: str
    link: str
    label: Optional[str] = None
    release_date: Optional[str] = None
    release_name: Optional[str] = None
    genre_tags: list[str] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.artist.lower().strip()}||{self.title.lower().strip()}"


@dataclass
class Candidate:
    """Normalised recommendation candidate, ready for ranking and report generation."""
    artist: str
    title: str
    link: str
    source: str
    label: Optional[str] = None
    release_date: Optional[str] = None
    release_name: Optional[str] = None
    signals: list[RecommendationSignal] = field(default_factory=list)
    score: float = 0.0
    is_known: bool = False
    is_previously_recommended: bool = False
    genre_tags: list[str] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.artist.lower().strip()}||{self.title.lower().strip()}"

    @property
    def primary_reason(self) -> str:
        return self.signals[0].explanation if self.signals else ""


@dataclass
class RecommendationRecord:
    """Persisted record of a previously recommended track, used to prevent repeats."""
    artist: str
    title: str
    link: str
    source: str
    recommended_at: str  # ISO date string
    report_id: str       # e.g. "2026-W10"

    @property
    def key(self) -> str:
        return f"{self.artist.lower().strip()}||{self.title.lower().strip()}"


@dataclass
class PoolRecord:
    """A candidate that was scored but not recommended — held for future runs."""
    artist: str
    title: str
    link: str
    source: str
    added_at: str            # ISO date string when first added to pool
    last_score: float = 0.0  # most recent computed score — used for cap-trimming
    label: Optional[str] = None
    release_date: Optional[str] = None
    release_name: Optional[str] = None
    genre_tags: list[str] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.artist.lower().strip()}||{self.title.lower().strip()}"
