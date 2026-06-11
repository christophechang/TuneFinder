"""
Feedback capture — mark outcomes against recommended tracks and aggregate stats.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.models import RecommendationRecord
from src.pipeline.dedup import make_dedup_key

OUTCOMES = ("bought", "liked", "skip", "own")

_FEEDBACK_FILE = "feedback.json"


@dataclass
class FeedbackEntry:
    key: str              # make_dedup_key(artist, title) of the resolved record
    artist: str
    title: str
    outcome: str          # one of OUTCOMES
    marked_at: str        # ISO datetime
    report_id: str
    track_no: Optional[int]
    history: str          # "weekly" | "mix-prep"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _entry_to_dict(e: FeedbackEntry) -> dict:
    return {
        "key": e.key,
        "artist": e.artist,
        "title": e.title,
        "outcome": e.outcome,
        "marked_at": e.marked_at,
        "report_id": e.report_id,
        "track_no": e.track_no,
        "history": e.history,
    }


def _dict_to_entry(d: dict) -> FeedbackEntry:
    return FeedbackEntry(
        key=d["key"],
        artist=d["artist"],
        title=d["title"],
        outcome=d["outcome"],
        marked_at=d["marked_at"],
        report_id=d["report_id"],
        track_no=d.get("track_no"),
        history=d["history"],
    )


def load_feedback(data_dir: str) -> list[FeedbackEntry]:
    path = os.path.join(data_dir, _FEEDBACK_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [_dict_to_entry(d) for d in data]


def append_feedback(entry: FeedbackEntry, data_dir: str) -> None:
    existing = load_feedback(data_dir)
    existing.append(entry)
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _FEEDBACK_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_entry_to_dict(e) for e in existing], f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------

def _latest_report_id(records: list[RecommendationRecord]) -> Optional[str]:
    """Return the report_id of the record with the latest recommended_at."""
    best: Optional[RecommendationRecord] = None
    for r in records:
        if not r.recommended_at:
            continue
        if best is None or r.recommended_at > best.recommended_at:
            best = r
    return best.report_id if best else None


def resolve_selector(
    selector: str,
    weekly_records: list[RecommendationRecord],
    mix_prep_records: list[RecommendationRecord],
) -> tuple[RecommendationRecord, str]:
    """Resolve a selector to (record, history-name).

    Raises LookupError with an explanatory message on failure.
    """
    if selector.isdigit():
        return _resolve_by_number(int(selector), weekly_records)
    return _resolve_by_string(selector, weekly_records, mix_prep_records)


def _resolve_by_number(
    track_no: int,
    weekly_records: list[RecommendationRecord],
) -> tuple[RecommendationRecord, str]:
    latest_id = _latest_report_id(weekly_records)
    if latest_id is None:
        raise LookupError("No weekly history found. Use the \"Artist - Title\" form.")

    latest_batch = [r for r in weekly_records if r.report_id == latest_id]

    # Check whether any record in the latest report has track_no values
    if not any(r.track_no is not None for r in latest_batch):
        raise LookupError(
            "Track numbers exist only for reports generated after v0.8.0. "
            "Use the \"Artist - Title\" form instead."
        )

    matches = [r for r in latest_batch if r.track_no == track_no]
    if not matches:
        raise LookupError(
            f"No track #{track_no} in the latest report ({latest_id}). "
            "Check the report and try again."
        )

    # Tie-break: latest recommended_at (same-week re-run)
    matches.sort(key=lambda r: r.recommended_at, reverse=True)
    return matches[0], "weekly"


def _resolve_by_string(
    selector: str,
    weekly_records: list[RecommendationRecord],
    mix_prep_records: list[RecommendationRecord],
) -> tuple[RecommendationRecord, str]:
    if " - " not in selector:
        raise LookupError(
            f"Could not parse selector {selector!r}. "
            "Use \"Artist - Title\" or a track number."
        )
    artist_part, title_part = selector.split(" - ", 1)
    target_key = make_dedup_key(artist_part, title_part)

    # Search weekly newest-first, then mix-prep newest-first
    for history_name, records in (
        ("weekly", sorted(weekly_records, key=lambda r: r.recommended_at, reverse=True)),
        ("mix-prep", sorted(mix_prep_records, key=lambda r: r.recommended_at, reverse=True)),
    ):
        for r in records:
            if make_dedup_key(r.artist, r.title) == target_key:
                return r, history_name

    raise LookupError(
        f"No recommended track matches {selector!r}. "
        "Check artist and title spelling, or list recent reports."
    )


# ---------------------------------------------------------------------------
# Stats aggregation (Commit 4)
# ---------------------------------------------------------------------------

def summarise_feedback(
    weekly: list[RecommendationRecord],
    mix_prep: list[RecommendationRecord],
    entries: list[FeedbackEntry],
) -> dict:
    """Pure aggregation — no printing. Returns nested dict of stats."""
    if not entries:
        return {}

    # Latest entry per (history, key)
    latest: dict[tuple[str, str], FeedbackEntry] = {}
    for e in entries:
        k = (e.history, e.key)
        if k not in latest or e.marked_at > latest[k].marked_at:
            latest[k] = e

    effective = list(latest.values())

    def _records_by_key(records: list[RecommendationRecord]) -> dict[str, RecommendationRecord]:
        # Newest per key
        out: dict[str, RecommendationRecord] = {}
        for r in records:
            rk = make_dedup_key(r.artist, r.title)
            if rk not in out or r.recommended_at > out[rk].recommended_at:
                out[rk] = r
        return out

    weekly_by_key = _records_by_key(weekly)
    mix_prep_by_key = _records_by_key(mix_prep)

    def _bucket(history_name: str, records_by_key: dict[str, RecommendationRecord]) -> dict:
        hist_entries = [e for e in effective if e.history == history_name]
        recommended = len(records_by_key)
        marked = len(hist_entries)
        non_own = [e for e in hist_entries if e.outcome != "own"]
        own_count = sum(1 for e in hist_entries if e.outcome == "own")
        positive = sum(1 for e in non_own if e.outcome in ("bought", "liked"))
        coverage_pct = round(marked / recommended * 100, 1) if recommended else 0.0
        positive_rate = round(positive / len(non_own) * 100, 1) if non_own else 0.0

        # By signal_code
        by_signal: dict[str, dict] = {}
        for e in hist_entries:
            rec = records_by_key.get(e.key)
            codes = rec.signal_codes if (rec and rec.signal_codes) else ["(pre-v0.8.0)"]
            for code in codes:
                if code not in by_signal:
                    by_signal[code] = {"marked": 0, "positive": 0}
                by_signal[code]["marked"] += 1
                if e.outcome in ("bought", "liked"):
                    by_signal[code]["positive"] += 1

        # By source
        by_source: dict[str, dict] = {}
        for e in hist_entries:
            rec = records_by_key.get(e.key)
            src = rec.source if rec else "(unknown)"
            if src not in by_source:
                by_source[src] = {"marked": 0, "positive": 0}
            by_source[src]["marked"] += 1
            if e.outcome in ("bought", "liked"):
                by_source[src]["positive"] += 1

        # By genre_tag
        by_genre: dict[str, dict] = {}
        for e in hist_entries:
            rec = records_by_key.get(e.key)
            tags = rec.genre_tags if (rec and rec.genre_tags) else ["(pre-v0.8.0)"]
            for tag in tags:
                if tag not in by_genre:
                    by_genre[tag] = {"marked": 0, "positive": 0}
                by_genre[tag]["marked"] += 1
                if e.outcome in ("bought", "liked"):
                    by_genre[tag]["positive"] += 1

        # By report_id (chronological)
        by_report: dict[str, dict] = {}
        for e in hist_entries:
            if e.report_id not in by_report:
                by_report[e.report_id] = {"marked": 0, "positive": 0}
            by_report[e.report_id]["marked"] += 1
            if e.outcome in ("bought", "liked"):
                by_report[e.report_id]["positive"] += 1

        return {
            "recommended": recommended,
            "marked": marked,
            "coverage_pct": coverage_pct,
            "positive_rate": positive_rate,
            "own_count": own_count,
            "by_signal": by_signal,
            "by_source": by_source,
            "by_genre": by_genre,
            "by_report": dict(sorted(by_report.items())),
        }

    return {
        "weekly": _bucket("weekly", weekly_by_key),
        "mix_prep": _bucket("mix-prep", mix_prep_by_key),
    }
