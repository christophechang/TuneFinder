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
from src.pipeline.dedup import make_dedup_key, normalise_artist
from src.pipeline.profile import _split_artists
from src.pipeline.storage import atomic_write_json

OUTCOMES = ("bought", "liked", "skip", "own", "heard")

# Outcomes that carry no taste signal and are excluded from every positive-rate
# denominator. `own` is an identity-gap mark (already in the library); `heard`
# is "listened, no verdict" — deliberately inert so a lukewarm play never dents
# the artist's or genre's stats. Neither feeds the skip-derived artist penalty.
NEUTRAL_OUTCOMES = ("own", "heard")

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
    path = os.path.join(data_dir, _FEEDBACK_FILE)
    atomic_write_json(path, [_entry_to_dict(e) for e in existing])


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
    # Remix-aware identity (issue #9) is deliberately NOT threaded here: selector
    # resolution matches a typed "Artist - Title" against stored records using
    # make_dedup_key on BOTH sides symmetrically, so the flag-off legacy key is
    # self-consistent regardless of the global setting. Marks reference the
    # history record captured at recommend-time; matching against the legacy key
    # keeps `mark` working for records written under either regime.
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

def latest_marks(entries: list[FeedbackEntry]) -> list[FeedbackEntry]:
    """Return one FeedbackEntry per (history, key) — the latest by marked_at.

    Marks are append-only (see append_feedback / cmd_mark), so a re-mark adds a
    new entry rather than overwriting. Every consumer that wants "the current
    outcome" collapses to the newest per (history, key); this is the single
    shared implementation of that rule (used by summarise_feedback and
    tune_report).
    """
    latest: dict[tuple[str, str], FeedbackEntry] = {}
    for e in entries:
        k = (e.history, e.key)
        if k not in latest or e.marked_at > latest[k].marked_at:
            latest[k] = e
    return list(latest.values())


# ---------------------------------------------------------------------------
# Skip-derived negative signal (issue #11)
# ---------------------------------------------------------------------------

_POSITIVE_MARK_OUTCOMES = ("bought", "liked")


def skipped_artists(entries: list[FeedbackEntry], min_skips: int) -> set[str]:
    """Return normalised artist names (dedup.normalise_artist) that qualify for
    the skip-derived negative signal (issue #11 — ranker `skipped_artist`).

    Reuses `latest_marks`' latest-mark-per-(history, key) semantics: a stale
    mark superseded by a later re-mark on the same track never counts. Each
    surviving entry's artist string is split via `profile._split_artists`
    (collaborators get individual credit, same rule as profile building) and
    each part normalised via `dedup.normalise_artist` so "Sully" and "sully "
    collapse to one key. Combining across BOTH histories ('weekly' and
    'mix-prep' — a skip is a skip regardless of which report it came from), an
    artist qualifies when they have >= min_skips latest-mark 'skip' outcomes
    AND zero latest-mark positive outcomes ('bought' or 'liked'). A single
    positive mark disqualifies the artist entirely, even if the skip count
    would otherwise clear the threshold — one 'liked' means taste changed, not
    aversion, and the penalty should not keep firing against current evidence.
    'own' is neutral (an identity-gap mark, not a taste signal) — it counts
    toward neither skips nor positives.
    """
    skip_counts: dict[str, int] = {}
    has_positive: set[str] = set()

    for entry in latest_marks(entries):
        for part in _split_artists(entry.artist):
            name = normalise_artist(part)
            if not name:
                continue
            if entry.outcome == "skip":
                skip_counts[name] = skip_counts.get(name, 0) + 1
            elif entry.outcome in _POSITIVE_MARK_OUTCOMES:
                has_positive.add(name)
            # "own" — neutral, no-op.

    return {
        name for name, count in skip_counts.items()
        if count >= min_skips and name not in has_positive
    }


def summarise_feedback(
    weekly: list[RecommendationRecord],
    mix_prep: list[RecommendationRecord],
    entries: list[FeedbackEntry],
) -> dict:
    """Pure aggregation — no printing. Returns nested dict of stats."""
    if not entries:
        return {}

    effective = latest_marks(entries)

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
        non_own = [e for e in hist_entries if e.outcome not in NEUTRAL_OUTCOMES]
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


# ---------------------------------------------------------------------------
# Feedback-driven tuning report (issue #7)
# ---------------------------------------------------------------------------

# Below this many marks (excluding `own`) the per-dimension rates are too noisy
# to draw conclusions from — the report still prints the table, but with a
# prominent "anecdote, not evidence" caveat. Not a scoring weight (it never
# feeds ranking), so it lives here as a module constant rather than in
# ScoringWeights / settings.yaml.
_MIN_MARKS_FOR_CONCLUSIONS = 20

_POSITIVE_OUTCOMES = ("bought", "liked")


def _fmt_rate(positive: int, non_own: int) -> str:
    """Positive rate as a percentage string, or '—' when there's no non-own denominator."""
    if non_own <= 0:
        return "—"
    return f"{round(positive / non_own * 100, 1)}%"


def _fmt_lift(positive: int, non_own: int, baseline: float, marked: int) -> str:
    """Lift = this dimension's positive rate / overall baseline positive rate.

    '—' when there's nothing to compare (no marks, no non-own denominator, or a
    zero baseline) rather than inventing a ratio.
    """
    if marked == 0 or non_own <= 0 or baseline <= 0:
        return "—"
    rate = positive / non_own
    return f"{round(rate / baseline, 2):.2f}"


