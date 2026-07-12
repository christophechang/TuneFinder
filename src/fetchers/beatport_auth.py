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
