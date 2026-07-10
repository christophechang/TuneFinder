import json
import os
import re
from datetime import datetime, timezone

from src.logger import get_logger
from src.models import ArtistProfile, Mix, Track
from src.pipeline.storage import atomic_write_json

logger = get_logger(__name__)

# Average days per month used to convert a mix's age into months for the
# recency-weight half-life calculation (issue #11) — 365.25 / 12.
_DAYS_PER_MONTH = 30.44

_KNOWN_TRACKS_FILE = "known_tracks.json"
_ARTIST_PROFILES_FILE = "artist_profiles.json"
_GENRE_AFFINITY_FILE = "genre_affinity.json"

# Splits collaborative artist strings into individual names.
# Handles: "Bakey, Kasia" / "Calibre feat. Jabu" / "A & B" / "A x B"
_SPLIT_RE = re.compile(r"\s*,\s*|\s+feat\.\s+|\s+ft\.\s+|\s+&\s+|\s+x\s+", re.IGNORECASE)


def _split_artists(artist_string: str) -> list[str]:
    parts = _SPLIT_RE.split(artist_string)
    return [p.strip() for p in parts if p.strip()]


def resolve_profile(
    name_part: str,
    profiles_lower: dict[str, ArtistProfile],
    aliases: dict[str, str] | None = None,
) -> ArtistProfile | None:
    """Resolve a written artist-name part to its ArtistProfile, if any.

    Tries a direct lower().strip() lookup first (today's behaviour). If that
    misses and an alias map is given (alias_lower -> canonical_lower, see
    Settings.artist_aliases), resolves alias -> canonical -> profile. Tiny and
    dependency-free — no logging, no IO.
    """
    key = name_part.lower().strip()
    profile = profiles_lower.get(key)
    if profile is not None:
        return profile
    if aliases:
        canonical = aliases.get(key)
        if canonical:
            return profiles_lower.get(canonical)
    return None


# ---------------------------------------------------------------------------
# Profile building
# ---------------------------------------------------------------------------

def build_artist_profiles(tracks: list[Track]) -> dict[str, ArtistProfile]:
    """
    Build an ArtistProfile for each individual artist from the track catalogue.

    Collaborative strings (e.g. "Bakey, Kasia") are split so each artist
    gets individual credit. play_count is weighted by recurrence_count so
    tracks that appear in multiple mixes contribute proportionally more.
    """
    profiles: dict[str, ArtistProfile] = {}

    for track in tracks:
        for artist_name in _split_artists(track.artist):
            if artist_name not in profiles:
                profiles[artist_name] = ArtistProfile(name=artist_name)

            profile = profiles[artist_name]
            profile.play_count += track.recurrence_count

            for genre in track.genres_seen:
                if genre not in profile.genres_seen:
                    profile.genres_seen.append(genre)

            if track.title not in profile.track_titles:
                profile.track_titles.append(track.title)

    logger.info(f"[profile] Built {len(profiles)} artist profiles")
    return profiles


