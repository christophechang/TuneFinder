"""Run orchestration as a callable service layer.

The weekly and mix-prep pipelines used to live inline in the CLI handlers
(tunefinder/__main__.py cmd_run / cmd_mix_prep). They are extracted here so
the web API (src/web) and the CLI drive the exact same code path. Behaviour
is preserved verbatim — same stage order, same dry-run gating, same Discord
posts, same log lines — with three additions:

- an optional progress callback receiving (stage, detail) events,
- the data_dir run lock held for the whole run (storage.run_lock), so a
  web-triggered run and the launchd weekly run can never interleave,
- a structured report artifact built for every run and persisted on live
  runs (report_artifact.py).

Imports of externally-patched collaborators (fetch_all_sources, catalog
fetches, make_discord_client) stay function-local, matching the original
CLI style — tests patch them at their definition sites.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.logger import get_logger
from src.pipeline.storage import run_lock

# Single source of truth for mix-prep genres — the CLI argparse choices and
# the web API validation both import this.
MIX_PREP_GENRES = (
    "dnb", "breaks", "uk-bass", "house", "ukg", "electronica",
    "downtempo", "techno", "funk-soul-jazz", "hip-hop",
)

ProgressFn = Callable[[str, str], None]


@dataclass
class WeeklyRunOptions:
    dry_run: bool = False


@dataclass
class MixPrepOptions:
    genre: str
    bpm_range: Optional[tuple[float, float]] = None
    key_camelot: Optional[str] = None
    bpm_flex: bool = True
    dry_run: bool = False


@dataclass
class RunOutcome:
    kind: str                      # "weekly" | "mix-prep"
    report_id: str
    dry_run: bool
    recommended_count: int = 0
    duration_seconds: int = 0
    report_text: str = ""
    stats: dict = field(default_factory=dict)
    artifact: Optional[dict] = None       # always built when a report exists
    artifact_path: Optional[str] = None   # persisted path (live runs only)
    audition_path: Optional[str] = None
    no_candidates: bool = False


def _noop_progress(stage: str, detail: str) -> None:
    return None


def _load_profile_state(settings, logger, dry_run, post_alert_fn, remix_aware=False):
    """Refresh profile, genre affinity, and known-track state, with graceful fallback.

    Attempts to fetch fresh tracks from the catalog API. On success, builds and
    SAVES the fresh state (same behaviour as before issue #12). If the fetch
    fails, loads the last-saved state from data_dir instead and skips all
    save_* calls (nothing new to persist). Returns (profiles, genre_affinity,
    known_keys, used_fallback).

    Known-key equivalence: build_known_track_keys(tracks) produces normalised
    dedup keys, and data/known_tracks.json (written by save_known_tracks) stores
    exactly those keys — load_known_tracks returns the same set.

    post_alert_fn: callable(message: str) to post alerts. Called on live runs
    only (mirrors existing anomaly-alert gating); dry-run logs instead.
    """
    from src.fetchers.catalog import fetch_all_mixes, fetch_all_tracks
    from src.pipeline.profile import (
        apply_recency_weights, build_artist_profiles, build_genre_affinity, build_known_track_keys,
        save_known_tracks, save_artist_profiles, save_genre_affinity,
        load_artist_profiles, load_genre_affinity, load_known_tracks,
    )

    try:
        logger.info("[load_profile_state] Fetching tracks from catalog API...")
        tracks = fetch_all_tracks(settings)
    except Exception as exc:
        logger.error(f"[load_profile_state] fetch_all_tracks failed: {exc}")
        profiles = load_artist_profiles(settings.data_dir)
        genre_affinity = load_genre_affinity(settings.data_dir)
        known_keys = load_known_tracks(settings.data_dir)

        if profiles:
            alert_msg = (
                f"Profile fetch failed ({exc}) — proceeding with {len(profiles)} cached "
                f"artist profiles from last saved state. Check catalog API connectivity."
            )
        else:
            alert_msg = (
                f"Profile fetch failed ({exc}) — no cached profiles available either. "
                "Proceeding in discovery-only mode (no artist/label signals)."
            )

        if not dry_run:
            post_alert_fn(alert_msg)
        else:
            logger.warning(f"[load_profile_state] ALERT (dry-run, not posted): {alert_msg}")

        return profiles, genre_affinity, known_keys, True

    profiles = build_artist_profiles(tracks)
    genre_affinity = build_genre_affinity(tracks)
    known_keys = build_known_track_keys(tracks, remix_aware)

    # Taste recency weighting (issue #11) — best-effort: mixes fetch failure is
    # graceful degradation only (the track/profile fetch above already
    # succeeded), not the same class of failure as _load_profile_state's own
    # alerting above — no alert, profiles keep raw play_count scoring for this
    # run via the ranker's fallback.
    try:
        weights = settings.scoring_weights()
        mixes = fetch_all_mixes(settings)
        apply_recency_weights(profiles, mixes, weights.taste_half_life_months)
    except Exception as exc:
        logger.warning(f"[load_profile_state] mixes fetch failed — recency weights unavailable, using raw play counts ({exc})")

    save_known_tracks(tracks, settings.data_dir, remix_aware)
    save_artist_profiles(profiles, settings.data_dir)
    save_genre_affinity(genre_affinity, settings.data_dir)
    logger.info(f"[load_profile_state] Refreshed profile state from {len(tracks)} known tracks")
    return profiles, genre_affinity, known_keys, False


def run_weekly(settings, options: WeeklyRunOptions, progress: Optional[ProgressFn] = None) -> RunOutcome:
    """The weekly pipeline — extracted from cmd_run, behaviour-identical.

    Holds the data_dir run lock for the duration; raises
    storage.RunLockHeldError if another run is active.
    """
    from src.fetchers import fetch_all_sources, save_source_items, archive_source_items
    from src.pipeline.history import (
        load_history, build_history_keys, append_records, make_report_id,
    )
    from src.pipeline.dedup import (
        deduplicate_source_items, items_to_candidates,
        filter_known, filter_history, filter_release_date,
    )
    from src.pipeline.ranker import rank_candidates
    from src.pipeline.labels import (
        load_label_affinity, update_label_affinity, save_label_affinity, fresh_label_artist_data,
    )
    from src.pipeline.feedback import load_feedback, skipped_artists
    from src.pipeline.pool import load_pool, pool_to_candidates, save_pool, POOL_CAP
    from src.pipeline.report import generate_report, report_order
    from src.pipeline.report_artifact import build_report_artifact, write_report_artifact
    from src.pipeline.source_health import append_run_health, load_run_health, detect_anomalies
    from src.output.discord import make_discord_client
    from src.models import RecommendationRecord, PoolRecord
    from datetime import datetime, timezone

    dry_run = options.dry_run
    emit = progress or _noop_progress
    logger = get_logger(__name__)
    start = time.time()
    report_id = make_report_id()
    # Remix-aware track identity (issue #9) — read once, thread to every identity
    # call site. Default off ⇒ byte-identical to the legacy pipeline.
    remix_aware = settings.pipeline_remix_aware_identity
    logger.info(f"[run] Starting report run — {report_id}" + (" (DRY RUN)" if dry_run else ""))

    with run_lock(settings.data_dir):
        # Create discord client early for alerts (same as anomaly-alert gating pattern)
        discord = make_discord_client(settings)

        # 1. Refresh profile and known-track set (with degraded mode fallback)
        emit("profile", "Refreshing taste profile from catalog API")

        def _post_profile_alert(msg: str):
            if not dry_run:
                discord.post_alert(msg)

        profiles, genre_affinity, known_keys, used_fallback = _load_profile_state(
            settings, logger, dry_run, _post_profile_alert, remix_aware
        )
        if used_fallback:
            logger.warning("[run] Proceeding with last-saved profile state (degraded mode)")
            emit("profile", "Degraded mode — using last-saved profile state")

        # 1b. Load label affinity store (issue #5) — persisted artist<->label memory
        label_store = load_label_affinity(settings.data_dir)

        # 2. Load recommendation history and candidate pool
        history = load_history(settings.data_dir)
        history_keys = build_history_keys(history, remix_aware)
        pool_records = load_pool(settings.data_dir)

        # 3. Fetch external sources
        emit("sources", "Fetching enabled sources")
        source_items, fetcher_health = fetch_all_sources(settings)
        save_source_items(source_items, settings.data_dir)
        archive_source_items(source_items, settings.data_dir, report_id)
        sources_fetched = len(source_items)
        emit("sources", f"Fetched {sources_fetched} items")

        # 3b. Anomaly detection — load prior health before appending current run
        prior_health_runs = load_run_health(settings.data_dir)
        anomalies = detect_anomalies(
            fetcher_health, prior_health_runs,
            settings.alerts_source_drop_threshold_pct,
            settings.alerts_min_history_runs,
        )
        if not dry_run:
            append_run_health(fetcher_health, settings.data_dir, report_id)
            if anomalies:
                discord_early = make_discord_client(settings)
                discord_early.post_alert("\n".join(anomalies))
        else:
            for msg in anomalies:
                logger.warning(f"[run] ANOMALY (dry-run, not posted): {msg}")

        # 4. Dedup + filter
        emit("filter", "Deduplicating and filtering candidates")
        source_items = deduplicate_source_items(source_items, remix_aware)
        after_dedup = len(source_items)
        candidates = items_to_candidates(source_items)
        label_seed = list(candidates)  # capture before filtering so known artists inform label relevance
        candidates = filter_known(candidates, known_keys, remix_aware)
        after_known = len(candidates)
        candidates = filter_history(candidates, history_keys, remix_aware)
        after_history = len(candidates)
        window_days = settings.pipeline_release_date_window_days
        if window_days:
            candidates = filter_release_date(candidates, window_days)
        after_release_date = len(candidates)

        # Inject pool candidates (skip any already present as fresh tracks)
        fresh_candidates = list(candidates)
        fresh_keys = {c.key for c in fresh_candidates}
        pool_injected = [
            c for c in pool_to_candidates([r for r in pool_records if r.key not in fresh_keys])
            if c.key not in known_keys and c.key not in history_keys
        ]
        all_candidates = fresh_candidates + pool_injected
        candidates = all_candidates

        stats = {
            "sources_fetched": sources_fetched,
            "raw_count": sources_fetched,
            "after_dedup": after_dedup,
            "after_known": after_known,
            "after_history": after_history,
            "after_release_date": after_release_date,
            "pool_injected": len(pool_injected),
            "fetcher_health": fetcher_health,
        }

        if not candidates:
            logger.warning("[run] No candidates remaining after filtering — nothing to report")
            if not dry_run:
                discord = make_discord_client(settings)
                discord.post_alert(f"Run {report_id}: no candidates after filtering. Check sources.")
            else:
                logger.warning(f"[run] ALERT (dry-run, not posted): Run {report_id}: no candidates after filtering. Check sources.")
            return RunOutcome(
                kind="weekly", report_id=report_id, dry_run=dry_run,
                duration_seconds=int(time.time() - start), stats=stats, no_candidates=True,
            )

        # 5. Rank and split into sections
        emit("rank", f"Scoring {len(candidates)} candidates")
        weights = settings.scoring_weights()
        label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
        # Skip-derived negative signal (issue #11) — artists with repeated 'skip'
        # marks and no positives get a soft penalty. See src/pipeline/feedback.skipped_artists.
        feedback_entries = load_feedback(settings.data_dir)
        skip_set = skipped_artists(feedback_entries, weights.skipped_artist_min_skips)
        sections, label_artists = rank_candidates(
            candidates, profiles, settings, label_seed=label_seed, genre_affinity=genre_affinity,
            label_memory=label_memory, skip_penalty_artists=skip_set,
        )
        aliases = settings.artist_aliases()

        # 5b. Update label affinity store from this run's label_seed (live runs only —
        # a dry-run must not persist state it didn't actually recommend from).
        now_iso = datetime.now(timezone.utc).isoformat()
        if not dry_run:
            profiles_lower = {k.lower(): v for k, v in profiles.items()}
            label_store = update_label_affinity(label_store, label_seed, profiles_lower, aliases, now_iso)
            save_label_affinity(label_store, settings.data_dir)
        else:
            logger.info("[run] DRY RUN — label affinity store not updated")

        # 6. Generate report + structured artifact
        emit("report", "Generating report")
        report_text = generate_report(sections, report_id, stats, settings, profiles=profiles, label_artists=label_artists, aliases=aliases)
        artifact = build_report_artifact(
            sections, report_id, "weekly", stats,
            profiles=profiles, label_artists=label_artists, aliases=aliases, dry_run=dry_run,
        )
        artifact_path = None
        if not dry_run:
            artifact_path = write_report_artifact(artifact, settings.data_dir)
            logger.info(f"[run] Report artifact written: {artifact_path}")

        # 6b. Write audition page (live runs only)
        audition_path = None
        if not dry_run:
            from src.pipeline.audition import generate_audition_page, write_audition_page
            audition_html = generate_audition_page(sections, report_id, settings, profiles=profiles, label_artists=label_artists, aliases=aliases)
            audition_path = write_audition_page(audition_html, settings.data_dir, report_id)
            logger.info(f"[run] Audition page written: {audition_path}")
        else:
            logger.info("[run] DRY RUN — audition page not written")

        # 7. Post to Discord (skipped in dry-run)
        emit("deliver", "Posting report")
        if dry_run:
            report_text = "🧪 **[DRY RUN — history not updated]**\n\n" + report_text
            logger.info("[run] DRY RUN — skipping Discord post. Report preview follows:")
            logger.info("\n" + report_text)
        else:
            discord = make_discord_client(settings)
            discord.post_report(report_text)

        # 8. Update recommendation history and rebuild candidate pool (skipped in dry-run)
        recommended = report_order(sections)
        new_records = [
            RecommendationRecord(
                artist=c.artist,
                title=c.title,
                link=c.link,
                source=c.source,
                recommended_at=now_iso,
                report_id=report_id,
                track_no=i,
                signal_codes=[s.code for s in c.signals],
                genre_tags=c.genre_tags,
                score=c.score,
                label=c.label,
            )
            for i, c in enumerate(recommended, start=1)
        ]
        recommended_keys = {c.key for c in recommended}
        existing_added_at = {r.key: r.added_at for r in pool_records}
        unselected = sorted(
            [c for c in all_candidates if c.key not in recommended_keys],
            key=lambda c: c.score,
            reverse=True,
        )[:POOL_CAP]
        new_pool = [
            PoolRecord(
                artist=c.artist,
                title=c.title,
                link=c.link,
                source=c.source,
                label=c.label,
                release_date=c.release_date,
                release_name=c.release_name,
                genre_tags=c.genre_tags,
                raw_metadata=c.raw_metadata,
                added_at=existing_added_at.get(c.key, now_iso),
                last_score=c.score,
            )
            for c in unselected
        ]
        if not dry_run:
            append_records(new_records, settings.data_dir)
            save_pool(new_pool, settings.data_dir)

        # 9. Post run summary to log channel (skipped in dry-run)
        duration = int(time.time() - start)
        by_source: dict[str, int] = {}
        for item in source_items:
            by_source[item.source] = by_source.get(item.source, 0) + 1
        source_summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))
        date_filter_note = (
            f"release date filter: ≤{window_days}d (Bandcamp exempt)"
            if window_days else "release date filter: off"
        )
        log_msg = (
            f"**Run complete** — {report_id} | {duration}s\n"
            f"Sources: {source_summary}\n"
            f"Candidates: {sources_fetched} → {after_dedup} deduped → "
            f"{after_known} after known filter → {after_history} after history → "
            f"{after_release_date} after {date_filter_note}\n"
            f"Pool: {len(pool_injected)} injected, {len(new_pool)} total (cap {POOL_CAP})\n"
            f"Recommended: {len(new_records)} tracks"
        )
        if not dry_run:
            discord.post_log(log_msg)

        emit("done", f"{len(new_records)} tracks recommended")
        return RunOutcome(
            kind="weekly", report_id=report_id, dry_run=dry_run,
            recommended_count=len(new_records), duration_seconds=duration,
            report_text=report_text, stats=stats,
            artifact=artifact, artifact_path=artifact_path, audition_path=audition_path,
        )


def run_mix_prep(settings, options: MixPrepOptions, progress: Optional[ProgressFn] = None) -> RunOutcome:
    """The mix-prep pipeline — extracted from cmd_mix_prep, behaviour-identical.

    Callers validate genre/bpm/key up front (CLI: argparse + fail-fast parses;
    API: request validation). Holds the data_dir run lock for the duration.
    """
    from src.fetchers import fetch_all_sources
    from src.pipeline.history import (
        load_mix_prep_history, build_history_keys, append_mix_prep_records, make_report_id,
    )
    from src.pipeline.dedup import (
        deduplicate_source_items, items_to_candidates,
        filter_known, filter_genre, filter_genre_exclusions, filter_release_date,
    )
    from src.pipeline.ranker import rank_candidates_mix_prep
    from src.pipeline.labels import (
        load_label_affinity, update_label_affinity, save_label_affinity, fresh_label_artist_data,
    )
    from src.pipeline.feedback import load_feedback, skipped_artists
    from src.pipeline.pool import load_pool, pool_to_candidates
    from src.pipeline.report import generate_mix_prep_report, report_order
    from src.pipeline.report_artifact import build_report_artifact, write_report_artifact
    from src.pipeline.harmonic import partition_by_harmonic
    from src.output.discord import make_discord_client
    from src.models import RecommendationRecord
    from datetime import datetime, timezone

    genre = options.genre
    dry_run = options.dry_run
    bpm_range = options.bpm_range
    key_camelot = options.key_camelot
    bpm_flex = options.bpm_flex
    emit = progress or _noop_progress

    logger = get_logger(__name__)
    start = time.time()
    remix_aware = settings.pipeline_remix_aware_identity  # issue #9 — default off
    report_id = f"{make_report_id()}-mix-prep-{genre}"
    logger.info(f"[mix-prep] Starting mix-prep run — genre: {genre} — {report_id}" + (" (DRY RUN)" if dry_run else ""))

    with run_lock(settings.data_dir):
        # Create discord client early for alerts (same as anomaly-alert gating pattern)
        discord = make_discord_client(settings)

        # 1. Refresh profile and known-track set (with degraded mode fallback)
        emit("profile", "Refreshing taste profile from catalog API")

        def _post_profile_alert(msg: str):
            if not dry_run:
                discord.post_alert(msg)

        profiles, genre_affinity, known_keys, used_fallback = _load_profile_state(
            settings, logger, dry_run, _post_profile_alert, remix_aware
        )
        if used_fallback:
            logger.warning("[mix-prep] Proceeding with last-saved profile state (degraded mode)")
            emit("profile", "Degraded mode — using last-saved profile state")

        # 1b. Load label affinity store (issue #5) — persisted artist<->label memory
        label_store = load_label_affinity(settings.data_dir)

        # 2. Load mix-prep history (separate from weekly history)
        mix_prep_history = load_mix_prep_history(settings.data_dir)
        mix_prep_history_keys = build_history_keys(mix_prep_history, remix_aware)

        # 3. Fetch external sources
        emit("sources", f"Fetching sources for {genre}")
        source_items, fetcher_health = fetch_all_sources(settings, target_genre=genre)
        sources_fetched = len(source_items)
        emit("sources", f"Fetched {sources_fetched} items")

        # 4. Dedup + filter + genre narrow
        emit("filter", "Deduplicating and filtering candidates")
        source_items = deduplicate_source_items(source_items, remix_aware)
        after_dedup = len(source_items)
        candidates = items_to_candidates(source_items)
        label_seed = list(candidates)
        candidates = filter_known(candidates, known_keys, remix_aware)
        candidates = [c for c in candidates if c.key not in mix_prep_history_keys]
        candidates = filter_genre(candidates, genre)
        candidates = filter_genre_exclusions(candidates, genre, settings.pipeline_genre_exclusions)
        window_days = settings.pipeline_release_date_window_days
        if window_days:
            candidates = filter_release_date(candidates, window_days)
        after_genre = len(candidates)

        # Inject pool candidates for this genre
        pool_records = load_pool(settings.data_dir)
        fresh_keys = {c.key for c in candidates}
        _pool = filter_genre(
            pool_to_candidates([r for r in pool_records if r.key not in fresh_keys]),
            genre,
        )
        _pool = filter_genre_exclusions(_pool, genre, settings.pipeline_genre_exclusions)
        # Pool injection is deliberately exempt from the release-date window (same as the weekly run) — the pool-age penalty handles staleness. See docs/scoring-review.md §2.5.
        pool_injected = [
            c for c in _pool
            if c.key not in known_keys and c.key not in mix_prep_history_keys
        ]
        candidates = candidates + pool_injected

        # BPM/key filter (issue #8) — applied after all existing filters + pool
        # injection, before ranking. Candidates with a KNOWN value that fails a
        # specified filter are dropped; candidates with an UNKNOWN value for a
        # specified filter are kept but demoted below every match (never dropped
        # for missing data — coverage is partial). See src/pipeline/harmonic.py.
        demoted_keys = None
        after_harmonic = None
        if bpm_range is not None or key_camelot is not None:
            before_harmonic = len(candidates)
            matches, unknowns = partition_by_harmonic(candidates, bpm_range, key_camelot, bpm_flex)
            candidates = matches + unknowns
            demoted_keys = {c.key for c in unknowns}
            after_harmonic = len(candidates)
            logger.info(
                f"[mix-prep] BPM/key filter: {len(matches)} matches, {len(unknowns)} demoted (unknown), "
                f"{before_harmonic - after_harmonic} dropped"
            )

        stats = {
            "sources_fetched": sources_fetched,
            "after_dedup": after_dedup,
            "after_genre": after_genre,
            "pool_injected": len(pool_injected),
            "fetcher_health": fetcher_health,
        }
        if after_harmonic is not None:
            stats["after_harmonic"] = after_harmonic

        if not candidates:
            logger.warning(f"[mix-prep] No {genre} candidates after filtering — nothing to report")
            if not dry_run:
                discord = make_discord_client(settings)
                discord.post_alert(f"Mix-prep {report_id}: no candidates found for genre '{genre}'. Check sources.")
            else:
                logger.warning(f"[mix-prep] ALERT (dry-run, not posted): Mix-prep {report_id}: no candidates found for genre '{genre}'. Check sources.")
            return RunOutcome(
                kind="mix-prep", report_id=report_id, dry_run=dry_run,
                duration_seconds=int(time.time() - start), stats=stats, no_candidates=True,
            )

        # 5. Rank and section
        emit("rank", f"Scoring {len(candidates)} candidates")
        weights = settings.scoring_weights()
        label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
        # Skip-derived negative signal (issue #11) — see run_weekly.
        feedback_entries = load_feedback(settings.data_dir)
        skip_set = skipped_artists(feedback_entries, weights.skipped_artist_min_skips)
        sections, label_artists = rank_candidates_mix_prep(
            candidates, profiles, settings, label_seed=label_seed, genre_affinity=genre_affinity,
            label_memory=label_memory, demoted_keys=demoted_keys, skip_penalty_artists=skip_set,
        )
        aliases = settings.artist_aliases()

        # 5b. Update label affinity store from this run's label_seed (live runs only).
        now_iso = datetime.now(timezone.utc).isoformat()
        if not dry_run:
            profiles_lower = {k.lower(): v for k, v in profiles.items()}
            label_store = update_label_affinity(label_store, label_seed, profiles_lower, aliases, now_iso)
            save_label_affinity(label_store, settings.data_dir)
        else:
            logger.info("[mix-prep] DRY RUN — label affinity store not updated")

        # 6. Generate report + structured artifact
        emit("report", "Generating report")
        filters_desc = None
        filters_payload = None
        if bpm_range is not None or key_camelot is not None:
            parts = []
            if bpm_range is not None:
                lo, hi = bpm_range
                flex_note = " (±half/double)" if bpm_flex else ""
                parts.append(f"BPM {lo:g}–{hi:g}{flex_note}")
            if key_camelot is not None:
                parts.append(f"key {key_camelot}±compat")
            filters_desc = "Filters: " + " · ".join(parts)
            filters_payload = {
                "bpm_min": bpm_range[0] if bpm_range else None,
                "bpm_max": bpm_range[1] if bpm_range else None,
                "bpm_flex": bpm_flex if bpm_range else None,
                "key_camelot": key_camelot,
                "description": filters_desc,
            }
        report_text = generate_mix_prep_report(
            sections, report_id, stats, genre, settings, profiles=profiles, label_artists=label_artists,
            aliases=aliases, filters_desc=filters_desc,
        )
        artifact = build_report_artifact(
            sections, report_id, "mix-prep", stats,
            profiles=profiles, label_artists=label_artists, aliases=aliases,
            genre=genre, filters=filters_payload, dry_run=dry_run,
        )
        artifact_path = None
        if not dry_run:
            artifact_path = write_report_artifact(artifact, settings.data_dir)
            logger.info(f"[mix-prep] Report artifact written: {artifact_path}")

        # 6b. Write audition page (live runs only)
        audition_path = None
        if not dry_run:
            from src.pipeline.audition import generate_audition_page, write_audition_page
            audition_html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                                   label_artists=label_artists, mark_by_number=False, aliases=aliases)
            audition_path = write_audition_page(audition_html, settings.data_dir, report_id)
            logger.info(f"[mix-prep] Audition page written: {audition_path}")
        else:
            logger.info("[mix-prep] DRY RUN — audition page not written")

        # 7. Post to mix-prep Discord channel (skipped in dry-run)
        emit("deliver", "Posting report")
        if dry_run:
            report_text = "🧪 **[DRY RUN — history not updated]**\n\n" + report_text
            logger.info("[mix-prep] DRY RUN — skipping Discord post. Report preview follows:")
            logger.info("\n" + report_text)
        else:
            discord = make_discord_client(settings)
            discord.post(settings.discord_mix_prep_channel, report_text)

        # 8. Save to mix-prep history (skipped in dry-run)
        recommended = report_order(sections)
        new_records = [
            RecommendationRecord(
                artist=c.artist,
                title=c.title,
                link=c.link,
                source=c.source,
                recommended_at=now_iso,
                report_id=report_id,
                track_no=i,
                signal_codes=[s.code for s in c.signals],
                genre_tags=c.genre_tags,
                score=c.score,
                label=c.label,
            )
            for i, c in enumerate(recommended, start=1)
        ]
        if not dry_run:
            append_mix_prep_records(new_records, settings.data_dir)

        duration = int(time.time() - start)
        emit("done", f"{len(new_records)} tracks recommended")
        return RunOutcome(
            kind="mix-prep", report_id=report_id, dry_run=dry_run,
            recommended_count=len(new_records), duration_seconds=duration,
            report_text=report_text, stats=stats,
            artifact=artifact, artifact_path=artifact_path, audition_path=audition_path,
        )
