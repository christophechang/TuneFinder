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
    from src.fetchers.catalog import fetch_all_mixes, fetch_all_tracks
    from src.pipeline.profile import (
        apply_recency_weights,
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

    # Taste recency weighting (issue #11) — best-effort: a mixes-fetch failure
    # is graceful degradation (profile build itself already succeeded), not an
    # alert-worthy failure, so profiles simply keep whatever recency weights
    # were last saved (or 0.0 / raw play_count fallback for a fresh profile).
    try:
        weights = settings.scoring_weights()
        mixes = fetch_all_mixes(settings)
        apply_recency_weights(profiles, mixes, weights.taste_half_life_months)
    except Exception as exc:
        logger.warning(f"[build-profile] mixes fetch failed — recency weights unavailable, using raw play counts ({exc})")

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


# Run orchestration lives in src/services/runs.py (shared by CLI and web API).
# _load_profile_state is re-exported here for back-compat (tests import it from
# tunefinder.__main__).
from src.services.runs import _load_profile_state  # noqa: E402,F401


def cmd_run(args):
    from src.pipeline.storage import RunLockHeldError
    from src.services.runs import WeeklyRunOptions, run_weekly

    dry_run = getattr(args, "dry_run", False)
    settings = load_settings()
    settings.validate()

    try:
        outcome = run_weekly(settings, WeeklyRunOptions(dry_run=dry_run))
    except RunLockHeldError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    if outcome.no_candidates:
        return
    print(f"Run complete — {outcome.report_id} — {outcome.recommended_count} tracks recommended in {outcome.duration_seconds}s"
          + (" (DRY RUN — no writes)" if dry_run else ""))


def _parse_filter_args(args) -> tuple[tuple[float, float] | None, str | None, bool]:
    """Fail-fast --bpm/--key/--no-bpm-flex parsing shared by mix-prep and
    free-downloads. Exits with a clean message on invalid values."""
    from src.pipeline.harmonic import to_camelot

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

    return bpm_range, key_camelot, not getattr(args, "no_bpm_flex", False)


def cmd_mix_prep(args):
    from src.pipeline.storage import RunLockHeldError
    from src.services.runs import MixPrepOptions, run_mix_prep

    genre = args.genre
    dry_run = getattr(args, "dry_run", False)

    # BPM/key filters (issue #8) — parsed and validated up front, before any
    # fetching, so a bad --bpm/--key value fails fast with a clean message.
    bpm_range, key_camelot, bpm_flex = _parse_filter_args(args)

    settings = load_settings()
    settings.validate()

    options = MixPrepOptions(
        genre=genre, bpm_range=bpm_range, key_camelot=key_camelot,
        bpm_flex=bpm_flex, dry_run=dry_run,
    )
    try:
        outcome = run_mix_prep(settings, options)
    except RunLockHeldError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    if outcome.no_candidates:
        return
    print(f"Mix-prep complete — {outcome.report_id} — {outcome.recommended_count} tracks in {outcome.duration_seconds}s"
          + (" (DRY RUN — no writes)" if dry_run else ""))


def cmd_free_downloads(args):
    from src.pipeline.storage import RunLockHeldError
    from src.services.runs import MixPrepOptions, run_mix_prep

    bpm_range, key_camelot, bpm_flex = _parse_filter_args(args)
    dry_run = getattr(args, "dry_run", False)

    settings = load_settings()
    settings.validate()

    options = MixPrepOptions(
        genre=args.genre, bpm_range=bpm_range, key_camelot=key_camelot,
        bpm_flex=bpm_flex, dry_run=dry_run, free_only=True,
    )
    try:
        outcome = run_mix_prep(settings, options)
    except RunLockHeldError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    if outcome.no_candidates:
        return
    print(f"Free-downloads run complete — {outcome.report_id} — {outcome.recommended_count} tracks in {outcome.duration_seconds}s"
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


def cmd_replay(args):
    from src.pipeline.replay import replay_week

    settings = load_settings()
    # No settings.validate() — offline reconstruction, no Discord/env needed.
    output = replay_week(args.week, getattr(args, "overrides", []) or [], settings)
    print(output)


def cmd_tune_report(args):
    from src.pipeline.feedback import load_feedback, tune_report
    from src.pipeline.history import load_history, load_mix_prep_history

    settings = load_settings()
    # No settings.validate() — offline, works without Discord env vars.
    entries = load_feedback(settings.data_dir)
    if not entries:
        print("No feedback recorded yet — mark tracks with `tunefinder mark`")
        return

    weekly = load_history(settings.data_dir)
    mix_prep = load_mix_prep_history(settings.data_dir)
    print(tune_report(weekly, mix_prep, entries))


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


def cmd_serve(args):
    """Run the web API (and optionally the built SPA) with uvicorn.

    Does not hard-require Discord env vars — browsing works without them, and
    a web-triggered live run would simply skip Discord delivery (the client
    no-ops without a token, with a logged warning). check-config remains the
    place to verify full delivery credentials.
    """
    import uvicorn

    from src.web.app import create_app
    from src.web.auth import AuthConfigError

    settings = load_settings()
    logger = get_logger(__name__)
    try:
        settings.validate()
    except EnvironmentError as exc:
        logger.warning(f"[serve] {exc} — reports/feedback work; Discord delivery is disabled until set.")

    try:
        app = create_app(settings)
    except AuthConfigError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    print(f"TuneFinder web API on http://{args.host}:{args.port} — docs at /docs")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main():
    from src.pipeline.feedback import OUTCOMES
    from src.services.runs import MIX_PREP_GENRES
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
        choices=list(MIX_PREP_GENRES),
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
    free_dl_parser = subparsers.add_parser(
        "free-downloads",
        help="Genre-focused report of the best free downloads (SoundCloud native + gated)",
    )
    free_dl_parser.add_argument("genre", choices=list(MIX_PREP_GENRES), help="Genre to focus on")
    free_dl_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full pipeline but skip Discord posts and history writes",
    )
    free_dl_parser.add_argument(
        "--bpm", metavar="MIN-MAX", default=None,
        help="Filter to a BPM range, e.g. 170-180. Half/double-time matches are "
             "included by default (e.g. 85 matches 170-180) — see --no-bpm-flex. "
             "Tracks with no known BPM are kept but demoted, never dropped.",
    )
    free_dl_parser.add_argument(
        "--key", default=None,
        help="Filter to a Camelot code (e.g. 8A) or musical key (e.g. Am, C major, "
             "F# minor). Keeps exact matches, adjacent wheel positions (±1), and the "
             "relative major/minor. Tracks with no known key are kept but demoted, "
             "never dropped.",
    )
    free_dl_parser.add_argument(
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
    replay_parser = subparsers.add_parser(
        "replay",
        help="Replay an archived week's fetch offline under current or overridden config",
    )
    replay_parser.add_argument(
        "--week", required=True, metavar="YYYY-Www",
        help="Archived ISO week to replay, e.g. 2026-W23",
    )
    replay_parser.add_argument(
        "--set", action="append", default=[], dest="overrides", metavar="path=value",
        help="Override a config value for this replay only, e.g. "
             "--set scoring.w_known_artist=2.0 (repeatable; never writes settings.yaml)",
    )
    subparsers.add_parser(
        "tune-report",
        help="Feedback-driven per-signal/source/genre positive-rate and lift report",
    )
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the web API (tunefinder-web backend) with uvicorn",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8420, help="Port (default 8420)")

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
    elif args.command == "free-downloads":
        cmd_free_downloads(args)
    elif args.command == "mark":
        cmd_mark(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "explain":
        cmd_explain(args)
    elif args.command == "backfill-labels":
        cmd_backfill_labels(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "tune-report":
        cmd_tune_report(args)
    elif args.command == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()
