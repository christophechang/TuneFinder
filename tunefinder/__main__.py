import argparse
import os
import re
import sys

# Add project root to sys.path so `from src.xxx` works everywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import setup_logging, get_logger
from src.config import load_settings


def _parse_bpm_range(raw: str) -> tuple[float, float]:
    """Parse a `mix-prep --bpm MIN-MAX` value (e.g. "170-180") into (lo, hi).

    Raises ValueError with a clean, user-facing message on anything that
    isn't two non-negative numbers separated by a hyphen, or where min > max.
    """
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", raw)
    if not m:
        raise ValueError(f"invalid --bpm value {raw!r} — expected MIN-MAX, e.g. 170-180")
    lo, hi = float(m.group(1)), float(m.group(2))
    if lo > hi:
        raise ValueError(f"invalid --bpm range {raw!r} — min ({lo:g}) must be <= max ({hi:g})")
    return lo, hi


def cmd_check_config(args):
    settings = load_settings()
    settings.validate()

    from src.config import _REQUIRED_ENV_VARS
    print("\nEnvironment variables:")
    for key in _REQUIRED_ENV_VARS:
        status = "SET" if os.getenv(key) else "MISSING"
        print(f"  {key:<30}  {status}")
    print("\nReport generation: deterministic (no LLM)")
    print("")


def cmd_save_fixtures(args):
    from src.fetchers.catalog import save_fixtures
    settings = load_settings()
    save_fixtures(settings)
    print(f"Fixtures saved to {settings.testing_fixtures_dir}/")


def cmd_build_profile(args):
    from src.fetchers.catalog import fetch_all_tracks
    from src.pipeline.profile import (
        build_artist_profiles,
        build_genre_affinity,
        save_known_tracks,
        save_artist_profiles,
        save_genre_affinity,
    )
    settings = load_settings()
    logger = get_logger(__name__)

    logger.info("[build-profile] Fetching tracks...")
    tracks = fetch_all_tracks(settings)

    logger.info("[build-profile] Building artist profiles...")
    profiles = build_artist_profiles(tracks)
    genre_affinity = build_genre_affinity(tracks)

    save_known_tracks(tracks, settings.data_dir)
    save_artist_profiles(profiles, settings.data_dir)
    save_genre_affinity(genre_affinity, settings.data_dir)

    print(f"Profile built — {len(tracks)} known tracks, {len(profiles)} artists, "
          f"{len(genre_affinity)} genres → {settings.data_dir}/")


def cmd_fetch_sources(args):
    from src.fetchers import fetch_all_sources, save_source_items
    settings = load_settings()
    logger = get_logger(__name__)

    logger.info("[fetch-sources] Starting source fetch...")
    items, health = fetch_all_sources(settings)
    save_source_items(items, settings.data_dir)

    print(f"Fetched {len(items)} items → {settings.data_dir}/source_items.json")
    for source, info in sorted(health.items()):
        status = f"❌ ERROR: {info['error']}" if info["error"] else f"{info['count']} tracks"
        print(f"  {source}: {status}")


