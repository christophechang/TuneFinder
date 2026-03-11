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
from datetime import datetime, timezone

from src.logger import get_logger
from src.models import ArtistProfile, Candidate, RecommendationSignal
from src.pipeline.dedup import normalise_artist
from src.pipeline.profile import _split_artists

logger = get_logger(__name__)

# Internal genre set — soft match only, not a hard filter
_OUR_GENRES = {"dnb", "breaks", "uk-bass", "ukg", "house", "techno", "electronica", "electronic"}

# Scoring weights
_W_KNOWN_ARTIST = 3.0      # multiplied by play_count, per matched artist in track
_W_RECURRING = 2.0         # extra if any matched artist has play_count >= threshold
_W_LABEL_MATCH = 2.5       # label connected to a known artist via this candidate set
_W_CROSS_SOURCE = 1.0      # seen on 2+ sources (more credibility)
_W_GENRE = 0.5             # per matching genre tag
_W_FRESH = 0.5             # released within 30 days
_W_CHART_TOP = 1.5         # max bonus for chart_position == 1; decays linearly to 0 at position 100
_W_BANDCAMP = 1.0          # discovery bonus for Bandcamp (no chart data available)
_W_HUMAN_CURATED = 1.5     # bonus for human-curated sources (e.g. Subsurface Selections)

_MAX_ARTIST_SCORE = 10.0   # cap so one mega-artist doesn't dominate
_RECURRING_THRESHOLD = 3   # play_count needed to earn the recurring bonus
_FRESH_DAYS = 30
_CHART_SCALE = 100         # chart positions are 1–100


def _build_relevant_labels(
    candidates: list[Candidate],
    profiles_lower: dict[str, ArtistProfile],
) -> set[str]:
    """
    Derive label relevance from the candidate set: a label is relevant if
    any release in the candidate set has a known artist on that label.
    """
    relevant: set[str] = set()
    for c in candidates:
        if not c.label:
            continue
        for part in _split_artists(c.artist):
            if part.lower().strip() in profiles_lower:
                relevant.add(c.label.lower().strip())
                break
    logger.info(f"[ranker] {len(relevant)} relevant labels derived from candidate set")
    return relevant


def _score(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    relevant_labels: set[str],
) -> None:
    """Mutate candidate in place: assign signals and total score."""
    score = 0.0

    # --- Artist signals ---
    artist_score = 0.0
    best_play_count = 0
    matched: list[str] = []

    for part in _split_artists(c.artist):
        profile = profiles_lower.get(part.lower().strip())
        if profile:
            artist_score += profile.play_count * _W_KNOWN_ARTIST
            best_play_count = max(best_play_count, profile.play_count)
            matched.append(profile.name)

    if matched:
        artist_score = min(artist_score, _MAX_ARTIST_SCORE)
        score += artist_score
        names = ", ".join(matched[:2])
        c.signals.append(RecommendationSignal(
            code="known_artist",
            explanation=f"You play {names} — this is new material from them.",
        ))

        if best_play_count >= _RECURRING_THRESHOLD:
            score += _W_RECURRING
            c.signals.append(RecommendationSignal(
                code="recurring_artist",
                explanation=f"{matched[0]} appears in {best_play_count} of your mixes.",
            ))

    # --- Label signal ---
    if c.label and c.label.lower().strip() in relevant_labels:
        score += _W_LABEL_MATCH
        c.signals.append(RecommendationSignal(
            code="label_match",
            explanation=f"{c.label} — a label you've played artists from.",
        ))

    # --- Cross-source credibility ---
    seen_on = c.raw_metadata.get("seen_on_sources", [c.source])
    if len(seen_on) >= 2:
        score += _W_CROSS_SOURCE
        c.signals.append(RecommendationSignal(
            code="cross_source",
            explanation=f"Flagged by {len(seen_on)} sources: {', '.join(seen_on)}.",
        ))

    # --- Genre match (soft) ---
    matching = [g for g in c.genre_tags if g in _OUR_GENRES]
    if matching:
        score += _W_GENRE * len(matching)
        c.signals.append(RecommendationSignal(
            code="genre_match",
            explanation=f"Tagged: {', '.join(matching[:3])}.",
        ))

    # --- Freshness ---
    if c.release_date:
        try:
            rel = datetime.strptime(c.release_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - rel).days
            if 0 <= days_old <= _FRESH_DAYS:
                score += _W_FRESH
                c.signals.append(RecommendationSignal(
                    code="fresh_release",
                    explanation=f"Released {days_old} day{'s' if days_old != 1 else ''} ago.",
                ))
        except ValueError:
            pass

    # --- Chart position (Juno and any other source that sets chart_position) ---
    chart_pos = c.raw_metadata.get("chart_position")
    if chart_pos and isinstance(chart_pos, int) and 1 <= chart_pos <= _CHART_SCALE:
        chart_bonus = _W_CHART_TOP * (1 - (chart_pos - 1) / _CHART_SCALE)
        score += chart_bonus
        c.signals.append(RecommendationSignal(
            code="chart_position",
            explanation=f"#{chart_pos} on the {c.source.title()} weekly chart.",
        ))

    # --- Bandcamp discovery bonus (compensates for no chart_position signal) ---
    if c.source == "bandcamp":
        score += _W_BANDCAMP
        c.signals.append(RecommendationSignal(
            code="bandcamp_discovery",
            explanation="Bandcamp discovery — independent release outside chart sources.",
        ))

    # --- Human-curated source bonus ---
    if c.source == "subsurface_selections":
        score += _W_HUMAN_CURATED
        c.signals.append(RecommendationSignal(
            code="human_curated",
            explanation="Hand-picked by Subsurface Selections — human editorial curation.",
        ))

    c.score = round(score, 2)


