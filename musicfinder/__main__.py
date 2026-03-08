import argparse
import os
import sys

# Add project root to sys.path so `from src.xxx` works everywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import setup_logging, get_logger
from src.config import load_settings


def cmd_check_config(args):
    import os
    from src.config import PROVIDER_ENV_VAR

    settings = load_settings()
    settings.validate()

    stage1_cfg = settings.llm_stage1
    fallback_chain = settings.llm_fallback_chain

    cascade = [
        {"provider": stage1_cfg.get("provider", "mistral"), "model": stage1_cfg.get("model", ""), "position": "primary"}
    ] + [
        {"provider": e.get("provider", ""), "model": e.get("model", ""), "position": f"fallback {i + 1}"}
        for i, e in enumerate(fallback_chain)
    ]

    print("\nStage 1 LLM cascade:")
    print(f"  {'Position':<12}  {'Provider':<12}  {'Model':<30}  {'Env Var':<22}  Status")
    print(f"  {'-'*12}  {'-'*12}  {'-'*30}  {'-'*22}  ------")
    for entry in cascade:
        provider = entry["provider"]
        model = entry["model"]
        position = entry["position"]
        env_var = PROVIDER_ENV_VAR.get(provider)
        if env_var is None:
            status = "no key needed"
            env_var_display = "(none)"
        elif os.getenv(env_var):
            status = "SET"
            env_var_display = env_var
        else:
            status = "MISSING"
            env_var_display = env_var
        print(f"  {position:<12}  {provider:<12}  {model:<30}  {env_var_display:<22}  {status}")

    print("\nStage 2: anthropic / claude-sonnet-4-6 (direct, no fallback)")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    print(f"  ANTHROPIC_API_KEY: {'SET' if anthropic_key else 'MISSING'}\n")


def cmd_save_fixtures(args):
    from src.fetchers.catalog import save_fixtures
    settings = load_settings()
    save_fixtures(settings)
    print(f"Fixtures saved to {settings.testing_fixtures_dir}/")


def cmd_build_profile(args):
    from src.fetchers.catalog import fetch_all_tracks
    from src.pipeline.profile import (
        build_artist_profiles,
        save_known_tracks,
        save_artist_profiles,
    )
    settings = load_settings()
    logger = get_logger(__name__)

    logger.info("[build-profile] Fetching tracks...")
    tracks = fetch_all_tracks(settings)

    logger.info("[build-profile] Building artist profiles...")
    profiles = build_artist_profiles(tracks)

    save_known_tracks(tracks, settings.data_dir)
    save_artist_profiles(profiles, settings.data_dir)

    print(f"Profile built — {len(tracks)} known tracks, {len(profiles)} artists → {settings.data_dir}/")


def cmd_fetch_sources(args):
    from src.fetchers import fetch_all_sources, save_source_items
    settings = load_settings()
    logger = get_logger(__name__)

    logger.info("[fetch-sources] Starting source fetch...")
    items = fetch_all_sources(settings)
    save_source_items(items, settings.data_dir)

    by_source: dict[str, int] = {}
    for item in items:
        by_source[item.source] = by_source.get(item.source, 0) + 1

    print(f"Fetched {len(items)} items → {settings.data_dir}/source_items.json")
    for source, count in sorted(by_source.items()):
        print(f"  {source}: {count}")


def cmd_run(args):
    import time
    from src.fetchers.catalog import fetch_all_tracks
    from src.fetchers import fetch_all_sources, save_source_items
    from src.pipeline.profile import (
        build_artist_profiles, build_known_track_keys,
        save_known_tracks, save_artist_profiles,
    )
    from src.pipeline.history import (
        load_history, build_history_keys, append_records, make_report_id,
    )
    from src.pipeline.dedup import (
        deduplicate_source_items, items_to_candidates,
        filter_known, filter_history,
    )
    from src.pipeline.ranker import rank_candidates, all_section_candidates
    from src.pipeline.report import generate_report
    from src.output.discord import make_discord_client
    from src.models import RecommendationRecord
    from datetime import datetime, timezone

    settings = load_settings()
    settings.validate()
    logger = get_logger(__name__)
    start = time.time()
    report_id = make_report_id()
    logger.info(f"[run] Starting report run — {report_id}")

    # 1. Refresh profile and known-track set
    tracks = fetch_all_tracks(settings)
    profiles = build_artist_profiles(tracks)
    save_known_tracks(tracks, settings.data_dir)
    save_artist_profiles(profiles, settings.data_dir)
    known_keys = build_known_track_keys(tracks)

    # 2. Load recommendation history
    history = load_history(settings.data_dir)
    history_keys = build_history_keys(history)

    # 3. Fetch external sources
    source_items = fetch_all_sources(settings)
    save_source_items(source_items, settings.data_dir)
    sources_fetched = len(source_items)

    # 4. Dedup + filter
    source_items = deduplicate_source_items(source_items)
    after_dedup = len(source_items)
    candidates = items_to_candidates(source_items)
    candidates = filter_known(candidates, known_keys)
    after_known = len(candidates)
    candidates = filter_history(candidates, history_keys)
    after_history = len(candidates)

    stats = {
        "sources_fetched": sources_fetched,
        "raw_count": sources_fetched,
        "after_dedup": after_dedup,
        "after_known": after_known,
        "after_history": after_history,
    }

    if not candidates:
        logger.warning("[run] No candidates remaining after filtering — nothing to report")
        discord = make_discord_client(settings)
        discord.post_alert(f"Run {report_id}: no candidates after filtering. Check sources.")
        return

    # 5. Rank and split into sections
    sections = rank_candidates(candidates, profiles, settings)

    # 6. Generate report
    report_text = generate_report(sections, report_id, stats, settings)

    # 7. Post to Discord
    discord = make_discord_client(settings)
    discord.post_report(report_text)

    # 8. Update recommendation history
    now_iso = datetime.now(timezone.utc).isoformat()
    new_records = [
        RecommendationRecord(
            artist=c.artist,
            title=c.title,
            link=c.link,
            source=c.source,
            recommended_at=now_iso,
            report_id=report_id,
        )
        for c in all_section_candidates(sections)
    ]
    append_records(new_records, settings.data_dir)

    # 9. Post run summary to log channel
    duration = int(time.time() - start)
    by_source: dict[str, int] = {}
    for item in source_items:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    source_summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))
    log_msg = (
        f"**Run complete** — {report_id} | {duration}s\n"
        f"Sources: {source_summary}\n"
        f"Candidates: {sources_fetched} → {after_dedup} deduped → "
        f"{after_known} after known filter → {after_history} after history\n"
        f"Recommended: {len(new_records)} tracks"
    )
    discord.post_log(log_msg)
    print(f"Run complete — {report_id} — {len(new_records)} tracks recommended in {duration}s")


def main():
    parser = argparse.ArgumentParser(
        prog="musicfinder",
        description="Music Finder Report — weekly music discovery automation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-config", help="Validate all required env vars and config")
    subparsers.add_parser("save-fixtures", help="Fetch live API data and save to fixtures/ for offline testing")
    subparsers.add_parser("build-profile", help="Build artist profiles and known-track exclusion set from mix history")
    subparsers.add_parser("fetch-sources", help="Fetch candidate music from all enabled external sources")
    subparsers.add_parser("run", help="Run the full pipeline and post the weekly report to Discord")

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


if __name__ == "__main__":
    main()
