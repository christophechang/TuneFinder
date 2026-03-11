import json
import os
import re

from src.logger import get_logger
from src.models import ArtistProfile, Track

logger = get_logger(__name__)

_KNOWN_TRACKS_FILE = "known_tracks.json"
_ARTIST_PROFILES_FILE = "artist_profiles.json"

# Splits collaborative artist strings into individual names.
# Handles: "Bakey, Kasia" / "Calibre feat. Jabu" / "A & B" / "A x B"
_SPLIT_RE = re.compile(r"\s*,\s*|\s+feat\.\s+|\s+ft\.\s+|\s+&\s+|\s+x\s+", re.IGNORECASE)


def _split_artists(artist_string: str) -> list[str]:
    parts = _SPLIT_RE.split(artist_string)
    return [p.strip() for p in parts if p.strip()]


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


def build_known_track_keys(tracks: list[Track]) -> set[str]:
    """Return the normalised dedup keys for all known tracks.

    Uses make_dedup_key (strips version suffixes like '(Original Mix)', feat
    credits, etc.) so that known tracks match source items regardless of how
    version info is appended.
    """
    from src.pipeline.dedup import make_dedup_key
    return {make_dedup_key(t.artist, t.title) for t in tracks}


# ---------------------------------------------------------------------------
# Persistence — known tracks
# ---------------------------------------------------------------------------

def save_known_tracks(tracks: list[Track], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _KNOWN_TRACKS_FILE)
    keys = sorted(build_known_track_keys(tracks))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
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
        "associated_labels": p.associated_labels,
        "track_titles": p.track_titles,
    }


def _dict_to_profile(d: dict) -> ArtistProfile:
    return ArtistProfile(
        name=d["name"],
        play_count=d.get("play_count", 0),
        genres_seen=d.get("genres_seen", []),
        associated_labels=d.get("associated_labels", []),
        track_titles=d.get("track_titles", []),
    )


def save_artist_profiles(profiles: dict[str, ArtistProfile], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _ARTIST_PROFILES_FILE)
    data = {name: _profile_to_dict(p) for name, p in profiles.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
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
