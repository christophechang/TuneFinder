"""
Candidate ranker.

Scores each Candidate against the artist profile data, derives label relevance
from the candidate set itself, and assigns RecommendationSignal objects that
explain each scoring decision.

Candidates are then split into report sections:
  top_picks    — highest overall score, any signal type
  label_watch  — label_match signal, not already in top_picks
  artist_watch — known_artist signal, not already in top_picks/label_watch
  wildcards    — highest scoring remainder
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from src.logger import get_logger
from src.models import ArtistProfile, Candidate, RecommendationSignal
from src.pipeline.dedup import normalise_artist
from src.pipeline.profile import _split_artists, resolve_profile

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScoringWeights:
    """Configuration-driven scoring weights — tunable via config/settings.yaml."""
    w_known_artist: float = 3.0      # multiplied by play_count, per matched artist in track
    w_recurring: float = 2.0         # extra if any matched artist has play_count >= threshold
    w_label_base: float = 1.5        # base bonus for any label match
    w_label_per_artist: float = 0.5  # per additional known artist on the label, up to cap
    label_artist_cap: int = 3        # max known artists on a label that contribute to the bonus
    w_cross_source_per: float = 0.5  # per source seen on, up to cap (only credited when len >= 2)
    cross_source_cap: int = 4        # max source count that contributes to the bonus
    w_recency_penalty: float = 0.75  # subtract once if any matched artist was recommended in window
    recency_weeks: int = 4
    w_pool_age_per_week: float = 0.25  # subtracted per week since pool entry was added
    pool_age_penalty_max: float = 1.5  # cap total pool-age penalty
    w_genre: float = 0.5             # per matching genre tag
    genre_match_cap: int = 2         # cross-source dedup unions tags; cap prevents same popularity fact paid twice
    genre_affinity_min: float = 0.5  # multiplier floor for genres you rarely play (or aren't in the affinity map)
    genre_affinity_max: float = 2.0  # multiplier ceiling for your most-played genre(s)
    w_fresh: float = 0.5             # released within fresh_days
    fresh_days: int = 7              # genuinely just-out; inside a 28-day-filtered corpus, 30 was a constant
    w_chart_top: float = 1.5         # max bonus for chart_position == 1; decays linearly to 0 at position 100
    w_bandcamp: float = 1.0          # discovery bonus for Bandcamp (no chart data available)
    max_artist_score: float = 10.0   # cap so one mega-artist doesn't dominate
    recurring_threshold: int = 3     # play_count needed to earn the recurring bonus
    min_artist_match_len: int = 4    # matched artist-name parts shorter than this need independent corroboration (label or genre) to count — guards against short-name string collisions
    # --- Two-axis scoring (Wildcards selection) ---
    wildcards_axis: str = "discovery"        # "discovery" (rank by discovery_score only) | "combined" (pre-P2 behaviour: rank by total score)
    wildcards_max_familiarity: float = 0.0   # in "discovery" mode, wildcards require familiarity_score <= this


_GENRE_AUGMENT_MIN_ARTISTS = 3

# Baseline genres — soft match only, not a hard filter. Augmented at runtime
# by `_build_genre_set` with any catalog genre crossing the augment threshold.
_BASELINE_GENRES = {"dnb", "breaks", "uk-bass", "ukg", "house", "techno", "electronica", "electronic"}

# Too broad to be evidence of taste fit — nearly every track carries it.
# Stays in _BASELINE_GENRES (genre-set augmentation + cap exemption); excluded from scoring only.
_SCORING_EXEMPT_GENRES = {"electronic"}


def _build_genre_set(profiles_lower: dict[str, ArtistProfile]) -> set[str]:
    """Return the curated baseline genres unioned with any catalog genre that
    appears across `_GENRE_AUGMENT_MIN_ARTISTS` or more distinct profiles.
    """
    counts: dict[str, int] = {}
    for profile in profiles_lower.values():
        for g in profile.genres_seen:
            counts[g] = counts.get(g, 0) + 1
    augmented = {g for g, n in counts.items() if n >= _GENRE_AUGMENT_MIN_ARTISTS}
    result = _BASELINE_GENRES | augmented
    logger.info(f"[ranker] Genre set: {len(_BASELINE_GENRES)} baseline + {len(augmented - _BASELINE_GENRES)} catalog-augmented")
    return result

# Chart scale — not a weight, used in scoring calculation
_CHART_SCALE = 100         # chart positions are 1–100


def _build_relevant_labels(
    candidates: list[Candidate],
    profiles_lower: dict[str, ArtistProfile],
    aliases: dict[str, str] | None = None,
) -> tuple[set[str], dict[str, int], dict[str, list[str]]]:
    """Return (relevant_labels, label_known_artist_counts, label_artist_names).

    A label is relevant if any release on it has a known artist (matched in
    profiles_lower, or via aliases — see src/pipeline/profile.resolve_profile).
    The counts dict tracks how many DISTINCT known artists in the candidate set
    are on each label — used by the label scoring formula. label_artist_names
    holds all display names (sorted) per label key; truncation to 3 happens at
    render time.
    """
    relevant: set[str] = set()
    counts: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    for c in candidates:
        if not c.label:
            continue
        label_key = c.label.lower().strip()
        for part in _split_artists(c.artist):
            profile = resolve_profile(part, profiles_lower, aliases)
            if profile:
                relevant.add(label_key)
                counts.setdefault(label_key, set()).add(profile.name.lower())
                names.setdefault(label_key, set()).add(profile.name)
    counts_int = {k: len(v) for k, v in counts.items()}
    label_artist_names = {k: sorted(v) for k, v in names.items()}
    logger.info(f"[ranker] {len(relevant)} relevant labels derived from candidate set")
    return relevant, counts_int, label_artist_names


def _genre_affinity_multiplier(
    tag: str,
    genre_affinity: dict[str, float] | None,
    weights: "ScoringWeights",
) -> float:
    """Scale genre_match per tag by corpus-level genre affinity (P3).

    None/empty affinity → 1.0 (today's flat behaviour, no data to scale by).
    Otherwise a tag's share of the affinity map is normalised against the
    dominant genre's share and mapped into [genre_affinity_min,
    genre_affinity_max]; a tag absent from the map (a genre you don't play)
    gets the floor multiplier.
    """
    if not genre_affinity:
        return 1.0
    share = genre_affinity.get(tag)
    if share is None:
        return weights.genre_affinity_min
    max_share = max(genre_affinity.values())
    raw = 0.5 + 1.5 * (share / max_share)
    return min(max(raw, weights.genre_affinity_min), weights.genre_affinity_max)


def _score(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    relevant_labels: set[str],
    label_artist_counts: dict[str, int],
    genres_set: set[str],
    recent_artists: frozenset[str] | set[str] = frozenset(),
    weights: ScoringWeights | None = None,
    genre_affinity: dict[str, float] | None = None,
    aliases: dict[str, str] | None = None,
) -> None:
    """Mutate candidate in place: assign signals and total score."""
    if weights is None:
        weights = ScoringWeights()

    score = 0.0
    # Two-axis scoring (P2): the same signals feed a familiarity sub-total and a
    # discovery sub-total, in addition to the unchanged combined `score`. Used to
    # pick Wildcards by discovery merit alone instead of leftover total score.
    familiarity = 0.0
    discovery = 0.0

    # --- Artist signals (familiarity axis) ---
    artist_score = 0.0
    best_play_count = 0
    matched: list[str] = []

    # Short-name match guard (issue #4): a matched part shorter than
    # min_artist_match_len can string-collide with an unrelated release, so it
    # only counts when the candidate carries independent corroboration —
    # otherwise it's a false "You play X" claim. Computed once per candidate
    # since it doesn't depend on which part matched.
    has_label_corrob = bool(c.label) and c.label.lower().strip() in relevant_labels
    has_genre_corrob = any(
        g in genres_set and g not in _SCORING_EXEMPT_GENRES for g in c.genre_tags
    )
    corroborated = has_label_corrob or has_genre_corrob

    for part in _split_artists(c.artist):
        profile = resolve_profile(part, profiles_lower, aliases)
        if not profile:
            continue
        written_name = part.strip()
        if len(written_name) < weights.min_artist_match_len and not corroborated:
            logger.info(f"[ranker] short-name match '{written_name}' skipped (uncorroborated)")
            continue
        artist_score += profile.play_count * weights.w_known_artist
        best_play_count = max(best_play_count, profile.play_count)
        matched.append(profile.name)

    if matched:
        artist_score = min(artist_score, weights.max_artist_score)
        score += artist_score
        familiarity += artist_score
        names = ", ".join(matched[:2])
        c.signals.append(RecommendationSignal(
            code="known_artist",
            explanation=f"You play {names} — this is new material from them.",
        ))

        if best_play_count >= weights.recurring_threshold:
            score += weights.w_recurring
            familiarity += weights.w_recurring
            c.signals.append(RecommendationSignal(
                code="recurring_artist",
                explanation=f"{matched[0]} appears in {best_play_count} of your mixes.",
            ))

        # --- Artist-recency penalty ---
        if any(normalise_artist(name) in recent_artists for name in matched):
            score -= weights.w_recency_penalty
            familiarity -= weights.w_recency_penalty
            c.signals.append(RecommendationSignal(
                code="recent_recommendation",
                explanation=f"{matched[0]} appeared in a recent report — soft down-weight.",
            ))

    # --- Label signal (discovery axis) ---
    if c.label and c.label.lower().strip() in relevant_labels:
        label_key = c.label.lower().strip()
        known_on_label = min(label_artist_counts.get(label_key, 1), weights.label_artist_cap)
        label_bonus = weights.w_label_base + weights.w_label_per_artist * known_on_label
        score += label_bonus
        discovery += label_bonus
        c.signals.append(RecommendationSignal(
            code="label_match",
            explanation=f"{c.label} — a label you've played artists from.",
        ))

    # --- Cross-source credibility (discovery axis) ---
    seen_on = c.raw_metadata.get("seen_on_sources", [c.source])
    if len(seen_on) >= 2:
        capped = min(len(seen_on), weights.cross_source_cap)
        cross_source_bonus = weights.w_cross_source_per * capped
        score += cross_source_bonus
        discovery += cross_source_bonus
        c.signals.append(RecommendationSignal(
            code="cross_source",
            explanation=f"Flagged by {len(seen_on)} sources: {', '.join(seen_on)}.",
        ))

    # --- Genre match, soft, scaled by genre affinity (discovery axis) ---
    matching = [g for g in c.genre_tags if g in genres_set and g not in _SCORING_EXEMPT_GENRES]
    if matching:
        # When more tags match than genre_match_cap allows, count the
        # highest-affinity tags first — a dominant-genre tag shouldn't lose
        # its slot to a fringe one just because of tag order.
        by_multiplier = sorted(
            matching,
            key=lambda g: -_genre_affinity_multiplier(g, genre_affinity, weights),
        )
        counted = by_multiplier[:weights.genre_match_cap]
        genre_bonus = sum(
            weights.w_genre * _genre_affinity_multiplier(g, genre_affinity, weights)
            for g in counted
        )
        score += genre_bonus
        discovery += genre_bonus
        c.signals.append(RecommendationSignal(
            code="genre_match",
            explanation=f"Tagged: {', '.join(matching[:3])}.",
        ))

    # --- Freshness (discovery axis) ---
    if c.release_date:
        try:
            rel = datetime.strptime(c.release_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - rel).days
            if 0 <= days_old <= weights.fresh_days:
                score += weights.w_fresh
                discovery += weights.w_fresh
                c.signals.append(RecommendationSignal(
                    code="fresh_release",
                    explanation=f"Released {days_old} day{'s' if days_old != 1 else ''} ago.",
                ))
        except ValueError:
            pass

    # --- Chart position, discovery axis (any source that sets chart_position, e.g. Beatport) ---
    chart_pos = c.raw_metadata.get("chart_position")
    if chart_pos and isinstance(chart_pos, int) and 1 <= chart_pos <= _CHART_SCALE:
        chart_bonus = weights.w_chart_top * (1 - (chart_pos - 1) / _CHART_SCALE)
        score += chart_bonus
        discovery += chart_bonus
        chart_period = c.raw_metadata.get("chart_period", "weekly")
        c.signals.append(RecommendationSignal(
            code="chart_position",
            explanation=f"#{chart_pos} on the {c.source.title()} {chart_period} chart.",
        ))

    # --- Bandcamp discovery bonus (discovery axis; compensates for no chart_position signal) ---
    if c.source == "bandcamp":
        score += weights.w_bandcamp
        discovery += weights.w_bandcamp
        c.signals.append(RecommendationSignal(
            code="bandcamp_discovery",
            explanation="Bandcamp discovery — independent release outside chart sources.",
        ))

    # --- Pool age penalty ---
    # Applied to the TOTAL score only, not either axis. Decision: pool age is an
    # artefact of the queue (how long a candidate sat unrecommended), not a fact
    # about its familiarity or discovery merit — deducting it from an axis would
    # understate a stale-but-genuinely-discovery-worthy pool candidate exactly
    # when Wildcards selection (discovery axis) needs to see it clearly. Both
    # axes therefore stay gross; only the combined ranking absorbs the penalty.
    if c.pool_added_at:
        try:
            added = datetime.fromisoformat(c.pool_added_at)
            if added.tzinfo is None:
                added = added.replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - added).days
            weeks_old = max(0, days_old // 7)
            penalty = min(weights.w_pool_age_per_week * weeks_old, weights.pool_age_penalty_max)
            if penalty > 0:
                score -= penalty
                c.signals.append(RecommendationSignal(
                    code="pool_age",
                    explanation=f"Carried over from pool for {weeks_old} week{'s' if weeks_old != 1 else ''}.",
                ))
        except ValueError:
            pass

    c.score = round(score, 2)
    c.familiarity_score = round(familiarity, 2)
    c.discovery_score = round(discovery, 2)


def _assign_sections(
    ranked: list[Candidate],
    settings,
    genres_set: set[str],
    trace: dict | None = None,
) -> dict[str, list[Candidate]]:
    top_n = settings.pipeline_top_picks_count
    label_n = settings.pipeline_label_watch_count
    artist_n = settings.pipeline_artist_watch_count
    wildcard_n = settings.pipeline_wildcard_count
    min_score = settings.pipeline_section_min_score
    weights = settings.scoring_weights()

    used: set[int] = set()
    MAX_PER_ARTIST = 2
    MAX_PER_RELEASE = 2
    MAX_PER_GENRE = 3
    # "electronic" is too broad to cap — nearly every track carries it
    _UNCAPPED_GENRES = {"electronic"}

    # Genre cap is global across all sections so one genre can't flood the full report
    genre_counts: dict[str, int] = {}

    def pick(
        n: int,
        require_signal: str = None,
        section_name: str = "",
        order: list[Candidate] | None = None,
        floor_field: str = "score",
        extra_skip=None,
    ) -> list[Candidate]:
        # Artist/release caps reset per section so an artist can appear in different
        # sections (e.g. top_picks and label_watch serve distinct curatorial purposes)
        artist_counts: dict[str, int] = {}
        release_counts: dict[str, int] = {}
        result = []
        candidates_iter = order if order is not None else ranked
        floor_suffix = " (discovery axis)" if floor_field == "discovery_score" else ""
        for c in candidates_iter:
            if id(c) in used:
                continue
            if getattr(c, floor_field) < min_score:
                if trace is not None and section_name:
                    trace.setdefault(id(c), []).append((section_name, f"below floor {min_score}{floor_suffix}"))
                continue
            if extra_skip is not None:
                skip_reason = extra_skip(c)
                if skip_reason:
                    if trace is not None and section_name:
                        trace.setdefault(id(c), []).append((section_name, skip_reason))
                    continue
            if require_signal and not any(s.code == require_signal for s in c.signals):
                if trace is not None and section_name:
                    trace.setdefault(id(c), []).append((section_name, f"lacks {require_signal} signal"))
                continue
            artist_key = normalise_artist(c.artist)
            release_key = (c.release_name or "").strip().lower()
            if artist_counts.get(artist_key, 0) >= MAX_PER_ARTIST:
                if trace is not None and section_name:
                    trace.setdefault(id(c), []).append((section_name, "artist cap"))
                continue
            if release_key and release_counts.get(release_key, 0) >= MAX_PER_RELEASE:
                if trace is not None and section_name:
                    trace.setdefault(id(c), []).append((section_name, "release cap"))
                continue
            # Genre cap: use first specific (non-broad) genre tag; exempt if none found
            cap_genre = next(
                (g for g in c.genre_tags if g in genres_set and g not in _UNCAPPED_GENRES),
                None,
            )
            if cap_genre and genre_counts.get(cap_genre, 0) >= MAX_PER_GENRE:
                if trace is not None and section_name:
                    trace.setdefault(id(c), []).append((section_name, f"genre cap ({cap_genre})"))
                continue
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            if release_key:
                release_counts[release_key] = release_counts.get(release_key, 0) + 1
            if cap_genre:
                genre_counts[cap_genre] = genre_counts.get(cap_genre, 0) + 1
            result.append(c)
            used.add(id(c))
            if len(result) >= n:
                break
        return result

    top_picks = pick(top_n, section_name="top_picks")
    label_watch = pick(label_n, require_signal="label_match", section_name="label_watch")
    artist_watch = pick(artist_n, require_signal="known_artist", section_name="artist_watch")

    if weights.wildcards_axis == "discovery":
        # Rank remaining candidates by discovery_score (stable tie-break: total
        # score, then original — already score-sorted — order) so Wildcards
        # becomes a genuine discovery channel instead of known-artist overflow.
        discovery_order = [
            c for _, c in sorted(
                enumerate(ranked),
                key=lambda ic: (-ic[1].discovery_score, -ic[1].score, ic[0]),
            )
        ]

        def _familiarity_skip(c: Candidate):
            if c.familiarity_score > weights.wildcards_max_familiarity:
                return (
                    f"familiarity too high for wildcards "
                    f"({c.familiarity_score} > {weights.wildcards_max_familiarity})"
                )
            return None

        # Floor applies to discovery_score in discovery mode — a track with a
        # high total score but low discovery merit (e.g. an already-known
        # artist carried mostly by familiarity) must not slip into Wildcards
        # on its total; it has to clear the floor on discovery grounds alone.
        wildcards = pick(
            wildcard_n,
            section_name="wildcards",
            order=discovery_order,
            floor_field="discovery_score",
            extra_skip=_familiarity_skip,
        )
    else:
        wildcards = pick(wildcard_n, section_name="wildcards")

    genre_summary = ", ".join(f"{g}: {n}" for g, n in sorted(genre_counts.items()))
    logger.info(
        f"[ranker] Sections — top_picks: {len(top_picks)}, "
        f"label_watch: {len(label_watch)}, artist_watch: {len(artist_watch)}, "
        f"wildcards: {len(wildcards)} (floor={min_score}) | genres: {genre_summary or 'none tagged'}"
    )
    return {
        "top_picks": top_picks,
        "label_watch": label_watch,
        "artist_watch": artist_watch,
        "wildcards": wildcards,
    }


def rank_candidates(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
    genre_affinity: dict[str, float] | None = None,
) -> tuple[dict[str, list[Candidate]], dict[str, list[str]]]:
    """
    Score all candidates, assign signals, sort, and split into report sections.
    Returns (sections, label_artist_names).
    sections keys: top_picks, label_watch, artist_watch, wildcards.
    label_artist_names: {label_key: sorted list of display names}.

    label_seed: pre-filter candidates used for label relevance derivation.
    Pass the full source candidate list before filter_known/filter_history so
    that known artists (who are filtered out of candidates) still contribute
    their labels. Defaults to candidates if not provided.

    genre_affinity: corpus-level genre share map (src/pipeline/profile.py
    build_genre_affinity). None/empty → flat 1.0 multiplier, today's behaviour.
    """
    from src.pipeline.history import recent_recommended_artists

    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    aliases = settings.artist_aliases()
    relevant_labels, label_artist_counts, label_artist_names = _build_relevant_labels(
        label_seed if label_seed is not None else candidates, profiles_lower, aliases
    )
    weights = settings.scoring_weights()
    recent_artists = recent_recommended_artists(settings.data_dir, weeks=weights.recency_weeks)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, label_artist_counts, genres_set, recent_artists, weights, genre_affinity, aliases)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections(ranked, settings, genres_set), label_artist_names


def _assign_sections_mix_prep(
    ranked: list[Candidate],
    settings,
) -> dict[str, list[Candidate]]:
    top_n = settings.pipeline_mix_prep_top_picks_count
    deep_n = settings.pipeline_mix_prep_deep_cuts_count
    min_score = settings.pipeline_section_min_score

    used: set[int] = set()
    MAX_PER_ARTIST = 2
    MAX_PER_RELEASE = 2

    def pick(n: int) -> list[Candidate]:
        artist_counts: dict[str, int] = {}
        release_counts: dict[str, int] = {}
        result = []
        for c in ranked:
            if id(c) in used:
                continue
            if c.score < min_score:
                continue
            artist_key = normalise_artist(c.artist)
            release_key = (c.release_name or "").strip().lower()
            if artist_counts.get(artist_key, 0) >= MAX_PER_ARTIST:
                continue
            if release_key and release_counts.get(release_key, 0) >= MAX_PER_RELEASE:
                continue
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            if release_key:
                release_counts[release_key] = release_counts.get(release_key, 0) + 1
            result.append(c)
            used.add(id(c))
            if len(result) >= n:
                break
        return result

    top_picks = pick(top_n)
    deep_cuts = pick(deep_n)
    logger.info(f"[ranker] Mix-prep sections — top_picks: {len(top_picks)}, deep_cuts: {len(deep_cuts)} (floor={min_score})")
    return {"top_picks": top_picks, "deep_cuts": deep_cuts}


def rank_candidates_mix_prep(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
    genre_affinity: dict[str, float] | None = None,
) -> tuple[dict[str, list[Candidate]], dict[str, list[str]]]:
    """
    Score and section candidates for a mix-prep run.
    Returns (sections, label_artist_names).
    Same scoring as rank_candidates but uses two sections (top_picks, deep_cuts)
    with no per-genre cap — the genre has already been filtered upstream.

    genre_affinity: see rank_candidates. None/empty → flat 1.0 multiplier.
    """
    from src.pipeline.history import recent_recommended_artists

    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    aliases = settings.artist_aliases()
    relevant_labels, label_artist_counts, label_artist_names = _build_relevant_labels(
        label_seed if label_seed is not None else candidates, profiles_lower, aliases
    )
    weights = settings.scoring_weights()
    recent_artists = recent_recommended_artists(settings.data_dir, weeks=weights.recency_weeks)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, label_artist_counts, genres_set, recent_artists, weights, genre_affinity, aliases)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Mix-prep scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections_mix_prep(ranked, settings), label_artist_names


