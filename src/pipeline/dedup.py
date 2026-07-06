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


# ---------------------------------------------------------------------------
# Remix-aware track identity (issue #9) — OFF by default
#
# Today _VERSION_RE strips ALL version suffixes, so owning "Title (Original Mix)"
# suppresses every future named remix of that title. When remix-awareness is on,
# a *named* remix/VIP/rework becomes a distinct identity (an extra `rmx:<name>`
# qualifier), while Original/Extended/Radio/album versions keep merging exactly
# as they do today.
#
# Classification of a parenthetical/bracketed version tag:
#   GENERIC (merge — no qualifier, key identical to today):
#     original mix, extended mix, extended, radio edit, radio version, club mix,
#     album version, ep version, vocal mix, instrumental, dub — plus anything with
#     no recognised remix keyword (e.g. "(Reprise)", "(Original)").
#     JUDGEMENT: "instrumental" and a plain "dub" are borderline distinct works,
#     but we treat them as GENERIC to stay conservative about duplicate
#     recommendations — a DJ who owns the vocal almost never wants the
#     instrumental/dub resurfaced as a fresh discovery.
#   NAMED (distinct — qualifier `rmx:<name>`):
#     "<name> <keyword>" where keyword ∈ {remix, mix, edit, rework, bootleg, vip,
#     flip, refix, remake, version} and <name> is non-empty after removing the
#     keyword and any leading/trailing GENERIC modifier words (extended, radio,
#     club). So "(Calibre Remix)"/"(Calibre remix)"/"[Calibre Remix]" → rmx:calibre;
#     "(Break's Deep Mix)" → rmx:break's deep; "(Extended Remix)" → empty name →
#     GENERIC (merges with the original). A bare "(VIP)" → rmx:vip (a VIP is a
#     distinct work by the same artist).
# ---------------------------------------------------------------------------

# Exact version tags that always merge (kept verbatim so multi-word generics whose
# lead word is not itself a generic modifier — e.g. "album version", "vocal mix" —
# are recognised without over-stripping).
_GENERIC_VERSIONS = {
    "original mix", "extended mix", "extended", "radio edit", "radio version",
    "club mix", "album version", "ep version", "vocal mix", "instrumental", "dub",
}
# Modifier words stripped from a remix name before deciding named-vs-generic, so
# "(Extended Remix)"/"(Radio Mix)"/"(Club Edit)" collapse to an empty name → merge.
_GENERIC_MODIFIERS = {"extended", "radio", "club"}
# Remix keywords, longest-first in the alternation so "remix" wins over "mix".
_NAMED_RE = re.compile(
    r"^(?P<name>.*?)\b(?P<kw>remix|rework|bootleg|refix|remake|flip|version|vip|edit|mix)\b\s*$"
)
# Any parenthesised or bracketed group (inner text captured).
_PAREN_GROUP_RE = re.compile(r"[\(\[]([^)\]]*)[\)\]]")


def _strip_generic_modifiers(name: str) -> str:
    words = name.split()
    while words and words[0] in _GENERIC_MODIFIERS:
        words.pop(0)
    while words and words[-1] in _GENERIC_MODIFIERS:
        words.pop()
    return " ".join(words)


def _classify_version(inner: str) -> str | None:
    """Return an `rmx:<name>` qualifier for a NAMED remix tag, or None if the tag
    is generic (merges). `inner` is the text inside the parens, lowercased.
    """
    s = " ".join(inner.split())  # collapse whitespace
    if not s or s in _GENERIC_VERSIONS:
        return None
    m = _NAMED_RE.match(s)
    if not m:
        return None
    name = _strip_generic_modifiers(m.group("name").strip())
    if not name:
        # Keyword with no distinguishing name. A bare "VIP" is still a distinct
        # work by the same artist; everything else (bare "Remix", "Mix", …) merges.
        return "rmx:vip" if m.group("kw") == "vip" else None
    return f"rmx:{name}"


def make_dedup_key(artist: str, title: str, remix_aware: bool = False) -> str:
    """Canonical (artist, title) identity key.

    remix_aware=False (default) is the legacy behaviour, byte-for-byte: version
    suffixes and feat. credits are stripped so every version of a title collapses
    to `artist||title`. All existing callers rely on this exact output — do not
    change it.

    remix_aware=True keeps generic versions merging but gives a *named* remix its
    own identity: `artist||title||rmx:<name>` (see the module comment above).
    """
    if not remix_aware:
        return f"{normalise_artist(artist)}||{normalise_title(title)}"

    a = normalise_artist(artist)
    t = title.strip().lower()

    # Find the (last) named-remix group and note its span so it can be removed
    # from the base title. Generic groups are left for _VERSION_RE to strip below,
    # which keeps the base identical to the legacy key for every generic tag.
    qualifier: str | None = None
    named_span: tuple[int, int] | None = None
    for m in _PAREN_GROUP_RE.finditer(t):
        q = _classify_version(m.group(1))
        if q is not None:
            qualifier = q
            named_span = m.span()
    if named_span is not None:
        t = t[: named_span[0]] + t[named_span[1] :]

    # Extract remaining version parentheticals first, then feat-strip the base —
    # same order as the legacy path (normalise_title): _VERSION_RE before _FEAT_RE.
    t = _VERSION_RE.sub("", t)
    t = _FEAT_RE.sub("", t)
    base = t.strip()

    key = f"{a}||{base}"
    if qualifier:
        key += f"||{qualifier}"
    return key