def cmd_run(args):
    import time
    from src.fetchers.catalog import fetch_all_tracks
    from src.fetchers import fetch_all_sources, save_source_items, archive_source_items
    from src.pipeline.profile import (
        build_artist_profiles, build_genre_affinity, build_known_track_keys,
        save_known_tracks, save_artist_profiles, save_genre_affinity,
    )
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
    from src.pipeline.pool import load_pool, pool_to_candidates, save_pool, POOL_CAP
    from src.pipeline.report import generate_report, report_order
    from src.pipeline.source_health import append_run_health, load_run_health, detect_anomalies
    from src.output.discord import make_discord_client
    from src.models import RecommendationRecord, PoolRecord
    from datetime import datetime, timezone

    dry_run = getattr(args, "dry_run", False)
    settings = load_settings()
    settings.validate()
    logger = get_logger(__name__)
    start = time.time()
    report_id = make_report_id()
    logger.info(f"[run] Starting report run — {report_id}" + (" (DRY RUN)" if dry_run else ""))

    # 1. Refresh profile and known-track set
    tracks = fetch_all_tracks(settings)
    profiles = build_artist_profiles(tracks)
    genre_affinity = build_genre_affinity(tracks)
    save_known_tracks(tracks, settings.data_dir)
    save_artist_profiles(profiles, settings.data_dir)
    save_genre_affinity(genre_affinity, settings.data_dir)
    known_keys = build_known_track_keys(tracks)

    # 1b. Load label affinity store (issue #5) — persisted artist<->label memory
    label_store = load_label_affinity(settings.data_dir)

    # 2. Load recommendation history and candidate pool
    history = load_history(settings.data_dir)
    history_keys = build_history_keys(history)
    pool_records = load_pool(settings.data_dir)

    # 3. Fetch external sources
    source_items, fetcher_health = fetch_all_sources(settings)
    save_source_items(source_items, settings.data_dir)
    archive_source_items(source_items, settings.data_dir, report_id)
    sources_fetched = len(source_items)

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
    source_items = deduplicate_source_items(source_items)
    after_dedup = len(source_items)
    candidates = items_to_candidates(source_items)
    label_seed = list(candidates)  # capture before filtering so known artists inform label relevance
    candidates = filter_known(candidates, known_keys)
    after_known = len(candidates)
    candidates = filter_history(candidates, history_keys)
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
        return

    # 5. Rank and split into sections
    weights = settings.scoring_weights()
    label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
    sections, label_artists = rank_candidates(
        candidates, profiles, settings, label_seed=label_seed, genre_affinity=genre_affinity,
        label_memory=label_memory,
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

    # 6. Generate report
    report_text = generate_report(sections, report_id, stats, settings, profiles=profiles, label_artists=label_artists, aliases=aliases)

    # 6b. Write audition page (live runs only)
    if not dry_run:
        from src.pipeline.audition import generate_audition_page, write_audition_page
        audition_html = generate_audition_page(sections, report_id, settings, profiles=profiles, label_artists=label_artists, aliases=aliases)
        audition_path = write_audition_page(audition_html, settings.data_dir, report_id)
        logger.info(f"[run] Audition page written: {audition_path}")
    else:
        logger.info("[run] DRY RUN — audition page not written")

    # 7. Post to Discord (skipped in dry-run)
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
    print(f"Run complete — {report_id} — {len(new_records)} tracks recommended in {duration}s"
          + (" (DRY RUN — no writes)" if dry_run else ""))


def cmd_mix_prep(args):
    import time
    from src.fetchers.catalog import fetch_all_tracks
    from src.fetchers import fetch_all_sources
    from src.pipeline.profile import (
        build_artist_profiles, build_genre_affinity, build_known_track_keys,
        save_known_tracks, save_artist_profiles, save_genre_affinity,
    )
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
    from src.pipeline.pool import load_pool, pool_to_candidates
    from src.pipeline.report import generate_mix_prep_report, report_order
    from src.pipeline.harmonic import to_camelot, partition_by_harmonic
    from src.output.discord import make_discord_client
    from src.models import RecommendationRecord
    from datetime import datetime, timezone

    genre = args.genre
    dry_run = getattr(args, "dry_run", False)

    # BPM/key filters (issue #8) — parsed and validated up front, before any
    # fetching, so a bad --bpm/--key value fails fast with a clean message.
    bpm_range = None
    bpm_arg = getattr(args, "bpm", None)
    if bpm_arg:
        try:
            bpm_range = _parse_bpm_range(bpm_arg)
        except ValueError as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)

    key_camelot = None
    key_arg = getattr(args, "key", None)
    if key_arg:
        key_camelot = to_camelot(key_arg)
        if key_camelot is None:
            print(
                f"Error: could not parse --key {key_arg!r} — use Camelot notation "
                "(e.g. 8A) or a musical key (e.g. Am, C major)"
            )
            raise SystemExit(1)

    bpm_flex = not getattr(args, "no_bpm_flex", False)

    settings = load_settings()
    settings.validate()
    logger = get_logger(__name__)
    start = time.time()
    report_id = f"{make_report_id()}-mix-prep-{genre}"
    logger.info(f"[mix-prep] Starting mix-prep run — genre: {genre} — {report_id}" + (" (DRY RUN)" if dry_run else ""))

    # 1. Refresh profile and known-track set
    tracks = fetch_all_tracks(settings)
    profiles = build_artist_profiles(tracks)
    genre_affinity = build_genre_affinity(tracks)
    save_known_tracks(tracks, settings.data_dir)
    save_artist_profiles(profiles, settings.data_dir)
    save_genre_affinity(genre_affinity, settings.data_dir)
    known_keys = build_known_track_keys(tracks)

    # 1b. Load label affinity store (issue #5) — persisted artist<->label memory
    label_store = load_label_affinity(settings.data_dir)

    # 2. Load mix-prep history (separate from weekly history)
    mix_prep_history = load_mix_prep_history(settings.data_dir)
    mix_prep_history_keys = build_history_keys(mix_prep_history)

    # 3. Fetch external sources
    source_items, fetcher_health = fetch_all_sources(settings, target_genre=genre)
    sources_fetched = len(source_items)

    # 4. Dedup + filter + genre narrow
    source_items = deduplicate_source_items(source_items)
    after_dedup = len(source_items)
    candidates = items_to_candidates(source_items)
    label_seed = list(candidates)
    candidates = filter_known(candidates, known_keys)
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
        return

    # 5. Rank and section
    weights = settings.scoring_weights()
    label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
    sections, label_artists = rank_candidates_mix_prep(
        candidates, profiles, settings, label_seed=label_seed, genre_affinity=genre_affinity,
        label_memory=label_memory, demoted_keys=demoted_keys,
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

    # 6. Generate report
    filters_desc = None
    if bpm_range is not None or key_camelot is not None:
        parts = []
        if bpm_range is not None:
            lo, hi = bpm_range
            flex_note = " (±half/double)" if bpm_flex else ""
            parts.append(f"BPM {lo:g}–{hi:g}{flex_note}")
        if key_camelot is not None:
            parts.append(f"key {key_camelot}±compat")
        filters_desc = "Filters: " + " · ".join(parts)
    report_text = generate_mix_prep_report(
        sections, report_id, stats, genre, settings, profiles=profiles, label_artists=label_artists,
        aliases=aliases, filters_desc=filters_desc,
    )

    # 6b. Write audition page (live runs only)
    if not dry_run:
        from src.pipeline.audition import generate_audition_page, write_audition_page
        audition_html = generate_audition_page(sections, report_id, settings, profiles=profiles,
                                               label_artists=label_artists, mark_by_number=False, aliases=aliases)
        audition_path = write_audition_page(audition_html, settings.data_dir, report_id)
        logger.info(f"[mix-prep] Audition page written: {audition_path}")
    else:
        logger.info("[mix-prep] DRY RUN — audition page not written")

    # 7. Post to mix-prep Discord channel (skipped in dry-run)
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
    print(f"Mix-prep complete — {report_id} — {len(new_records)} tracks in {duration}s"
          + (" (DRY RUN — no writes)" if dry_run else ""))


def cmd_mark(args):
    from src.pipeline.feedback import (
        OUTCOMES, load_feedback, append_feedback, resolve_selector, FeedbackEntry,
    )
    from src.pipeline.history import load_history, load_mix_prep_history
    from src.pipeline.dedup import make_dedup_key
    from datetime import datetime, timezone

    settings = load_settings()
    logger = get_logger(__name__)

    weekly = load_history(settings.data_dir)
    mix_prep = load_mix_prep_history(settings.data_dir)

    try:
        record, history_name = resolve_selector(args.selector, weekly, mix_prep)
    except LookupError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    entry_key = make_dedup_key(record.artist, record.title)
    existing = load_feedback(settings.data_dir)

    # Check for a previous outcome on the same (history, key)
    prev = [e for e in existing if e.history == history_name and e.key == entry_key]
    if prev:
        latest_prev = max(prev, key=lambda e: e.marked_at)
        print(f"Note: previously marked as {latest_prev.outcome!r} — appending new mark.")

    now_iso = datetime.now(timezone.utc).isoformat()
    entry = FeedbackEntry(
        key=entry_key,
        artist=record.artist,
        title=record.title,
        outcome=args.outcome,
        marked_at=now_iso,
        report_id=record.report_id,
        track_no=record.track_no,
        history=history_name,
    )
    append_feedback(entry, settings.data_dir)

    # Derive week label for confirmation
    try:
        dt = datetime.fromisoformat(record.recommended_at)
        year, week, _ = dt.isocalendar()
        week_label = f"{year}-W{week:02d}"
    except Exception:
        week_label = record.report_id

    track_label = f"#{record.track_no} " if record.track_no is not None else ""
    print(f"Marked {track_label}{record.artist} — {record.title} as {args.outcome} ({week_label})")
    logger.info(f"[mark] {history_name}/{entry_key} → {args.outcome}")


def cmd_explain(args):
    from src.pipeline.explain import explain_track

    settings = load_settings()
    # No settings.validate() — works without Discord env vars
    output = explain_track(args.selector, settings)
    print(output)


def cmd_backfill_labels(args):
    """Replay archived source_items_*.json.gz snapshots into the label affinity
    store (issue #5) so historical weeks seed Label Watch memory instead of
    starting empty. No Discord, no settings.validate() — offline and idempotent
    (re-running converges to the same store since each archive's associations
    are keyed by label+artist and simply overwritten with the same values).

    Each archive predates report_id-level timestamps in the store format, so
    the archive file's own mtime (set at write time by archive_source_items)
    is used as a stable pseudo-timestamp for that week's associations — an
    honest approximation, documented here rather than invented precision.
    """
    from src.fetchers import list_archive_files, load_archived_source_items
    from src.pipeline.dedup import items_to_candidates
    from src.pipeline.profile import load_artist_profiles
    from src.pipeline.labels import load_label_affinity, update_label_affinity, save_label_affinity
    from datetime import datetime, timezone

    settings = load_settings()
    # No settings.validate() — offline, no Discord/env needed.
    logger = get_logger(__name__)

    profiles = load_artist_profiles(settings.data_dir)
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    aliases = settings.artist_aliases()

    archive_files = list_archive_files(settings.data_dir)
    if not archive_files:
        print(f"No archived source_items found under {settings.data_dir}/archive/ — nothing to backfill.")
        return

    store = load_label_affinity(settings.data_dir)
    for path in archive_files:
        mtime_iso = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
        items = load_archived_source_items(path)
        candidates = items_to_candidates(items)
        store = update_label_affinity(store, candidates, profiles_lower, aliases, mtime_iso)
        logger.info(f"[backfill-labels] Replayed {path} ({len(items)} items, as-of {mtime_iso})")

    save_label_affinity(store, settings.data_dir)

    label_count = len(store)
    association_count = sum(len(entry.get("artists", {})) for entry in store.values())
    print(
        f"Backfilled {len(archive_files)} archives → {label_count} labels, "
        f"{association_count} artist associations → {settings.data_dir}/label_affinity.json"
    )


def cmd_stats(args):
    from src.pipeline.feedback import load_feedback, summarise_feedback
    from src.pipeline.history import load_history, load_mix_prep_history

    settings = load_settings()

    entries = load_feedback(settings.data_dir)
    if not entries:
        print("No feedback recorded yet — mark tracks with `tunefinder mark`")
        return

    weekly = load_history(settings.data_dir)
    mix_prep = load_mix_prep_history(settings.data_dir)
    stats = summarise_feedback(weekly, mix_prep, entries)

    for hist_name, label in (("weekly", "Weekly"), ("mix_prep", "Mix-Prep")):
        bucket = stats.get(hist_name, {})
        if not bucket:
            continue
        print(f"\n=== {label} ===")
        print(f"Recommended: {bucket['recommended']}  |  "
              f"Marked: {bucket['marked']}  |  "
              f"Coverage: {bucket['coverage_pct']}%  |  "
              f"Positive rate: {bucket['positive_rate']}%")
        own = bucket.get("own_count", 0)
        if own:
            print(f"Own (identity-gap misses): {own}")

        def _print_section(title: str, d: dict) -> None:
            if not d:
                return
            print(f"\n  {title}:")
            for name, counts in sorted(d.items(), key=lambda kv: -kv[1]["marked"]):
                pos = counts["positive"]
                tot = counts["marked"]
                print(f"    {name}: {tot} marked, {pos} positive")

        _print_section("By signal", bucket.get("by_signal", {}))
        _print_section("By source", bucket.get("by_source", {}))
        _print_section("By genre", bucket.get("by_genre", {}))
        _print_section("By report", bucket.get("by_report", {}))


def main():
    from src.pipeline.feedback import OUTCOMES
    parser = argparse.ArgumentParser(
        prog="tunefinder",
        description="TuneFinder — weekly music discovery automation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-config", help="Validate all required env vars and config")
    subparsers.add_parser("save-fixtures", help="Fetch live API data and save to fixtures/ for offline testing")
    subparsers.add_parser("build-profile", help="Build artist profiles and known-track exclusion set from mix history")
    subparsers.add_parser("fetch-sources", help="Fetch candidate music from all enabled external sources")
    run_parser = subparsers.add_parser("run", help="Run the full pipeline and post the weekly report to Discord")
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full pipeline, log the report preview, but skip Discord posts and history/pool writes",
    )
    mix_prep_parser = subparsers.add_parser(
        "mix-prep",
        help="Generate a genre-focused track list for mix preparation",
    )
    mix_prep_parser.add_argument(
        "genre",
        choices=["dnb", "breaks", "uk-bass", "house", "ukg", "electronica", "downtempo", "techno", "funk-soul-jazz", "hip-hop"],
        help="Genre to focus on",
    )
    mix_prep_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full pipeline but skip Discord posts and history writes",
    )
    mix_prep_parser.add_argument(
        "--bpm", metavar="MIN-MAX", default=None,
        help="Filter to a BPM range, e.g. 170-180. Half/double-time matches are "
             "included by default (e.g. 85 matches 170-180) — see --no-bpm-flex. "
             "Tracks with no known BPM are kept but demoted, never dropped.",
    )
    mix_prep_parser.add_argument(
        "--key", default=None,
        help="Filter to a Camelot code (e.g. 8A) or musical key (e.g. Am, C major, "
             "F# minor). Keeps exact matches, adjacent wheel positions (±1), and the "
             "relative major/minor. Tracks with no known key are kept but demoted, "
             "never dropped.",
    )
    mix_prep_parser.add_argument(
        "--no-bpm-flex", action="store_true",
        help="Disable half/double-time BPM matching (flex is on by default for --bpm).",
    )
    mark_parser = subparsers.add_parser("mark", help="Record an outcome for a recommended track")
    mark_parser.add_argument(
        "selector",
        help="Track number (from latest weekly report) or \"Artist - Title\"",
    )
    mark_parser.add_argument(
        "outcome",
        choices=list(OUTCOMES),
        help="Outcome to record",
    )
    subparsers.add_parser("stats", help="Show feedback statistics")
    explain_parser = subparsers.add_parser(
        "explain",
        help="Trace a track through the weekly pipeline offline",
    )
    explain_parser.add_argument(
        "selector",
        help="\"Artist - Title\" of the track to trace",
    )
    subparsers.add_parser(
        "backfill-labels",
        help="Replay archived source_items snapshots into the label affinity store",
    )

    args = parser.parse_args()

    setup_logging(log_dir="logs")
    logger = get_logger(__name__)
    logger.info(f"[main] Running command: {args.command}")

    if args.command == "check-config":
        cmd_check_config(args)
    elif args.command == "save-fixtures":
        cmd_save_fixtures(args)
    elif args.command == "build-profile":
        cmd_build_profile(args)
    elif args.command == "fetch-sources":
        cmd_fetch_sources(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "mix-prep":
        cmd_mix_prep(args)
    elif args.command == "mark":
        cmd_mark(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "explain":
        cmd_explain(args)
    elif args.command == "backfill-labels":
        cmd_backfill_labels(args)


if __name__ == "__main__":
    main()
