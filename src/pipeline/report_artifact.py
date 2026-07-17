"""Structured report artifact — the web-native equivalent of the Discord text.

Every run builds a JSON-serialisable artifact describing the full report:
sections in rendered order, per-track reasons, signals, scores, BPM/key,
embed ids, and funnel stats. Live runs persist it to
data/reports/report_{report_id}.json (atomic write); dry runs keep it
in-memory only, mirroring the audition-page policy. Unlike audition pages,
artifacts are never pruned — they are the web app's browsing history.

build_report_artifact is pure (no IO); write/load/list do the file handling.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import Optional

from src.models import ArtistProfile, Candidate
from src.pipeline.dedup import make_dedup_key
from src.pipeline.harmonic import candidate_bpm, candidate_camelot
from src.pipeline.reasons import compose_reason
from src.pipeline.report import _SECTION_ORDER, report_order
from src.pipeline.storage import atomic_write_text

SCHEMA_VERSION = 1

_SECTION_LABELS = {
    "top_picks": "Top Picks",
    "label_watch": "Label Watch",
    "artist_watch": "Artist Watch",
    "wildcards": "Wildcards",
    "deep_cuts": "Deep Cuts",
    "free_downloads": "Free Downloads",
}

_ARTIFACT_RE = re.compile(r"^report_(?P<report_id>.+)\.json$")


def _embed(c: Candidate) -> Optional[dict]:
    """Player embed descriptor — same source preference as the audition page."""
    album_id = c.raw_metadata.get("bandcamp_album_id")
    if album_id and isinstance(album_id, int):
        return {"type": "bandcamp", "album_id": album_id}
    beatport_id = c.raw_metadata.get("beatport_id")
    if beatport_id and isinstance(beatport_id, int):
        return {"type": "beatport", "track_id": beatport_id}
    if c.source == "soundcloud" and c.link:
        # SoundCloud's widget embeds from the bare track permalink — no id needed.
        return {"type": "soundcloud", "url": c.link}
    return None


def _track_payload(
    n: int,
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    label_artists: Optional[dict[str, list[str]]],
    today: Optional[date],
    aliases: Optional[dict[str, str]],
) -> dict:
    key_raw = c.raw_metadata.get("keysign") or c.raw_metadata.get("key")
    return {
        "track_no": n,
        "key": make_dedup_key(c.artist, c.title),
        "artist": c.artist,
        "title": c.title,
        "link": c.link,
        "source": c.source,
        "seen_on_sources": c.raw_metadata.get("seen_on_sources", [c.source]),
        "label": c.label,
        "release_date": c.release_date,
        "release_name": c.release_name,
        "genre_tags": c.genre_tags,
        "score": c.score,
        "familiarity_score": c.familiarity_score,
        "discovery_score": c.discovery_score,
        "signals": [{"code": s.code, "explanation": s.explanation} for s in c.signals],
        "reason": compose_reason(c, profiles_lower, label_artists=label_artists, today=today, aliases=aliases),
        "bpm": candidate_bpm(c),
        "camelot": candidate_camelot(c),
        "key_raw": str(key_raw) if key_raw else None,
        "chart_position": c.raw_metadata.get("chart_position"),
        "embed": _embed(c),
        "pool_added_at": c.pool_added_at,
    }


def build_report_artifact(
    sections: dict[str, list[Candidate]],
    report_id: str,
    kind: str,
    stats: dict,
    profiles: Optional[dict[str, ArtistProfile]] = None,
    label_artists: Optional[dict[str, list[str]]] = None,
    aliases: Optional[dict[str, str]] = None,
    genre: Optional[str] = None,
    filters: Optional[dict] = None,
    dry_run: bool = False,
    today: Optional[date] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """Build the artifact dict. Pure — no IO.

    kind: "weekly" | "mix-prep". today/generated_at must be injected in tests
    for determinism (defaults are UTC now, same convention as the renderers).
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}

    numbers = {id(c): i for i, c in enumerate(report_order(sections), start=1)}
    section_payloads = []
    for section_key in _SECTION_ORDER:
        tracks = sections.get(section_key)
        if not tracks:
            continue
        section_payloads.append({
            "key": section_key,
            "label": _SECTION_LABELS.get(section_key, section_key.replace("_", " ").title()),
            "tracks": [
                _track_payload(numbers[id(c)], c, profiles_lower, label_artists, today, aliases)
                for c in sorted(tracks, key=lambda c: numbers[id(c)])
            ],
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "report_id": report_id,
        "generated_at": generated_at,
        "dry_run": dry_run,
        "genre": genre,
        "filters": filters,
        "stats": stats,
        "label_artists": label_artists or {},
        "sections": section_payloads,
        "track_count": len(numbers),
    }


def report_artifact_path(data_dir: str, report_id: str) -> str:
    return os.path.join(data_dir, "reports", f"report_{report_id}.json")


def write_report_artifact(artifact: dict, data_dir: str) -> str:
    """Persist the artifact atomically. Returns the written path."""
    path = report_artifact_path(data_dir, artifact["report_id"])
    atomic_write_text(path, json.dumps(artifact, indent=2, ensure_ascii=False))
    return path


def load_report_artifact(data_dir: str, report_id: str) -> Optional[dict]:
    path = report_artifact_path(data_dir, report_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_report_artifact_ids(data_dir: str) -> list[str]:
    """Report ids with a persisted artifact, newest file first."""
    reports_dir = os.path.join(data_dir, "reports")
    if not os.path.isdir(reports_dir):
        return []
    entries = []
    for name in os.listdir(reports_dir):
        m = _ARTIFACT_RE.match(name)
        if m:
            entries.append((os.path.getmtime(os.path.join(reports_dir, name)), m.group("report_id")))
    return [report_id for _, report_id in sorted(entries, reverse=True)]
