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
_FEAT_RE = re.compile(r"\s+(feat\.|ft\.|featuring)\s+.+$", re.IGNORECASE)


def normalise_title(title: str) -> str:
    t = title.strip().lower()
    t = _VERSION_RE.sub("", t)
    t = _FEAT_RE.sub("", t)
    return t.strip()


def normalise_artist(artist: str) -> str:
    a = artist.strip().lower()
    a = _FEAT_RE.sub("", a)
    return a.strip()


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
    """Remove candidates that match a track in the known-track exclusion set."""
    result = []
    removed = 0
    for c in candidates:
        if c.key in known_keys or make_dedup_key(c.artist, c.title) in known_keys:
            removed += 1
        else:
            result.append(c)
    logger.info(f"[dedup] Filtered {removed} known tracks → {len(result)} candidates remaining")
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
