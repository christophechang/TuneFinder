"""
SoundCloud source fetcher — official API track search (free-download/bootleg lane).

Uses the official public API (https://api.soundcloud.com) with an app-level
client_credentials token: no user login, no callback URL. App registration
requires an Artist Pro subscription; credentials come from
SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET in .env. The token endpoint is
itself rate-limited, so tokens are cached in data/soundcloud_token.json.

Key behaviours:
- One search per configured target: {tf_tag, q?, genres?, tags?}
- created_at[from] window derived from lookback_days ("yyyy-mm-dd hh:mm:ss");
  the API currently ignores it, so the window is also enforced client-side
- downloadable_only (default true) keeps only tracks with downloads enabled —
  this lane exists to surface free DLs/bootlegs the stores don't carry
- linked_partitioning pagination via next_href, capped at _MAX_PAGES
- Auth header scheme is "OAuth <token>" (SoundCloud-specific, not Bearer)
"""
import json
import os
import re
import time
import urllib.parse
from datetime import date, timedelta

import requests

from src.fetchers.common import polite_sleep
from src.logger import get_logger
from src.models import SourceItem
from src.pipeline.storage import atomic_write_json

logger = get_logger(__name__)

_API_BASE = "https://api.soundcloud.com"
_TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
_CACHE_FILE = "soundcloud_token.json"
_EXPIRY_MARGIN_S = 60
_TIMEOUT = 25
_MAX_PAGES = 3
# created_at comes back as ISO ("2026-07-10T07:00:00Z") or the legacy
# "2026/07/10 07:00:00 +0000" form depending on API era — accept both.
_DATE_RE = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})")


class SoundCloudAuthError(Exception):
    """Raised when SoundCloud authentication cannot produce a valid access token."""


# ---------------------------------------------------------------------------
# Token cache (client_credentials)
# ---------------------------------------------------------------------------