# ---------------------------------------------------------------------------
# Cross-source deduplication
# ---------------------------------------------------------------------------

def _richness(item: SourceItem) -> int:
    return sum([bool(item.label), bool(item.release_date), bool(item.release_name), len(item.genre_tags)])


# Embed/display metadata worth preserving from merged-away duplicates.
# Backfill only — the winning item's values are never overwritten.
_MERGE_BACKFILL_KEYS = (
    "beatport_id", "volumo_track_id", "volumo_album_id",
    "bandcamp_album_id", "bpm", "key", "keysign",
)


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

    # Backfill embed metadata from losing items without overwriting winner's values.
    losers = [i for i in items if i is not best]
    for key in _MERGE_BACKFILL_KEYS:
        if best.raw_metadata.get(key) is None:
            for loser in losers:
                val = loser.raw_metadata.get(key)
                if val is not None:
                    best.raw_metadata[key] = val
                    break

    return best


def deduplicate_source_items(items: list[SourceItem], remix_aware: bool = False) -> list[SourceItem]:
    """
    Group items by normalised (artist, title) key and merge duplicates.
    Items seen on multiple sources are merged into one with all sources recorded.

    When remix_aware is True, named remixes group separately from the original
    (see make_dedup_key); generic versions still merge exactly as today.
    """
    groups: dict[str, list[SourceItem]] = {}
    for item in items:
        key = make_dedup_key(item.artist, item.title, remix_aware)
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


def filter_known(candidates: list[Candidate], known_keys: set[str], remix_aware: bool = False) -> list[Candidate]:
    """Remove candidates that match a track in the known-track exclusion set.

    Checks both the release-level title and any individual tracks nested in
    raw_metadata["tracks"], so a release whose tracks are already owned is
    correctly excluded.

    `c.key` is the raw artist||title property (unaffected by remix-awareness); the
    make_dedup_key checks are where remix-awareness lands. Under remix_aware, an
    owned original no longer blocks a named remix of the same title, and vice
    versa — provided known_keys was built remix-aware too (build_known_track_keys).
    """
    result = []
    removed = 0
    for c in candidates:
        if c.key in known_keys or make_dedup_key(c.artist, c.title, remix_aware) in known_keys:
            removed += 1
            continue
        # Also check individual tracks embedded in the release
        tracks = c.raw_metadata.get("tracks", [])
        if any(
            make_dedup_key(t.get("artist", c.artist), t["title"], remix_aware) in known_keys
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


def filter_release_date(
    candidates: list[Candidate],
    window_days: int,
    today: "date | None" = None,
) -> list[Candidate]:
    """Drop candidates whose release_date is older than window_days ago.

    Items with no release_date are always kept — we can't confirm they're stale,
    so we give them the benefit of the doubt. Some Bandcamp items may carry dates
    now (since the discover_web migration), but many undated items remain.

    `today` sets the reference date the cutoff is measured back from. Default
    (None) uses today's UTC date — the live-run behaviour. Offline replay
    (src/pipeline/replay.py) passes the archived week's own reference date so a
    week replayed months later still evaluates its window against that week,
    not against now (otherwise every candidate ages out).
    """
    from datetime import datetime, timedelta, timezone, date
    ref = today if today is not None else datetime.now(timezone.utc).date()
    cutoff = (ref - timedelta(days=window_days)).isoformat()
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


def filter_history(candidates: list[Candidate], history_keys: set[str], remix_aware: bool = False) -> list[Candidate]:
    """Remove candidates that have been previously recommended.

    Under remix_aware, a previously recommended named remix no longer blocks the
    original (or a different remix) — provided history_keys was built remix-aware
    (build_history_keys), which also keeps the legacy key so old records still
    block their exact old-style matches.
    """
    result = []
    removed = 0
    for c in candidates:
        if c.key in history_keys or make_dedup_key(c.artist, c.title, remix_aware) in history_keys:
            removed += 1
        else:
            result.append(c)
    logger.info(f"[dedup] Filtered {removed} previously recommended → {len(result)} candidates remaining")
    return result
