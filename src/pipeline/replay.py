"""
Replay harness (issue #7) — reconstruct a past weekly report offline from an
archived source-item snapshot, optionally under overridden scoring/pipeline
config, and diff it against what was actually recommended that week.

Offline and side-effect free: no Discord, no settings.validate(), no writes to
live state. Mirrors cmd_run's weekly pipeline order (see tunefinder/__main__.py)
reusing the same pipeline utilities as src/pipeline/explain.py.

State honesty — what is as-of-WEEK vs as-of-NOW:
- as-of-week: the fetched corpus (the archived source_items snapshot) and the
  release-date window reference date (derived from the archive's ISO week, so a
  week replayed months later still evaluates its window against that week).
- as-of-now: known-tracks, recommendation history, artist profiles, genre
  affinity, aliases and label-affinity memory are all read from the CURRENT
  data/ state. `fresh_release` / recency also use the current clock (documented
  approximation). The candidate POOL is NOT injected at all — pool state is
  today's, not that week's, so replay deliberately covers the fresh corpus only.
"""
import copy
import os
import re
from datetime import date

import yaml

from src.config import Settings
from src.fetchers import list_archive_files, load_archived_source_items
from src.pipeline.dedup import (
    deduplicate_source_items,
    filter_history,
    filter_known,
    filter_release_date,
    items_to_candidates,
    make_dedup_key,
)
from src.pipeline.history import build_history_keys, load_history
from src.pipeline.labels import fresh_label_artist_data, load_label_affinity
from src.pipeline.profile import (
    load_artist_profiles,
    load_genre_affinity,
    load_known_tracks,
)
from src.pipeline.ranker import rank_candidates
from src.pipeline.report import generate_report, report_order

_BANNER = (
    "REPLAY — offline reconstruction; known-track/history/profile state is "
    "as-of-now; pool not injected"
)

_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")


def _reference_date(report_id: str) -> date:
    """Derive the run's reference date from an ISO-week report_id (e.g.
    '2026-W23'). The weekly run fires Sunday 09:00, so the ISO week's Sunday
    (isoweekday 7) is the reference date threaded into the release-date window
    and the report's `today=` for deterministic reasons/date rendering.
    """
    m = _WEEK_RE.fullmatch(report_id)
    if not m:
        raise ValueError(
            f"invalid week {report_id!r} — expected ISO week form YYYY-Www, e.g. 2026-W23"
        )
    year, week = int(m.group(1)), int(m.group(2))
    return date.fromisocalendar(year, week, 7)


def _available_weeks(data_dir: str) -> list[str]:
    weeks = []
    for path in list_archive_files(data_dir):
        m = re.fullmatch(r"source_items_(.+)\.json\.gz", os.path.basename(path))
        if m:
            weeks.append(m.group(1))
    return sorted(weeks)


def _apply_override(data: dict, dotted_path: str, raw_value: str) -> None:
    """Assign a YAML-scalar value into `data` at a dotted path, creating nested
    dicts as needed. Values are parsed via yaml.safe_load so `2.0` becomes a
    float, `true` a bool, `dnb` a string — matching how settings.yaml is typed.
    """
    keys = dotted_path.split(".")
    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = yaml.safe_load(raw_value)


def build_overridden_settings(settings: Settings, overrides: list[str]) -> Settings:
    """Return a fresh Settings built from a DEEP COPY of `settings`' raw data
    with each `path=value` override applied. Never mutates the passed settings
    or writes settings.yaml.
    """
    data = copy.deepcopy(settings._data)
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"invalid --set {raw!r} — expected dotted.path=value, e.g. scoring.w_known_artist=2.0"
            )
        dotted_path, raw_value = raw.split("=", 1)
        _apply_override(data, dotted_path.strip(), raw_value.strip())
    return Settings(data)


