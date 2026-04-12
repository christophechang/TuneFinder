"""
Candidate deduplication, normalisation, and exclusion filtering.

Normalisation strips common remix suffixes and featured artists so that
"Track Title (Original Mix)" and "Track Title" resolve to the same key,
and "Artist feat. Someone" and "Artist" resolve to the same artist key.

Cross-source deduplication merges items with the same normalised key into
one Candidate, keeping the richest metadata and recording all sources seen.
"""
import re

from src.logger import get_logger
from src.models import Candidate, RecommendationSignal, SourceItem

logger = get_logger(__name__)

# Strip remix/version/edit suffixes in parentheses or brackets
_VERSION_RE = re.compile(
    r"\s*[\(\[](original mix|extended mix|extended|radio edit|radio version|"
    r"vip|dub|instrumental|vocal mix|club mix|ep version|album version|"
    r"[^)\]]*\b(remix|mix|edit|version|bootleg|rework|reprise)\b[^)\]]*)\s*[\)\]]",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)
# Splits multi-artist strings on common separators: " / ", " & ", " x ", " vs ", " vs. "
# Requires whitespace around x/vs to avoid false splits inside artist names.
_ARTIST_SEP_RE = re.compile(r"\s*/\s*|\s*&\s*|\s+(?:x|vs\.?)\s+", re.IGNORECASE)


def normalise_title(title: str) -> str:
    t = title.strip().lower()
    t = _VERSION_RE.sub("", t)
    t = _FEAT_RE.sub("", t)
    return t.strip()


def normalise_artist(artist: str) -> str:
    a = artist.strip().lower()
    a = _FEAT_RE.sub("", a)
    # Canonicalise multi-artist separators so "A / B", "A & B", "A x B" all
    # produce the same key as "A, B" (the format Beatport/Traxsource use).
    parts = [p.strip() for p in _ARTIST_SEP_RE.split(a) if p.strip()]
    return ", ".join(parts)


def make_dedup_key(artist: str, title: str) -> str:
    return f"{normalise_artist(artist)}||{normalise_title(title)}"


# ---------------------------------------------------------------------------
# Cross-source deduplication
# ---------------------------------------------------------------------------

def _richness(item: SourceItem) -> int:
    return sum([bool(item.label), bool(item.release_date), bool(item.release_name), len(item.genre_tags)])


def _merge_group(items: list[SourceItem]) -> SourceItem:
    """Merge a group of duplicate SourceItems — keep richest metadata."""
    best = max(items, key=_richness)

    all_genres: list[str] = []
    for item in items:
        for g in item.genre_tags:
            if g not in all_genres:
                all_genres.append(g)
    best.genre_tags = all_genres
    best.raw_metadata["seen_on_sources"] = sorted({i.source for i in items})
    return best


def deduplicate_source_items(items: list[SourceItem]) -> list[SourceItem]:
    """
    Group items by normalised (artist, title) key and merge duplicates.
    Items seen on multiple sources are merged into one with all sources recorded.
    """
    groups: dict[str, list[SourceItem]] = {}
    for item in items:
        key = make_dedup_key(item.artist, item.title)
        groups.setdefault(key, []).append(item)

    merged = [_merge_group(group) for group in groups.values()]
    logger.info(
        f"[dedup] {len(items)} items → {len(merged)} unique "
        f"({len(items) - len(merged)} cross-source duplicates merged)"
    )
    return merged


# ---------------------------------------------------------------------------
# Conversion and filtering
# ---------------------------------------------------------------------------

def items_to_candidates(items: list[SourceItem]) -> list[Candidate]:
    return [
        Candidate(
            artist=item.artist,
            title=item.title,
            link=item.link,
            source=item.source,
            label=item.label,
            release_date=item.release_date,
            release_name=item.release_name,
            genre_tags=item.genre_tags,
            raw_metadata=item.raw_metadata,
        )
        for item in items
    ]


def filter_known(candidates: list[Candidate], known_keys: set[str]) -> list[Candidate]:
    """Remove candidates that match a track in the known-track exclusion set.

    Checks both the release-level title and any individual tracks nested in
    raw_metadata["tracks"] (populated by the Juno scraper), so that an EP
    whose tracks are already owned is correctly excluded.
    """
    result = []
    removed = 0
    for c in candidates:
        if c.key in known_keys or make_dedup_key(c.artist, c.title) in known_keys:
            removed += 1
            continue
        # Also check individual tracks embedded in the release (Juno EP tracks)
        tracks = c.raw_metadata.get("tracks", [])
        if any(
            make_dedup_key(t.get("artist", c.artist), t["title"]) in known_keys
            for t in tracks if t.get("title")
        ):
            removed += 1
            continue
        result.append(c)
    logger.info(f"[dedup] Filtered {removed} known tracks → {len(result)} candidates remaining")
    return result


def filter_genre(candidates: list[Candidate], genre: str) -> list[Candidate]:
    """Keep only candidates tagged with the specified genre."""
    result = [c for c in candidates if genre in c.genre_tags]
    logger.info(f"[dedup] Genre filter '{genre}': {len(candidates) - len(result)} removed → {len(result)} remaining")
    return result


def filter_genre_exclusions(
    candidates: list[Candidate],
    genre: str,
    exclusions: dict[str, list[str]],
) -> list[Candidate]:
    """Remove candidates that carry any tag that is mutually exclusive with the target genre.

    Handles cross-source tag merging where a track may be tagged with both the
    target genre and a contradictory one (e.g. electronica + ukg after dedup merge).
    """
    excluded_tags = set(exclusions.get(genre, []))
    if not excluded_tags:
        return candidates
    result = [c for c in candidates if not excluded_tags.intersection(c.genre_tags)]
    removed = len(candidates) - len(result)
    if removed:
        logger.info(
            f"[dedup] Genre exclusion filter '{genre}': {removed} removed → {len(result)} remaining"
        )
    return result


def filter_release_date(candidates: list[Candidate], window_days: int) -> list[Candidate]:
    """Drop candidates whose release_date is older than window_days ago.

    Items with no release_date (e.g. Bandcamp) are always kept — we can't
    confirm they're stale, so we give them the benefit of the doubt.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    result = []
    removed = 0
    for c in candidates:
        if c.release_date is None or c.release_date >= cutoff:
            result.append(c)
        else:
            removed += 1
    if removed:
        logger.info(
            f"[dedup] Release date filter ({window_days}d, cutoff {cutoff}): "
            f"{removed} removed → {len(result)} remaining"
        )
    return result


def filter_history(candidates: list[Candidate], history_keys: set[str]) -> list[Candidate]:
    """Remove candidates that have been previously recommended."""
    result = []
    removed = 0
    for c in candidates:
        if c.key in history_keys or make_dedup_key(c.artist, c.title) in history_keys:
            removed += 1
        else:
            result.append(c)
    logger.info(f"[dedup] Filtered {removed} previously recommended → {len(result)} candidates remaining")
    return result