def _load_cache(data_dir: str) -> dict | None:
    path = os.path.join(data_dir, _CACHE_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # Structural validation — a malformed cache must recover via re-fetch, never crash.
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        return None
    return data


def _save_cache(data_dir: str, tok: dict) -> None:
    now = time.time()
    payload = {
        "access_token": tok["access_token"],
        "expires_at": now + int(tok.get("expires_in", 0)),
        "obtained_at": now,
    }
    path = os.path.join(data_dir, _CACHE_FILE)
    atomic_write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _fetch_token(session: requests.Session, client_id: str, client_secret: str) -> dict:
    resp = session.post(
        _TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SoundCloudAuthError(f"token request failed: HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise SoundCloudAuthError("token response missing access_token")
    return data


def _get_access_token(settings, session: requests.Session) -> str:
    """Return a valid app access token, or raise SoundCloudAuthError."""
    client_id = settings.soundcloud_client_id
    client_secret = settings.soundcloud_client_secret
    if not client_id or not client_secret:
        raise SoundCloudAuthError(
            "credentials not set (SOUNDCLOUD_CLIENT_ID/SOUNDCLOUD_CLIENT_SECRET)"
        )

    cache = _load_cache(settings.data_dir)
    if cache and (cache.get("expires_at", 0) - time.time()) > _EXPIRY_MARGIN_S:
        return cache["access_token"]

    tok = _fetch_token(session, client_id, client_secret)
    _save_cache(settings.data_dir, tok)
    return tok["access_token"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _get_json(url: str, session: requests.Session) -> dict:
    resp = session.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _build_search_url(target: dict, created_from: str, limit: int) -> str:
    params: dict = {}
    for key in ("q", "genres", "tags"):
        val = (target.get(key) or "").strip()
        if val:
            params[key] = val
    params["created_at[from]"] = f"{created_from} 00:00:00"
    params["limit"] = limit
    params["linked_partitioning"] = "true"
    return f"{_API_BASE}/tracks?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Track parsing
# ---------------------------------------------------------------------------

def _parse_release_date(created_at) -> str | None:
    m = _DATE_RE.match(str(created_at or ""))
    if not m:
        return None
    return "-".join(m.groups())


def _parse_track(track: dict, tag: str) -> SourceItem | None:
    title = (track.get("title") or "").strip()
    artist = (track.get("metadata_artist") or "").strip() \
        or ((track.get("user") or {}).get("username") or "").strip()
    # The API appends utm_* tracking params to permalink_url — keep links clean.
    link = (track.get("permalink_url") or "").split("?", 1)[0]
    if not title or not artist or not link:
        return None

    return SourceItem(
        source="soundcloud",
        artist=artist,
        title=title,
        link=link,
        label=track.get("label_name") or None,
        release_date=_parse_release_date(track.get("created_at")),
        genre_tags=[tag],
        raw_metadata={
            "soundcloud_id": track.get("id"),
            "urn": track.get("urn"),
            "downloadable": track.get("downloadable"),
            "download_count": track.get("download_count"),
            "playback_count": track.get("playback_count"),
            "favoritings_count": track.get("favoritings_count"),
            "purchase_url": track.get("purchase_url"),
            "purchase_title": track.get("purchase_title"),
            "license": track.get("license"),
            "sc_genre": track.get("genre"),
            "tag_list": track.get("tag_list"),
            "duration_ms": track.get("duration"),
            "bpm": track.get("bpm"),
            "key": track.get("key_signature"),
            "reposts_count": track.get("reposts_count"),
            # Display-only stash — deliberately NOT the pipeline release_date:
            # a 2005 bootleg uploaded yesterday must survive the 28-day window.
            "release_year": track.get("release_year"),
            "release_month": track.get("release_month"),
            "release_day": track.get("release_day"),
        },
    )


# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------

def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("soundcloud")
    if not cfg.get("enabled", False):
        return []

    targets = [t for t in cfg.get("targets", []) if t.get("tf_tag")]
    if target_genre is not None:
        targets = [t for t in targets if t.get("tf_tag") == target_genre]
    if not targets:
        return []

    downloadable_only = cfg.get("downloadable_only", True)
    lookback_days = cfg.get("lookback_days", 28)
    limit = cfg.get("limit_per_target", 50)
    max_duration_ms = cfg.get("max_duration_minutes", 15) * 60 * 1000
    created_from = (date.today() - timedelta(days=lookback_days)).isoformat()

    session = requests.Session()
    token = _get_access_token(settings, session)  # raises SoundCloudAuthError
    session.headers.update({
        "Authorization": f"OAuth {token}",
        "Accept": "application/json; charset=utf-8",
    })

    all_items: list[SourceItem] = []
    attempted = 0
    completed = 0

    for i, target in enumerate(targets):
        tag = target["tf_tag"]
        if i:
            polite_sleep(1.0)
        attempted += 1

        tag_items: list[SourceItem] = []
        url: str | None = _build_search_url(target, created_from, limit)
        page = 0
        try:
            while url and page < _MAX_PAGES:
                logger.info(f"[soundcloud] {tag}: page {page + 1} — {url}")
                data = _get_json(url, session)
                page += 1
                for track in (data.get("collection") or []):
                    if downloadable_only and track.get("downloadable") is not True:
                        continue
                    # Search results mix single tracks with full DJ sets/mixes —
                    # anything over the duration cap is a set, not a release.
                    duration = track.get("duration")
                    if max_duration_ms and duration and duration > max_duration_ms:
                        continue
                    item = _parse_track(track, tag)
                    if item is None:
                        continue
                    # The API ignores created_at[from] (verified live 2026-07-17),
                    # so the lookback window is enforced here. Undated items pass,
                    # matching the pipeline's release-date-window semantics.
                    if item.release_date is not None and item.release_date < created_from:
                        continue
                    tag_items.append(item)
                url = data.get("next_href")
        except Exception as e:
            logger.warning(f"[soundcloud] {tag}: fetch failed: {e}")
            continue
        completed += 1

        logger.info(f"[soundcloud] {tag}: {len(tag_items)} tracks")
        all_items.extend(tag_items)

    if attempted > 0 and completed == 0:
        raise RuntimeError(f"soundcloud: all {attempted} targets failed")

    logger.info(f"[soundcloud] Total: {len(all_items)} tracks across {completed}/{attempted} targets")
    return all_items