def _diff_vs_history(
    recommended: list,
    history: list,
    report_id: str,
    remix_aware: bool = False,
) -> list[str]:
    """Diff the replayed recommendation set against what recommendation_history
    actually recorded for `report_id`, keyed by normalised dedup key."""
    hist_records = [r for r in history if r.report_id == report_id]
    if not hist_records:
        return [f"No recommendation_history records for {report_id} — nothing to diff against."]

    hist_by_key = {make_dedup_key(r.artist, r.title, remix_aware): r for r in hist_records}
    replay_by_key = {make_dedup_key(c.artist, c.title, remix_aware): c for c in recommended}

    still = sorted(set(replay_by_key) & set(hist_by_key))
    newly = sorted(set(replay_by_key) - set(hist_by_key))
    gone = sorted(set(hist_by_key) - set(replay_by_key))

    lines = [f"Compared against {len(hist_records)} recorded recommendation(s) for {report_id}:"]

    lines.append(f"= would still recommend ({len(still)}):")
    for key in still:
        c = replay_by_key[key]
        lines.append(f"    = {c.artist} — {c.title}")

    lines.append(f"+ newly surfaced ({len(newly)}):")
    for key in newly:
        c = replay_by_key[key]
        lines.append(f"    + {c.artist} — {c.title}")

    lines.append(f"- no longer surfaced ({len(gone)}):")
    for key in gone:
        r = hist_by_key[key]
        lines.append(f"    - {r.artist} — {r.title}")

    return lines


def replay_week(week: str, overrides: list[str], settings: Settings) -> str:
    """Replay one archived week's fetch offline and return the rendered report
    plus a diff-vs-history section, all under `_BANNER`. Returns a clean error
    message (not a raised exception) when the archive for `week` is absent.
    """
    data_dir = settings.data_dir
    archive_path = os.path.join(data_dir, "archive", f"source_items_{week}.json.gz")
    if not os.path.exists(archive_path):
        available = _available_weeks(data_dir)
        if available:
            avail_str = ", ".join(available)
            return f"No archive found for week {week!r}.\nAvailable weeks: {avail_str}"
        return (
            f"No archive found for week {week!r}.\n"
            f"No archived weeks found under {os.path.join(data_dir, 'archive')}/."
        )

    ref_date = _reference_date(week)
    rep_settings = build_overridden_settings(settings, overrides)
    # Remix-aware track identity (issue #9) — honour an override too, default off.
    remix_aware = rep_settings.pipeline_remix_aware_identity

    # Corpus is as-of-week (the archived snapshot); everything else as-of-now.
    items = load_archived_source_items(archive_path)
    known_keys = load_known_tracks(data_dir)
    profiles = load_artist_profiles(data_dir)
    genre_affinity = load_genre_affinity(data_dir)
    history = load_history(data_dir)
    # The replayed week's own records are excluded from the history filter: at
    # run time that week hadn't been recorded yet, so blocking on it would drop
    # every track it recommended and make "would still recommend" unreachable in
    # the diff below. Every OTHER week's history still filters as-of-now.
    history_keys = build_history_keys([r for r in history if r.report_id != week], remix_aware)
    aliases = rep_settings.artist_aliases()
    label_store = load_label_affinity(data_dir)

    # Weekly pipeline order (mirrors cmd_run) — NO pool injection in replay.
    deduped = deduplicate_source_items(items, remix_aware)
    candidates = items_to_candidates(deduped)
    label_seed = list(candidates)  # pre-filter, so known artists still inform label relevance
    candidates = filter_known(candidates, known_keys, remix_aware)
    after_known = len(candidates)
    candidates = filter_history(candidates, history_keys, remix_aware)
    after_history = len(candidates)
    window_days = rep_settings.pipeline_release_date_window_days
    if window_days:
        candidates = filter_release_date(candidates, window_days, today=ref_date)
    after_release_date = len(candidates)

    stats = {
        "sources_fetched": len(items),
        "after_dedup": len(deduped),
        "after_known": after_known,
        "after_history": after_history,
        "after_release_date": after_release_date,
        "pool_injected": 0,
    }

    weights = rep_settings.scoring_weights()
    label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
    sections, label_artists = rank_candidates(
        candidates, profiles, rep_settings, label_seed=label_seed,
        genre_affinity=genre_affinity, label_memory=label_memory,
    )

    report_text = generate_report(
        sections, week, stats, rep_settings, profiles=profiles,
        label_artists=label_artists, today=ref_date, aliases=aliases,
    )

    recommended = report_order(sections)
    diff_lines = _diff_vs_history(recommended, history, week, remix_aware)

    header = [_BANNER, f"Week: {week} (reference date {ref_date.isoformat()})"]
    if overrides:
        header.append("Overrides: " + " ".join(overrides))
    header.append("")

    parts = header + [report_text, "", "=== DIFF vs recommendation_history ==="] + diff_lines
    return "\n".join(parts)