def apply_recency_weights(
    profiles: dict[str, ArtistProfile],
    mixes: list[Mix],
    half_life_months: float,
    now: datetime | None = None,
) -> None:
    """Add recency-weighted play counts to matched profiles, in place (issue #11).

    For each mix with a parseable `published_at` (ISO date/datetime string —
    missing, blank, or unparseable means the mix is skipped entirely, no
    partial credit), every tracklist TrackRef's artist string is split
    (_split_artists, same collaborator-splitting rule as build_artist_profiles)
    and each part resolved to a profile via a direct lower().strip() lookup —
    aliases aren't needed here since profiles were built from the same
    catalogue strings the mixes come from. Each matched profile's
    `recency_weighted_play_count` gains `0.5 ** (age_months / half_life_months)`
    per track occurrence (age_months = days_old / 30.44), mirroring how
    `play_count` counts recurrences — one increment per occurrence, not one
    per mix.

    A profile never referenced by any dated mix is left untouched (stays at
    whatever it already was — 0.0 for a freshly built profile). Callers must
    treat 0.0 as "no recency data available", not "never played" — the ranker
    falls back to raw play_count in that case (see ranker._score).

    Stored values are rounded to 3 decimal places.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    profiles_lower = {k.lower(): v for k, v in profiles.items()}

    for mix in mixes:
        published_raw = (mix.published_at or "").strip()
        if not published_raw:
            continue
        try:
            published = datetime.fromisoformat(published_raw)
        except ValueError:
            logger.warning(f"[profile] Mix {mix.id!r} has unparseable published_at {published_raw!r} — skipped")
            continue

        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        age_days = (now - published).total_seconds() / 86400
        age_months = age_days / _DAYS_PER_MONTH
        weight = 0.5 ** (age_months / half_life_months)

        for track_ref in mix.tracklist:
            for part in _split_artists(track_ref.artist):
                profile = profiles_lower.get(part.lower().strip())
                if profile is None:
                    continue
                profile.recency_weighted_play_count += weight

    for profile in profiles_lower.values():
        profile.recency_weighted_play_count = round(profile.recency_weighted_play_count, 3)

    logger.info(f"[profile] Applied recency weights from {len(mixes)} mixes (half-life={half_life_months}mo)")


def build_genre_affinity(tracks: list[Track]) -> dict[str, float]:
    """
    Build a corpus-level genre distribution from the mix catalogue.

    Each track's genres_seen tags are weighted by recurrence_count (a track
    played in 5 mixes counts 5x toward every genre it's tagged with) so the
    result reflects how much of your actual playing time skews toward each
    genre, not just how many distinct tracks carry the tag. Output shares sum
    to 1.0; empty input (or tracks with no genre tags at all) returns {}.
    """
    weighted_counts: dict[str, int] = {}
    for track in tracks:
        for genre in track.genres_seen:
            weighted_counts[genre] = weighted_counts.get(genre, 0) + track.recurrence_count

    total = sum(weighted_counts.values())
    if total == 0:
        logger.info("[profile] No genre data — genre affinity is empty")
        return {}

    affinity = {genre: count / total for genre, count in weighted_counts.items()}
    logger.info(f"[profile] Built genre affinity for {len(affinity)} genres")
    return affinity


def build_known_track_keys(tracks: list[Track], remix_aware: bool = False) -> set[str]:
    """Return the normalised dedup keys for all known tracks.

    Uses make_dedup_key (strips version suffixes like '(Original Mix)', feat
    credits, etc.) so that known tracks match source items regardless of how
    version info is appended.

    When remix_aware is True, emit BOTH the remix-aware key AND the legacy
    (flag-off) key for every track. The remix-aware key gives named remixes their
    own identity going forward; the legacy key keeps backward compatibility so an
    old known_tracks.json (or a track owned under the old regime) still blocks its
    exact old-style match even with the flag on.
    """
    from src.pipeline.dedup import make_dedup_key
    keys: set[str] = set()
    for t in tracks:
        keys.add(make_dedup_key(t.artist, t.title))
        if remix_aware:
            keys.add(make_dedup_key(t.artist, t.title, remix_aware=True))
    return keys


# ---------------------------------------------------------------------------
# Persistence — known tracks
# ---------------------------------------------------------------------------

def save_known_tracks(tracks: list[Track], data_dir: str, remix_aware: bool = False) -> None:
    path = os.path.join(data_dir, _KNOWN_TRACKS_FILE)
    keys = sorted(build_known_track_keys(tracks, remix_aware))
    atomic_write_json(path, keys)
    logger.info(f"[profile] Saved {len(keys)} known track keys to {path}")


def load_known_tracks(data_dir: str) -> set[str]:
    path = os.path.join(data_dir, _KNOWN_TRACKS_FILE)
    if not os.path.exists(path):
        logger.warning(f"[profile] No known tracks file at {path} — returning empty set")
        return set()
    with open(path, "r", encoding="utf-8") as f:
        keys = json.load(f)
    logger.info(f"[profile] Loaded {len(keys)} known track keys from {path}")
    return set(keys)


# ---------------------------------------------------------------------------
# Persistence — artist profiles
# ---------------------------------------------------------------------------

def _profile_to_dict(p: ArtistProfile) -> dict:
    return {
        "name": p.name,
        "play_count": p.play_count,
        "genres_seen": p.genres_seen,
        "track_titles": p.track_titles,
        "recency_weighted_play_count": p.recency_weighted_play_count,
    }


def _dict_to_profile(d: dict) -> ArtistProfile:
    return ArtistProfile(
        name=d["name"],
        play_count=d.get("play_count", 0),
        genres_seen=d.get("genres_seen", []),
        track_titles=d.get("track_titles", []),
        # Default 0.0 for profile files saved before issue #11 — falls back to
        # raw play_count in scoring (see ranker._score), never zeroes anything out.
        recency_weighted_play_count=d.get("recency_weighted_play_count", 0.0),
    )


def save_artist_profiles(profiles: dict[str, ArtistProfile], data_dir: str) -> None:
    path = os.path.join(data_dir, _ARTIST_PROFILES_FILE)
    data = {name: _profile_to_dict(p) for name, p in profiles.items()}
    atomic_write_json(path, data)
    logger.info(f"[profile] Saved {len(profiles)} artist profiles to {path}")


def load_artist_profiles(data_dir: str) -> dict[str, ArtistProfile]:
    path = os.path.join(data_dir, _ARTIST_PROFILES_FILE)
    if not os.path.exists(path):
        logger.warning(f"[profile] No artist profiles file at {path} — returning empty dict")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    profiles = {name: _dict_to_profile(d) for name, d in data.items()}
    logger.info(f"[profile] Loaded {len(profiles)} artist profiles from {path}")
    return profiles


# ---------------------------------------------------------------------------
# Persistence — genre affinity
# ---------------------------------------------------------------------------

def save_genre_affinity(affinity: dict[str, float], data_dir: str) -> None:
    path = os.path.join(data_dir, _GENRE_AFFINITY_FILE)
    atomic_write_json(path, affinity)
    logger.info(f"[profile] Saved genre affinity for {len(affinity)} genres to {path}")


def load_genre_affinity(data_dir: str) -> dict[str, float]:
    path = os.path.join(data_dir, _GENRE_AFFINITY_FILE)
    if not os.path.exists(path):
        logger.warning(f"[profile] No genre affinity file at {path} — returning empty dict")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        affinity = json.load(f)
    logger.info(f"[profile] Loaded genre affinity for {len(affinity)} genres from {path}")
    return affinity
