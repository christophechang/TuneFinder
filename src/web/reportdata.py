"""Assembly of report list/detail payloads from artifacts, histories, feedback.

Reports come in two fidelities:
- artifact-backed (data/reports/report_{id}.json, written since the web
  transformation) — full sections, reasons, signals, players;
- degraded (pre-artifact reports) — reconstructed from history records only:
  artist/title/link/source/signal codes/score, no reasons or players.

Feedback state (latest mark per (history, key)) is joined onto every track in
both fidelities so the SPA renders current outcomes without a second call.
"""
from __future__ import annotations

from typing import Optional

from src.pipeline.dedup import make_dedup_key
from src.pipeline.feedback import FeedbackEntry, latest_marks, load_feedback
from src.pipeline.history import load_history, load_mix_prep_history
from src.pipeline.report_artifact import list_report_artifact_ids, load_report_artifact


def report_kind(report_id: str) -> tuple[str, Optional[str]]:
    """(kind, genre) derived from the report id convention."""
    if "-mix-prep-" in report_id:
        return "mix-prep", report_id.split("-mix-prep-", 1)[1]
    return "weekly", None


def _marks_by_history_key(data_dir: str) -> dict[tuple[str, str], FeedbackEntry]:
    return {(e.history, e.key): e for e in latest_marks(load_feedback(data_dir))}


def _feedback_payload(entry: Optional[FeedbackEntry]) -> Optional[dict]:
    if entry is None:
        return None
    return {"outcome": entry.outcome, "marked_at": entry.marked_at}


def list_reports(settings, kind: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Report summaries from both histories, newest first."""
    data_dir = settings.data_dir
    artifact_ids = set(list_report_artifact_ids(data_dir))
    marks = _marks_by_history_key(data_dir)

    groups: dict[str, dict] = {}
    for history_name, records in (("weekly", load_history(data_dir)),
                                  ("mix-prep", load_mix_prep_history(data_dir))):
        for r in records:
            g = groups.setdefault(r.report_id, {
                "report_id": r.report_id,
                "kind": report_kind(r.report_id)[0],
                "genre": report_kind(r.report_id)[1],
                "generated_at": r.recommended_at,
                "track_count": 0,
                "marked_count": 0,
                "has_artifact": r.report_id in artifact_ids,
            })
            g["track_count"] += 1
            if r.recommended_at > (g["generated_at"] or ""):
                g["generated_at"] = r.recommended_at
            if (history_name, make_dedup_key(r.artist, r.title)) in marks:
                g["marked_count"] += 1

    summaries = sorted(groups.values(), key=lambda g: g["generated_at"] or "", reverse=True)
    if kind:
        summaries = [g for g in summaries if g["kind"] == kind]
    return summaries[:limit]


def get_report_detail(settings, report_id: str) -> Optional[dict]:
    """Full report payload, artifact-backed when available, else degraded.

    Returns None when the report id is unknown to both artifacts and histories.
    """
    data_dir = settings.data_dir
    kind, genre = report_kind(report_id)
    history_name = "weekly" if kind == "weekly" else "mix-prep"
    marks = _marks_by_history_key(data_dir)

    artifact = load_report_artifact(data_dir, report_id)
    if artifact is not None:
        sections = []
        for section in artifact.get("sections", []):
            tracks = []
            for t in section.get("tracks", []):
                t = dict(t)
                t["signal_codes"] = [s["code"] for s in t.get("signals", [])]
                t["feedback"] = _feedback_payload(marks.get((history_name, t["key"])))
                tracks.append(t)
            sections.append({"key": section["key"], "label": section["label"], "tracks": tracks})
        return {
            "report_id": report_id,
            "kind": kind,
            "genre": genre if genre is not None else artifact.get("genre"),
            "generated_at": artifact.get("generated_at"),
            "degraded": False,
            "dry_run": bool(artifact.get("dry_run", False)),
            "filters": artifact.get("filters"),
            "stats": artifact.get("stats"),
            "label_artists": artifact.get("label_artists", {}),
            "sections": sections,
            "track_count": artifact.get("track_count", 0),
        }

    records = load_history(data_dir) if kind == "weekly" else load_mix_prep_history(data_dir)
    batch = [r for r in records if r.report_id == report_id]
    if not batch:
        return None

    batch.sort(key=lambda r: (r.track_no is None, r.track_no or 0))
    tracks = []
    for i, r in enumerate(batch, start=1):
        key = make_dedup_key(r.artist, r.title)
        tracks.append({
            "track_no": r.track_no if r.track_no is not None else i,
            "key": key,
            "artist": r.artist,
            "title": r.title,
            "link": r.link,
            "source": r.source,
            "seen_on_sources": [r.source] if r.source else [],
            "label": r.label,
            "genre_tags": r.genre_tags,
            "score": r.score,
            "signals": [],
            "signal_codes": r.signal_codes,
            "feedback": _feedback_payload(marks.get((history_name, key))),
        })
    generated_at = max((r.recommended_at for r in batch), default=None)
    return {
        "report_id": report_id,
        "kind": kind,
        "genre": genre,
        "generated_at": generated_at,
        "degraded": True,
        "dry_run": False,
        "filters": None,
        "stats": None,
        "label_artists": {},
        "sections": [{"key": "tracks", "label": "Tracks", "tracks": tracks}],
        "track_count": len(tracks),
    }


def resolve_feedback_target(settings, report_id: Optional[str], track_no: Optional[int],
                            selector: Optional[str]):
    """Resolve a feedback request to (record, history_name).

    Precedence: (report_id, track_no) exact match, then selector (same
    semantics as `tunefinder mark`). Raises LookupError with a user-facing
    message when nothing matches or the request is underspecified.
    """
    from src.pipeline.feedback import resolve_selector

    weekly = load_history(settings.data_dir)
    mix_prep = load_mix_prep_history(settings.data_dir)

    if report_id is not None and track_no is not None:
        kind, _ = report_kind(report_id)
        records = weekly if kind == "weekly" else mix_prep
        history_name = "weekly" if kind == "weekly" else "mix-prep"
        for r in records:
            if r.report_id == report_id and r.track_no == track_no:
                return r, history_name
        raise LookupError(f"No track #{track_no} recorded for report {report_id!r}.")

    if selector:
        return resolve_selector(selector, weekly, mix_prep)

    raise LookupError("Provide either report_id + track_no, or a selector.")