def _tune_signal_values(rec: RecommendationRecord) -> list[str]:
    return list(rec.signal_codes) if rec.signal_codes else ["(pre-v0.8.0)"]


def _tune_genre_values(rec: RecommendationRecord) -> list[str]:
    return list(rec.genre_tags) if rec.genre_tags else ["(pre-v0.8.0)"]


def _tune_source_values(rec: RecommendationRecord) -> list[str]:
    return [rec.source or "(unknown)"]


# (machine key, display title, extractor) — order is the render order.
_TUNE_DIMS = (
    ("signal", "By signal", _tune_signal_values),
    ("source", "By source", _tune_source_values),
    ("genre", "By genre", _tune_genre_values),
)


def tune_data(
    weekly: list[RecommendationRecord],
    mix_prep: list[RecommendationRecord],
    entries: list[FeedbackEntry],
) -> dict:
    """Structured feedback-vs-history aggregation behind tune_report — pure.

    Returns raw counters per dimension value plus overall totals; consumers
    (the text report below, the web API) derive rate/lift from the counters.
    `own` is excluded from every positive-rate denominator (an identity-gap
    miss, not a taste signal — same convention as `stats`).
    """
    # Newest recommendation record per (history, dedup-key). A track can be
    # recommended in both weekly and mix-prep — those are distinct records and
    # each is joined to feedback marked against that history.
    records_by_hk: dict[tuple[str, str], RecommendationRecord] = {}
    for history_name, records in (("weekly", weekly), ("mix-prep", mix_prep)):
        for r in records:
            hk = (history_name, make_dedup_key(r.artist, r.title))
            if hk not in records_by_hk or r.recommended_at > records_by_hk[hk].recommended_at:
                records_by_hk[hk] = r

    marks_by_hk = {(e.history, e.key): e for e in latest_marks(entries)}

    # dim key -> value -> counters
    agg: dict[str, dict[str, dict[str, int]]] = {key: {} for key, _, _ in _TUNE_DIMS}

    def _slot(dim: str, value: str) -> dict[str, int]:
        return agg[dim].setdefault(
            value, {"recommended": 0, "marked": 0, "positive": 0, "non_own": 0}
        )

    # Pass A — recommended counts over the full recommendation corpus.
    for rec in records_by_hk.values():
        for dim, _, extractor in _TUNE_DIMS:
            for value in extractor(rec):
                _slot(dim, value)["recommended"] += 1

    # Pass B — marked / positive over joined marks; also the overall baseline.
    overall_marked = 0
    overall_non_own = 0
    overall_positive = 0
    for hk, entry in marks_by_hk.items():
        rec = records_by_hk.get(hk)
        if rec is None:
            continue
        is_neutral = entry.outcome in NEUTRAL_OUTCOMES
        is_positive = entry.outcome in _POSITIVE_OUTCOMES
        overall_marked += 1
        if not is_neutral:
            overall_non_own += 1
        if is_positive:
            overall_positive += 1
        for dim, _, extractor in _TUNE_DIMS:
            for value in extractor(rec):
                slot = _slot(dim, value)
                slot["marked"] += 1
                if not is_neutral:
                    slot["non_own"] += 1
                if is_positive:
                    slot["positive"] += 1

    recommended_total = len(records_by_hk)
    coverage = round(overall_marked / recommended_total * 100, 1) if recommended_total else 0.0
    baseline = overall_positive / overall_non_own if overall_non_own else 0.0

    return {
        "recommended_total": recommended_total,
        "marked": overall_marked,
        "non_own": overall_non_own,
        "positive": overall_positive,
        "coverage_pct": coverage,
        "baseline": baseline,
        "thin_data": overall_non_own < _MIN_MARKS_FOR_CONCLUSIONS,
        "min_marks_threshold": _MIN_MARKS_FOR_CONCLUSIONS,
        "dimensions": agg,
    }


def tune_report(
    weekly: list[RecommendationRecord],
    mix_prep: list[RecommendationRecord],
    entries: list[FeedbackEntry],
) -> str:
    """Join feedback marks against recommendation history and report, per signal
    code / source / genre: recommended count, marked, positive, positive rate,
    and lift vs. the overall baseline positive rate. Pure — returns plain text,
    no printing, no IO. `own` is excluded from every positive-rate denominator
    (an identity-gap miss, not a taste signal — same convention as `stats`).
    """
    data = tune_data(weekly, mix_prep, entries)
    baseline = data["baseline"]

    lines = ["=== Feedback-Driven Tuning Report ==="]
    lines.append(
        f"Recommended: {data['recommended_total']}  |  Marked: {data['marked']}  |  "
        f"Coverage: {data['coverage_pct']}%  |  Baseline positive rate: {_fmt_rate(data['positive'], data['non_own'])}"
    )
    if data["thin_data"]:
        lines.append("")
        lines.append(
            f"⚠️  Only {data['non_own']} marks — treat everything below as anecdote, not evidence."
        )

    for dim, title, _ in _TUNE_DIMS:
        rows = data["dimensions"][dim]
        if not rows:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for value, c in sorted(rows.items(), key=lambda kv: (-kv[1]["marked"], kv[0])):
            rate = _fmt_rate(c["positive"], c["non_own"])
            lift = _fmt_lift(c["positive"], c["non_own"], baseline, c["marked"])
            lines.append(
                f"  {value}: recommended={c['recommended']} marked={c['marked']} "
                f"positive={c['positive']} rate={rate} lift={lift}"
            )

    return "\n".join(lines)
