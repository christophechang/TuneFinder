"""
Beatport internal-API authentication.

Turns stored credentials into a Bearer access token via the community-standard
flow (login -> PKCE authorize -> token), cached in data/ and refreshed. Uses the
public docs client_id scraped at runtime (not hardcoded — it rotates). Plain
requests, no browser. See docs/superpowers/specs/2026-07-12-beatport-api-migration-design.md.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse

import requests

from src.logger import get_logger
from src.pipeline.storage import atomic_write_json

logger = get_logger(__name__)

_BASE = "https://api.beatport.com/v4"
_DOCS_URL = f"{_BASE}/docs/"
_LOGIN_URL = f"{_BASE}/auth/login/"
_AUTHORIZE_URL = f"{_BASE}/auth/o/authorize/"
_TOKEN_URL = f"{_BASE}/auth/o/token/"
_REDIRECT_URI = "https://api.beatport.com/v4/auth/o/post-message/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_CACHE_FILE = "beatport_token.json"
_EXPIRY_MARGIN_S = 300   # refresh when < 5 min of life remains
_TIMEOUT = 25
_CLIENT_ID_RE = re.compile(r'client[_]?id["\']?\s*[:=]\s*["\']([A-Za-z0-9]{20,})["\']', re.I)


class BeatportAuthError(Exception):
    """Raised when Beatport authentication cannot produce a valid access token."""


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _scrape_client_id(session: requests.Session) -> str:
    r = session.get(_DOCS_URL, timeout=_TIMEOUT)
    r.raise_for_status()
    blobs = [r.text]
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', r.text):
        try:
            jr = session.get(urllib.parse.urljoin(_DOCS_URL, src), timeout=_TIMEOUT)
            blobs.append(jr.text)
        except requests.RequestException:
            continue
    for blob in blobs:
        m = _CLIENT_ID_RE.search(blob)
        if m:
            return m.group(1)
    raise BeatportAuthError("could not scrape client_id from Beatport docs")


def _load_cache(data_dir: str) -> dict | None:
    path = os.path.join(data_dir, _CACHE_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # Structural validation — a malformed cache (e.g. [], {}, or missing
    # access_token) must recover via refresh/login, never crash get_access_token.
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        return None
    return data


def _save_cache(data_dir: str, tok: dict) -> None:
    now = time.time()
    payload = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": now + int(tok.get("expires_in", 0)),
        "obtained_at": now,
    }
    path = os.path.join(data_dir, _CACHE_FILE)
    atomic_write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _login(session: requests.Session, username: str, password: str) -> None:
    r = session.post(_LOGIN_URL, json={"username": username, "password": password}, timeout=_TIMEOUT)
    if r.status_code not in (200, 201, 204):
        raise BeatportAuthError(f"login rejected: HTTP {r.status_code}")


def _authorize(session: requests.Session, client_id: str, challenge: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(16),
    }
    r = session.get(_AUTHORIZE_URL, params=params, allow_redirects=False, timeout=_TIMEOUT)
    location = r.headers.get("location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
    if not code:
        raise BeatportAuthError(f"authorize returned no code (HTTP {r.status_code})")
    return code


def _exchange_token(session: requests.Session, client_id: str, code: str, verifier: str) -> dict:
    r = session.post(_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise BeatportAuthError(f"token exchange failed: HTTP {r.status_code}")
    data = r.json()
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise BeatportAuthError("token response missing access_token")
    return data


def _refresh(session: requests.Session, client_id: str, refresh_token: str) -> dict:
    r = session.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise BeatportAuthError(f"refresh failed: HTTP {r.status_code}")
    data = r.json()
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise BeatportAuthError("token response missing access_token")
    return data


def get_access_token(settings) -> str:
    """Return a valid Bearer access token, or raise BeatportAuthError."""
    username = settings.beatport_username
    password = settings.beatport_password
    if not username or not password:
        raise BeatportAuthError("credentials not set (BEATPORT_USERNAME/BEATPORT_PASSWORD)")

    data_dir = settings.data_dir
    cache = _load_cache(data_dir)
    if cache and (cache.get("expires_at", 0) - time.time()) > _EXPIRY_MARGIN_S:
        return cache["access_token"]

    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json",
                            "Origin": "https://www.beatport.com", "Referer": _DOCS_URL})

    try:
        client_id = _scrape_client_id(session)
    except requests.RequestException as exc:
        raise BeatportAuthError(f"client_id scrape failed: {exc}") from exc

    if cache and cache.get("refresh_token"):
        try:
            tok = _refresh(session, client_id, cache["refresh_token"])
            # OAuth servers often omit refresh_token on refresh — keep the old one
            # so we don't force a full login at the next expiry.
            if not tok.get("refresh_token"):
                tok["refresh_token"] = cache["refresh_token"]
            _save_cache(data_dir, tok)
            return tok["access_token"]
        except (requests.RequestException, BeatportAuthError, ValueError) as exc:
            logger.warning(f"[beatport-auth] refresh failed ({exc}); doing full login")

    try:
        verifier, challenge = _pkce_pair()
        _login(session, username, password)
        code = _authorize(session, client_id, challenge)
        tok = _exchange_token(session, client_id, code, verifier)
    except (requests.RequestException, ValueError) as exc:
        raise BeatportAuthError(f"login flow failed: {exc}") from exc
    _save_cache(data_dir, tok)
    return tok["access_token"]
