"""
Explain command — trace a track through the weekly pipeline offline.

Reconstruction from current data/ state (source_items.json of the last fetch)
— not a replay of the posted report.

explain is weekly-only: mix-prep filtering differs and is out of scope.
"""
import copy
from datetime import datetime, timezone

from src.fetchers import load_source_items
from src.pipeline.dedup import (
    deduplicate_source_items,
    filter_history,
    filter_known,
    filter_release_date,
    items_to_candidates,
    make_dedup_key,
)
from src.pipeline.feedback import load_feedback
from src.pipeline.history import build_history_keys, load_history
from src.pipeline.labels import fresh_label_artist_data, load_label_affinity
from src.pipeline.pool import load_pool, pool_to_candidates
from src.pipeline.profile import load_artist_profiles, load_genre_affinity, load_known_tracks
from src.pipeline.ranker import (
    _assign_sections,
    _build_genre_set,
    _build_relevant_labels,
    _merge_label_knowledge,
    _score,
)


def _find_history_record(key: str, history):
    for r in sorted(history, key=lambda r: r.recommended_at, reverse=True):
        if make_dedup_key(r.artist, r.title) == key:
            return r
    return None


def explain_track(selector: str, settings) -> str:
    """Trace a track through the weekly pipeline. Returns deterministic multi-line text."""
    lines = [
        "Reconstruction from current data/ state (source_items.json of the last fetch) "
        "— not a replay of the posted report.",
        f"Selector: {selector!r}",
        "",
    ]

    # Parse selector
    if " - " not in selector:
        lines.append("ERROR: selector must be 'Artist - Title'")
        return "\n".join(lines)

    artist_part, title_part = selector.split(" - ", 1)
    target_key = make_dedup_key(artist_part, title_part)
    lines.append(f"Dedup key: {target_key!r}")
    lines.append("")

    # Load offline data
    source_items = load_source_items(settings.data_dir)
    known_keys = load_known_tracks(settings.data_dir)
    profiles = load_artist_profiles(settings.data_dir)
    genre_affinity = load_genre_affinity(settings.data_dir)
    history = load_history(settings.data_dir)
    history_keys = build_history_keys(history)
    pool_records = load_pool(settings.data_dir)
    feedback_entries = load_feedback(settings.data_dir)

    # --- Fetched ---
    lines.append("=== FETCHED ===")
    matching_raw = [i for i in source_items if make_dedup_key(i.artist, i.title) == target_key]
    if matching_raw:
        for item in matching_raw:
            tags = ", ".join(item.genre_tags) if item.genre_tags else "(none)"
            lines.append(
                f"  source={item.source!r} label={item.label!r} "
                f"release_date={item.release_date!r} genre_tags=[{tags}] "
                f"link={item.link!r}"
            )
    else:
        lines.append("  Not in the current week's fetch.")
    lines.append("")

    # --- Dedup ---
    lines.append("=== DEDUP ===")
    deduped = deduplicate_source_items(source_items)
    target_deduped = next((i for i in deduped if make_dedup_key(i.artist, i.title) == target_key), None)
    if target_deduped:
        sources = target_deduped.raw_metadata.get("seen_on_sources", [target_deduped.source])
        tags = ", ".join(target_deduped.genre_tags) if target_deduped.genre_tags else "(none)"
        lines.append(f"  Merged item: seen_on_sources={sources} genre_tags=[{tags}]")
    else:
        lines.append("  Not present after dedup (not in current fetch).")
    lines.append("")

    # Resolve target candidate: deduped item → pool item → unknown
    if target_deduped:
        target_candidate = items_to_candidates([target_deduped])[0]
    else:
        pool_match = next((r for r in pool_records if r.key == target_key), None)
        if pool_match:
            target_candidate = pool_to_candidates([pool_match])[0]
        else:
            target_candidate = None

    # --- Known-track filter ---
    lines.append("=== KNOWN-TRACK FILTER ===")
    if target_candidate:
        kept = filter_known([target_candidate], known_keys)
        if kept:
            lines.append("  PASS — not in known-track exclusion set.")
        else:
            lines.append("  FILTERED — track matches known-track exclusion set.")
    else:
        known_verdict = "FILTERED" if target_key in known_keys else "PASS (key check only — no candidate)"
        lines.append(f"  {known_verdict}")
    lines.append("")

    # --- History filter ---
    lines.append("=== HISTORY FILTER ===")
    if target_candidate:
        kept_hist = filter_history([target_candidate], history_keys)
        if kept_hist:
            lines.append("  PASS — not in recommendation history.")
        else:
            rec = _find_history_record(target_key, history)
            if rec:
                lines.append(
                    f"  FILTERED — previously recommended in {rec.report_id} ({rec.recommended_at[:10]})."
                )
            else:
                lines.append("  FILTERED — matches history key.")
    else:
        if target_key in history_keys:
            rec = _find_history_record(target_key, history)
            if rec:
                lines.append(
                    f"  FILTERED — previously recommended in {rec.report_id} ({rec.recommended_at[:10]})."
                )
            else:
                lines.append("  FILTERED — matches history key.")
        else:
            lines.append("  PASS (key check only — no candidate).")
    lines.append("")

    # --- Release window ---
    window_days = settings.pipeline_release_date_window_days
    lines.append("=== RELEASE WINDOW ===")
    if not window_days:
        lines.append("  Release date filter: off (window_days unset).")
    elif target_candidate:
        kept_rd = filter_release_date([target_candidate], window_days)
        if kept_rd:
            lines.append(
                f"  PASS — release_date={target_candidate.release_date!r} within {window_days}-day window."
            )
        else:
            lines.append(
                f"  FILTERED — release_date={target_candidate.release_date!r} outside {window_days}-day window."
            )
    else:
        lines.append("  No candidate — cannot evaluate.")
    lines.append("")

    # --- Score reconstruction — single pass ---
    lines.append("=== SCORING + SECTION RECONSTRUCTION ===")

    # Build context identical to cmd_run
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    all_candidates_fresh = items_to_candidates(deduped)
    label_seed = list(all_candidates_fresh)

    # Filter pipeline (mirroring cmd_run)
    scored_candidates = filter_known(all_candidates_fresh, known_keys)
    scored_candidates = filter_history(scored_candidates, history_keys)
    if window_days:
        scored_candidates = filter_release_date(scored_candidates, window_days)

    # Pool injection (same exclusion logic as cmd_run)
    fresh_keys_set = {c.key for c in scored_candidates}
    pool_injected = [
        c for c in pool_to_candidates([r for r in pool_records if r.key not in fresh_keys_set])
        if c.key not in known_keys and c.key not in history_keys
    ]
    all_scored = scored_candidates + pool_injected

    aliases = settings.artist_aliases()
    relevant_labels, label_artist_counts, label_artist_names = _build_relevant_labels(
        label_seed if label_seed else all_scored, profiles_lower, aliases
    )
    weights = settings.scoring_weights()

    # Label affinity memory (issue #5) — mirror cmd_run so explain matches run
    # behaviour. Read-only reconstruction: explain never writes the store.
    label_store = load_label_affinity(settings.data_dir)
    label_memory = fresh_label_artist_data(label_store, weights.label_memory_max_age_weeks)
    relevant_labels, label_artist_counts, label_artist_names = _merge_label_knowledge(
        relevant_labels, label_artist_counts, label_artist_names, label_memory
    )

    from src.pipeline.history import recent_recommended_artists
    recent_artists = recent_recommended_artists(settings.data_dir, weeks=weights.recency_weeks)

    # Single scoring pass
    for c in all_scored:
        _score(c, profiles_lower, relevant_labels, label_artist_counts, genres_set, recent_artists, weights, genre_affinity, aliases)

    ranked = sorted(all_scored, key=lambda c: c.score, reverse=True)

    # Find target in the scored set
    target_scored = next((c for c in all_scored if c.key == target_key), None)

    hypothetical = False
    if target_scored is None:
        # Track was excluded before scoring — score a fresh copy hypothetically
        if target_candidate:
            hyp = copy.copy(target_candidate)
            hyp.signals = []
            hyp.score = 0.0
            _score(hyp, profiles_lower, relevant_labels, label_artist_counts, genres_set, recent_artists, weights, genre_affinity, aliases)
            target_scored = hyp
            hypothetical = True
        else:
            lines.append("  Selector unknown — cannot score (no candidate found in fetch or pool).")
            lines.append("")
    else:
        rank_pos = next((i + 1 for i, c in enumerate(ranked) if c is target_scored), None)
        lines.append(f"  Rank: #{rank_pos} of {len(ranked)} scored candidates (score={target_scored.score})")

    if target_scored:
        if hypothetical:
            lines.append(f"  (hypothetical — track was excluded before scoring)")
            lines.append(f"  Score: {target_scored.score}")
        signal_lines = [f"    [{s.code}] {s.explanation}" for s in target_scored.signals]
        if signal_lines:
            lines.append("  Signals:")
            lines.extend(signal_lines)
        else:
            lines.append("  Signals: none")

    lines.append("")

    # Section reconstruction
    lines.append("=== SECTION ===")
    if not hypothetical and target_scored is not None:
        trace: dict = {}
        sections = _assign_sections(ranked, settings, genres_set, trace=trace)

        landed = None
        for section_name, members in sections.items():
            if any(c is target_scored for c in members):
                landed = section_name
                break

        if landed:
            pos_in_section = next(
                (i + 1 for i, c in enumerate(sections[landed]) if c is target_scored), None
            )
            lines.append(f"  Landed in: {landed} (position #{pos_in_section})")
        else:
            skip_reasons = trace.get(id(target_scored), [])
            if skip_reasons:
                for sname, reason in skip_reasons:
                    lines.append(f"  Skipped from {sname}: {reason}")
            else:
                # Never examined → outscored
                rank_pos = next((i + 1 for i, c in enumerate(ranked) if c is target_scored), None)
                lines.append(
                    f"  outscored — sections filled before its rank "
                    f"(position #{rank_pos} of {len(ranked)})"
                )
    else:
        lines.append("  Section reconstruction skipped (track excluded before scoring).")
    lines.append("")

    # --- Pool ---
    lines.append("=== POOL ===")
    pool_rec = next((r for r in pool_records if r.key == target_key), None)
    if pool_rec:
        lines.append(f"  In pool — added_at={pool_rec.added_at!r} last_score={pool_rec.last_score}")
    else:
        lines.append("  Not in pool.")
    lines.append("")

    # --- Feedback ---
    lines.append("=== FEEDBACK ===")
    feedback_matches = [e for e in feedback_entries if e.key == target_key]
    if feedback_matches:
        for e in sorted(feedback_matches, key=lambda e: e.marked_at):
            lines.append(
                f"  [{e.history}] {e.outcome!r} marked {e.marked_at[:10]} "
                f"(report {e.report_id})"
            )
    else:
        lines.append("  No feedback recorded.")
    lines.append("")

    return "\n".join(lines)