def _assign_sections(
    ranked: list[Candidate],
    settings,
) -> dict[str, list[Candidate]]:
    top_n = settings.pipeline_top_picks_count
    label_n = settings.pipeline_label_watch_count
    artist_n = settings.pipeline_artist_watch_count
    wildcard_n = settings.pipeline_wildcard_count

    used: set[int] = set()
    MAX_PER_ARTIST = 2
    MAX_PER_RELEASE = 2
    MAX_PER_GENRE = 3
    # "electronic" is too broad to cap — nearly every track carries it
    _UNCAPPED_GENRES = {"electronic"}

    # Genre cap is global across all sections so one genre can't flood the full report
    genre_counts: dict[str, int] = {}

    def pick(n: int, require_signal: str = None) -> list[Candidate]:
        # Artist/release caps reset per section so an artist can appear in different
        # sections (e.g. top_picks and label_watch serve distinct curatorial purposes)
        artist_counts: dict[str, int] = {}
        release_counts: dict[str, int] = {}
        result = []
        for c in ranked:
            if id(c) in used:
                continue
            if require_signal and not any(s.code == require_signal for s in c.signals):
                continue
            artist_key = normalise_artist(c.artist)
            release_key = (c.release_name or "").strip().lower()
            if artist_counts.get(artist_key, 0) >= MAX_PER_ARTIST:
                continue
            if release_key and release_counts.get(release_key, 0) >= MAX_PER_RELEASE:
                continue
            # Genre cap: use first specific (non-broad) genre tag; exempt if none found
            cap_genre = next(
                (g for g in c.genre_tags if g in _OUR_GENRES and g not in _UNCAPPED_GENRES),
                None,
            )
            if cap_genre and genre_counts.get(cap_genre, 0) >= MAX_PER_GENRE:
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

    top_picks = pick(top_n)
    label_watch = pick(label_n, require_signal="label_match")
    artist_watch = pick(artist_n, require_signal="known_artist")
    wildcards = pick(wildcard_n)

    genre_summary = ", ".join(f"{g}: {n}" for g, n in sorted(genre_counts.items()))
    logger.info(
        f"[ranker] Sections — top_picks: {len(top_picks)}, "
        f"label_watch: {len(label_watch)}, artist_watch: {len(artist_watch)}, "
        f"wildcards: {len(wildcards)} | genres: {genre_summary or 'none tagged'}"
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
) -> dict[str, list[Candidate]]:
    """
    Score all candidates, assign signals, sort, and split into report sections.
    Returns a dict with keys: top_picks, label_watch, artist_watch, wildcards.

    label_seed: pre-filter candidates used for label relevance derivation.
    Pass the full source candidate list before filter_known/filter_history so
    that known artists (who are filtered out of candidates) still contribute
    their labels. Defaults to candidates if not provided.
    """
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    relevant_labels = _build_relevant_labels(label_seed if label_seed is not None else candidates, profiles_lower)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections(ranked, settings)


def _assign_sections_mix_prep(
    ranked: list[Candidate],
    settings,
) -> dict[str, list[Candidate]]:
    top_n = settings.pipeline_mix_prep_top_picks_count
    deep_n = settings.pipeline_mix_prep_deep_cuts_count

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
    logger.info(f"[ranker] Mix-prep sections — top_picks: {len(top_picks)}, deep_cuts: {len(deep_cuts)}")
    return {"top_picks": top_picks, "deep_cuts": deep_cuts}


def rank_candidates_mix_prep(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
) -> dict[str, list[Candidate]]:
    """
    Score and section candidates for a mix-prep run.
    Same scoring as rank_candidates but uses two sections (top_picks, deep_cuts)
    with no per-genre cap — the genre has already been filtered upstream.
    """
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    relevant_labels = _build_relevant_labels(label_seed if label_seed is not None else candidates, profiles_lower)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Mix-prep scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections_mix_prep(ranked, settings)


def all_section_candidates(sections: dict[str, list[Candidate]]) -> list[Candidate]:
    """Flatten all section candidates into a single list for history recording."""
    seen: set[int] = set()
    result = []
    for candidates in sections.values():
        for c in candidates:
            if id(c) not in seen:
                result.append(c)
                seen.add(id(c))
    return result
