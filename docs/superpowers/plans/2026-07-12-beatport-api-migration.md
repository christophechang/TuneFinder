# Beatport API Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Cloudflare-blocked Beatport HTML scraper with the internal v4 API, preserving the existing `SourceItem` contract and adding a musical-`key` enrichment.

**Architecture:** A new `beatport_auth.py` turns stored credentials into a Bearer token (login → PKCE authorize → token, cached in `data/` and refreshed). `beatport.py` is rewritten to pull `/catalog/genres/{id}/top/100/` with that token and parse to the same `SourceItem` shape. All network calls sit behind small helpers so tests mock them.

**Tech Stack:** Python 3.13, `requests`, stdlib PKCE (`hashlib`/`base64`/`secrets`), `pytest` + `unittest.mock`.

**Reference:** Spec `docs/superpowers/specs/2026-07-12-beatport-api-migration-design.md`. Proven auth flow: scratchpad `bp_auth_poc.py`. Pattern to mirror: `src/fetchers/volumo.py` + `tests/test_volumo.py`.

## Global Constraints

- **No new runtime dependencies.** `requests` (already present) + Python stdlib only. Do not add anything to `requirements.txt`.
- **Preserve the `SourceItem` output contract.** `source="beatport"`, plus `artist, title, link, label, release_date, release_name, genre_tags`, and `raw_metadata` keys `beatport_id, bpm, chart_position` (now also `key, mix_name, isrc`).
- **Keep `_SLUG_TO_TAGS` verbatim** from the current `src/fetchers/beatport.py` (genre-slug → internal-tag mapping, incl. merged feeds). Genre IDs in `config/settings.yaml` are already API-valid — do not change them.
- **Conventional commits; NO `Co-Authored-By` trailer.**
- **No hardcoded secrets/IDs.** The Beatport `client_id` is scraped at runtime; credentials come from `.env`.
- **Graceful degradation preserved:** the run never crashes; a broken Beatport surfaces as a source-health *error* (raise), not a silent `[]`.
- **Validation commands:** `./venv/bin/python -m tunefinder check-config`; `./venv/bin/pytest tests/ -v`.

---

### Task 1: Credentials config

**Files:**
- Modify: `src/config.py` (add two `Settings` properties + register optional env vars)
- Modify: `.env.example` (add the two vars)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.beatport_username -> str`, `Settings.beatport_password -> str` (empty string when unset).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_beatport_credentials_from_env(monkeypatch):
    from src.config import Settings
    monkeypatch.setenv("BEATPORT_USERNAME", "dj_test")
    monkeypatch.setenv("BEATPORT_PASSWORD", "s3cret")
    s = Settings({})
    assert s.beatport_username == "dj_test"
    assert s.beatport_password == "s3cret"


def test_beatport_credentials_default_empty(monkeypatch):
    from src.config import Settings
    monkeypatch.delenv("BEATPORT_USERNAME", raising=False)
    monkeypatch.delenv("BEATPORT_PASSWORD", raising=False)
    s = Settings({})
    assert s.beatport_username == ""
    assert s.beatport_password == ""


def test_validate_warns_when_beatport_enabled_without_creds(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    monkeypatch.delenv("BEATPORT_USERNAME", raising=False)
    monkeypatch.delenv("BEATPORT_PASSWORD", raising=False)
    s = Settings({"sources": {"beatport": {"enabled": True}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert any("Beatport is enabled" in r.message for r in caplog.records)


def test_validate_silent_when_beatport_disabled(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    s = Settings({"sources": {"beatport": {"enabled": False}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert not any("Beatport is enabled" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_config.py::test_beatport_credentials_from_env -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'beatport_username'`

- [ ] **Step 3: Implement**

In `src/config.py`, add two properties to the `Settings` class (near the Discord block):

```python
    @property
    def beatport_username(self) -> str:
        return os.getenv("BEATPORT_USERNAME", "")

    @property
    def beatport_password(self) -> str:
        return os.getenv("BEATPORT_PASSWORD", "")
```

Do **not** add these to `_OPTIONAL_ENV_VARS` — that generic list warns "configured provider will be skipped" (once per var), which misrepresents the behaviour (an *enabled* Beatport reports a source *failure*; it isn't silently skipped). Instead add one Beatport-specific check at the end of `Settings.validate()`, immediately before the final `logger.info(...)` line:

```python
        beatport = self._data.get("sources", {}).get("beatport", {})
        if beatport.get("enabled") and not (self.beatport_username and self.beatport_password):
            logger.warning(
                "[config] Beatport is enabled but BEATPORT_USERNAME/BEATPORT_PASSWORD "
                "are not both set — the source will report a failure until they are set."
            )
```

In `.env.example`, add:

```bash
BEATPORT_USERNAME=       # Beatport login — required to enable the Beatport source (unofficial API, personal use)
BEATPORT_PASSWORD=
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py .env.example tests/test_config.py
git commit -m "feat(beatport): add BEATPORT_USERNAME/PASSWORD config"
```

---

### Task 2: Auth module — helpers & token cache

**Files:**
- Create: `src/fetchers/beatport_auth.py`
- Test: `tests/test_beatport_auth.py`

**Interfaces:**
- Produces: `BeatportAuthError(Exception)`; `_pkce_pair() -> tuple[str, str]`; `_scrape_client_id(session) -> str`; `_load_cache(data_dir) -> dict | None`; `_save_cache(data_dir, tok: dict) -> None` (writes `{access_token, refresh_token, expires_at, obtained_at}`, mode `0600`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_beatport_auth.py`:

```python
import json
import os
import re
from unittest.mock import MagicMock

from src.fetchers import beatport_auth as ba


def test_pkce_pair_shapes():
    verifier, challenge = ba._pkce_pair()
    assert 40 <= len(verifier) <= 200
    assert re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", challenge)
    assert verifier != challenge  # challenge is S256(verifier)


def test_scrape_client_id_from_js():
    session = MagicMock()
    docs = MagicMock(text='<script src="/static/app.js"></script>')
    docs.raise_for_status.return_value = None
    js = MagicMock(text='var x={clientId:"0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"};')
    session.get.side_effect = [docs, js]
    assert ba._scrape_client_id(session) == "0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"


def test_scrape_client_id_missing_raises():
    session = MagicMock()
    docs = MagicMock(text="<html>no scripts</html>")
    docs.raise_for_status.return_value = None
    session.get.side_effect = [docs]
    try:
        ba._scrape_client_id(session)
        assert False, "expected BeatportAuthError"
    except ba.BeatportAuthError:
        pass


def test_cache_roundtrip_and_mode(tmp_path):
    ba._save_cache(str(tmp_path), {"access_token": "A", "refresh_token": "R", "expires_in": 36000})
    cache = ba._load_cache(str(tmp_path))
    assert cache["access_token"] == "A"
    assert cache["refresh_token"] == "R"
    assert cache["expires_at"] > cache["obtained_at"]
    mode = os.stat(os.path.join(str(tmp_path), "beatport_token.json")).st_mode & 0o777
    assert mode == 0o600


def test_load_cache_absent_returns_none(tmp_path):
    assert ba._load_cache(str(tmp_path)) is None


def test_load_cache_malformed_returns_none(tmp_path):
    path = os.path.join(str(tmp_path), "beatport_token.json")
    for bad in ("[]", "{}", '{"refresh_token": "R"}', "not json at all"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        assert ba._load_cache(str(tmp_path)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_beatport_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.fetchers.beatport_auth'`

- [ ] **Step 3: Implement the module foundations**

Create `src/fetchers/beatport_auth.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_beatport_auth.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/beatport_auth.py tests/test_beatport_auth.py
git commit -m "feat(beatport): auth helpers (PKCE, client_id scrape, token cache)"
```

---

### Task 3: Auth module — network flow & orchestration

**Files:**
- Modify: `src/fetchers/beatport_auth.py`
- Test: `tests/test_beatport_auth.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces: `_login(session, username, password)`; `_authorize(session, client_id, challenge) -> str` (auth code); `_exchange_token(session, client_id, code, verifier) -> dict`; `_refresh(session, client_id, refresh_token) -> dict`; **`get_access_token(settings) -> str`** (raises `BeatportAuthError`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_beatport_auth.py`:

```python
from unittest.mock import patch


def _settings(user="dj", pw="pw", data_dir="/tmp/bp"):
    s = MagicMock()
    s.beatport_username = user
    s.beatport_password = pw
    s.data_dir = data_dir
    return s


def test_missing_creds_raises():
    try:
        ba.get_access_token(_settings(user="", pw=""))
        assert False, "expected BeatportAuthError"
    except ba.BeatportAuthError:
        pass


def test_valid_cache_returns_without_network(tmp_path):
    ba._save_cache(str(tmp_path), {"access_token": "CACHED", "refresh_token": "R", "expires_in": 36000})
    with patch.object(ba.requests, "Session") as sess:
        token = ba.get_access_token(_settings(data_dir=str(tmp_path)))
    assert token == "CACHED"
    sess.assert_not_called()  # no network when cache is fresh


def test_refresh_path_sends_client_id(tmp_path):
    # expired access token, valid refresh token present
    ba._save_cache(str(tmp_path), {"access_token": "OLD", "refresh_token": "RT", "expires_in": 0})
    with patch.object(ba, "_scrape_client_id", return_value="CID") as scrape, \
         patch.object(ba, "_refresh", return_value={"access_token": "NEW", "refresh_token": "RT2", "expires_in": 36000}) as refresh:
        token = ba.get_access_token(_settings(data_dir=str(tmp_path)))
    assert token == "NEW"
    scrape.assert_called_once()
    # client_id passed positionally to _refresh(session, client_id, refresh_token)
    assert refresh.call_args[0][1] == "CID"
    assert refresh.call_args[0][2] == "RT"


def test_refresh_failure_falls_back_to_login(tmp_path):
    ba._save_cache(str(tmp_path), {"access_token": "OLD", "refresh_token": "RT", "expires_in": 0})
    with patch.object(ba, "_scrape_client_id", return_value="CID"), \
         patch.object(ba, "_refresh", side_effect=ba.BeatportAuthError("refresh dead")), \
         patch.object(ba, "_login") as login, \
         patch.object(ba, "_authorize", return_value="CODE"), \
         patch.object(ba, "_exchange_token", return_value={"access_token": "FRESH", "refresh_token": "R3", "expires_in": 36000}):
        token = ba.get_access_token(_settings(data_dir=str(tmp_path)))
    assert token == "FRESH"
    login.assert_called_once()


def test_login_failure_raises(tmp_path):
    with patch.object(ba, "_scrape_client_id", return_value="CID"), \
         patch.object(ba, "_login", side_effect=ba.BeatportAuthError("bad creds")):
        try:
            ba.get_access_token(_settings(data_dir=str(tmp_path)))
            assert False, "expected BeatportAuthError"
        except ba.BeatportAuthError:
            pass


def test_refresh_preserves_refresh_token_when_omitted(tmp_path):
    ba._save_cache(str(tmp_path), {"access_token": "OLD", "refresh_token": "KEEPME", "expires_in": 0})
    with patch.object(ba, "_scrape_client_id", return_value="CID"), \
         patch.object(ba, "_refresh", return_value={"access_token": "NEW", "expires_in": 36000}):  # no refresh_token
        token = ba.get_access_token(_settings(data_dir=str(tmp_path)))
    assert token == "NEW"
    assert ba._load_cache(str(tmp_path))["refresh_token"] == "KEEPME"  # old one preserved


# --- helper-contract tests (verify the POC-critical request details directly) ---

def test_authorize_builds_pkce_params_and_extracts_code():
    session = MagicMock()
    session.get.return_value = MagicMock(
        status_code=302,
        headers={"location": "https://api.beatport.com/v4/auth/o/post-message/?code=ABC&state=x"},
    )
    assert ba._authorize(session, "CID", "CHALLENGE") == "ABC"
    kw = session.get.call_args.kwargs
    p = kw["params"]
    assert p["client_id"] == "CID"
    assert p["response_type"] == "code"
    assert p["redirect_uri"] == "https://api.beatport.com/v4/auth/o/post-message/"
    assert p["code_challenge"] == "CHALLENGE"
    assert p["code_challenge_method"] == "S256"
    assert "scope" not in p                    # no scope, per POC
    assert kw["allow_redirects"] is False       # must capture the redirect, not follow it


def test_authorize_no_code_raises():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=400, headers={"location": ""})
    try:
        ba._authorize(session, "CID", "CH")
        assert False
    except ba.BeatportAuthError:
        pass


def test_exchange_token_posts_grant_and_returns_json():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "A"})
    assert ba._exchange_token(session, "CID", "CODE", "VERIFIER") == {"access_token": "A"}
    d = session.post.call_args.kwargs["data"]
    assert d["grant_type"] == "authorization_code"
    assert d["client_id"] == "CID"
    assert d["code"] == "CODE"
    assert d["code_verifier"] == "VERIFIER"


def test_exchange_token_non_200_raises():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=400)
    try:
        ba._exchange_token(session, "CID", "CODE", "V")
        assert False
    except ba.BeatportAuthError:
        pass


def test_refresh_posts_client_id_and_token():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "N"})
    assert ba._refresh(session, "CID", "RT") == {"access_token": "N"}
    d = session.post.call_args.kwargs["data"]
    assert d["grant_type"] == "refresh_token"
    assert d["client_id"] == "CID"             # the F1/spec requirement: client_id sent on refresh
    assert d["refresh_token"] == "RT"


def test_refresh_non_200_raises():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=401)
    try:
        ba._refresh(session, "CID", "RT")
        assert False
    except ba.BeatportAuthError:
        pass


def test_login_non_200_raises():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=403)
    try:
        ba._login(session, "u", "p")
        assert False
    except ba.BeatportAuthError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_beatport_auth.py -k "creds or cache or refresh or login" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'get_access_token'`

- [ ] **Step 3: Implement the flow + orchestration**

Append to `src/fetchers/beatport_auth.py`:

```python
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
    return r.json()


def _refresh(session: requests.Session, client_id: str, refresh_token: str) -> dict:
    r = session.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise BeatportAuthError(f"refresh failed: HTTP {r.status_code}")
    return r.json()


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

    client_id = _scrape_client_id(session)

    if cache and cache.get("refresh_token"):
        try:
            tok = _refresh(session, client_id, cache["refresh_token"])
            # OAuth servers often omit refresh_token on refresh — keep the old one
            # so we don't force a full login at the next expiry.
            if not tok.get("refresh_token"):
                tok["refresh_token"] = cache["refresh_token"]
            _save_cache(data_dir, tok)
            return tok["access_token"]
        except (requests.RequestException, BeatportAuthError) as exc:
            logger.warning(f"[beatport-auth] refresh failed ({exc}); doing full login")

    try:
        verifier, challenge = _pkce_pair()
        _login(session, username, password)
        code = _authorize(session, client_id, challenge)
        tok = _exchange_token(session, client_id, code, verifier)
    except requests.RequestException as exc:
        raise BeatportAuthError(f"login flow failed: {exc}") from exc
    _save_cache(data_dir, tok)
    return tok["access_token"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_beatport_auth.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/beatport_auth.py tests/test_beatport_auth.py
git commit -m "feat(beatport): login/authorize/refresh flow + get_access_token"
```

---

### Task 4: Fetcher rewrite

**Files:**
- Modify (rewrite internals): `src/fetchers/beatport.py`
- Test: `tests/test_beatport.py` (new)

**Interfaces:**
- Consumes: `beatport_auth.get_access_token(settings) -> str`.
- Produces: `fetch(settings, target_genre=None) -> list[SourceItem]`; `_get_json(url, session) -> dict`; `_parse_track(raw, fallback_tags, chart_position=None) -> SourceItem | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_beatport.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers import beatport


@pytest.fixture(autouse=True)
def _no_sleep():
    """Stop the fetcher's polite_sleep(2.0) from actually sleeping during tests."""
    with patch("src.fetchers.beatport.polite_sleep"):
        yield


def _settings(enabled=True, genres=None):
    s = MagicMock()
    if genres is None:
        genres = [{"name": "dnb", "slug": "drum-bass", "id": 1}]
    s.get_source_config.return_value = {"enabled": enabled, "genres": genres}
    return s


def _track(track_id=29206235, name="Lock It", slug="lock-it", bpm=87,
           mix="Primate Remix", key="Eb Major", label="Wobbles & Waffles",
           genre_slug="drum-bass", publish_date="2026-06-26", isrc="GB1"):
    return {
        "id": track_id, "name": name, "slug": slug, "bpm": bpm, "mix_name": mix,
        "isrc": isrc, "publish_date": publish_date,
        "artists": [{"name": "Flowidus"}, {"name": "Loboski"}],
        "genre": {"slug": genre_slug},
        "key": {"name": key},
        "release": {"name": "Lock It (Primate Remix)", "label": {"name": label}},
    }


def _page(results, has_next=False):
    return {"count": len(results), "next": ("x" if has_next else None), "results": results}


def test_fetch_disabled_returns_empty():
    assert beatport.fetch(_settings(enabled=False)) == []


def test_fetch_parses_source_item():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([_track()])):
        items = beatport.fetch(_settings())
    assert len(items) == 1
    it = items[0]
    assert it.source == "beatport"
    assert it.artist == "Flowidus, Loboski"
    assert it.title == "Lock It"
    assert it.label == "Wobbles & Waffles"          # from release.label.name
    assert it.link == "https://www.beatport.com/track/lock-it/29206235"
    assert it.release_date == "2026-06-26"
    assert it.release_name == "Lock It (Primate Remix)"
    assert "dnb" in it.genre_tags
    md = it.raw_metadata
    assert md["beatport_id"] == 29206235
    assert md["bpm"] == 87
    assert md["chart_position"] == 1
    assert md["key"] == "Eb Major"                  # harmonic enrichment
    assert md["mix_name"] == "Primate Remix"
    assert md["isrc"] == "GB1"


def test_chart_position_is_rank_order():
    tracks = [_track(track_id=i, name=f"T{i}", slug=f"t{i}") for i in range(1, 4)]
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page(tracks)):
        items = beatport.fetch(_settings())
    assert [it.raw_metadata["chart_position"] for it in items] == [1, 2, 3]


def test_target_genre_filters():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([])) as gj:
        beatport.fetch(settings, target_genre="dnb")
    # only the dnb genre (id 1) is requested
    assert all("/genres/1/" in call.args[0] for call in gj.call_args_list)


def test_target_genre_no_match_returns_empty():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token") as gt, \
         patch("src.fetchers.beatport._get_json") as gj:
        items = beatport.fetch(_settings(), target_genre="funk-soul-jazz")
    assert items == []
    gt.assert_not_called()
    gj.assert_not_called()


def test_partial_results_when_one_genre_fails():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=[_page([_track()]), Exception("boom")]):
        items = beatport.fetch(settings)
    assert len(items) == 1  # genre 1 succeeded, genre 5 failed


def test_all_genres_fail_raises():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=Exception("boom")):
        try:
            beatport.fetch(settings)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


def test_auth_error_propagates():
    from src.fetchers.beatport_auth import BeatportAuthError
    with patch("src.fetchers.beatport.beatport_auth.get_access_token",
               side_effect=BeatportAuthError("no creds")):
        try:
            beatport.fetch(_settings())
            assert False, "expected BeatportAuthError"
        except BeatportAuthError:
            pass


def test_pagination_follows_next_and_stops_at_100():
    page1 = _page([_track(track_id=i, slug=f"t{i}") for i in range(1, 61)], has_next=True)
    page2 = _page([_track(track_id=i, slug=f"u{i}") for i in range(61, 121)], has_next=True)
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=[page1, page2]) as gj:
        items = beatport.fetch(_settings())
    assert len(items) == 100                                  # capped at chart size
    assert [it.raw_metadata["chart_position"] for it in items[:3]] == [1, 2, 3]  # order kept
    assert gj.call_args_list[1].args[0] == "x"               # 2nd call used page1's `next` (="x"), not a re-request


def test_merged_feed_uses_per_track_genre_slug():
    """breaks/uk-bass is one combined feed; tags come from each track's own slug."""
    settings = _settings(genres=[{"name": "breaks-uk-bass", "slug": "breaks-breakbeat-uk-bass", "id": 9}])
    track = _track(track_id=1, slug="b", genre_slug="breaks-breakbeat-uk-bass")
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([track])):
        items = beatport.fetch(settings)
    assert items[0].genre_tags == ["breaks", "uk-bass"]      # per-track slug → both tags, not the feed name
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_beatport.py -v`
Expected: FAIL — the current `fetch` scrapes HTML; `_get_json` doesn't exist yet, several assertions fail.

- [ ] **Step 3: Rewrite `src/fetchers/beatport.py`**

Replace the file body (keep the `_SLUG_TO_TAGS` dict **verbatim** from the current file) with:

```python
"""
Beatport source fetcher — genre top-100 chart via the internal v4 API.

Replaces the former __NEXT_DATA__ HTML scrape (Cloudflare-blocked since 2026-07).
Auth (login -> PKCE -> Bearer token, cached/refreshed) lives in beatport_auth.
Endpoint: GET /v4/catalog/genres/{id}/top/100/.

Key signals: chart_position (rank in the genre top-100), bpm, key (harmonic mixing).
"""
import requests

from src.fetchers import beatport_auth
from src.fetchers.common import polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://api.beatport.com/v4"
_TRACK_URL = "https://www.beatport.com/track/{slug}/{id}"
_CHART_SIZE = 100
_PER_PAGE = 100
_TIMEOUT = 25

# Maps Beatport genre slugs to internal genre tags. KEEP VERBATIM from the prior
# implementation (merged feeds + house sub-genres roll-ups).
_SLUG_TO_TAGS: dict[str, list[str]] = {
    "drum-bass": ["dnb"],
    "breaks-breakbeat-uk-bass": ["breaks", "uk-bass"],
    "house": ["house"],
    "melodic-house-techno": ["house"],
    "minimal-deep-tech": ["house"],
    "deep-house": ["house"],
    "tech-house": ["house"],
    "uk-garage-bassline": ["ukg"],
    "electronica": ["electronica"],
    "downtempo": ["downtempo"],
    "techno-raw-deep-hypnotic": ["techno"],
    "hip-hop": ["hip-hop"],
    "rb": ["funk-soul-jazz"],
}


def _get_json(url: str, session: requests.Session) -> dict:
    resp = session.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _parse_track(raw: dict, fallback_tags: list[str], chart_position: int | None = None) -> SourceItem | None:
    title = (raw.get("name") or "").strip()
    if not title:
        return None
    artists = raw.get("artists") or []
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    if not artist:
        return None

    track_id = raw.get("id", "")
    slug = raw.get("slug", "")
    link = _TRACK_URL.format(slug=slug, id=track_id) if slug and track_id else ""

    release = raw.get("release") or {}
    release_name = release.get("name") or None
    label = (release.get("label") or {}).get("name") or None
    release_date = raw.get("publish_date") or raw.get("new_release_date") or ""

    genre_slug = (raw.get("genre") or {}).get("slug", "")
    genre_tags = _SLUG_TO_TAGS.get(genre_slug) or fallback_tags
    key = (raw.get("key") or {}).get("name") or None

    return SourceItem(
        source="beatport",
        artist=artist,
        title=title,
        link=link,
        label=label,
        release_date=release_date,
        release_name=release_name,
        genre_tags=genre_tags,
        raw_metadata={
            "beatport_id": track_id,
            "bpm": raw.get("bpm"),
            "chart_position": chart_position,
            "key": key,
            "mix_name": raw.get("mix_name"),
            "isrc": raw.get("isrc"),
        },
    )


def _fetch_genre_top(session: requests.Session, genre_id) -> list[dict]:
    """Return up to 100 raw track dicts in rank order for a genre's top-100 chart.

    Follows the API's own `next` URL (an absolute URL, per DRF pagination) rather
    than hand-building a `page=N` param. This is robust to whatever scheme the
    endpoint uses (page number, cursor, or `per_page` honoured in one shot) and
    cannot re-request the same page into duplicates. Stops at `next=None`, an
    empty page, or 100 tracks.
    """
    tracks: list[dict] = []
    url = f"{_BASE}/catalog/genres/{genre_id}/top/{_CHART_SIZE}/?per_page={_PER_PAGE}"
    while url and len(tracks) < _CHART_SIZE:
        data = _get_json(url, session)
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            break
        tracks.extend(results)
        url = data.get("next") if isinstance(data, dict) else None
    return tracks[:_CHART_SIZE]


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("beatport")
    if not cfg.get("enabled", False):
        return []

    genres: list[dict] = cfg.get("genres", [])
    if target_genre is not None:
        genres = [
            g for g in genres
            if target_genre in (_SLUG_TO_TAGS.get(g.get("slug", "")) or [g.get("name", "")])
        ]
    if not genres:
        return []

    token = beatport_auth.get_access_token(settings)  # raises BeatportAuthError
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    all_items: list[SourceItem] = []
    attempted = 0
    completed = 0

    for genre in genres:
        slug = genre.get("slug", "")
        genre_id = genre.get("id", "")
        name = genre.get("name", slug)
        fallback_tags = _SLUG_TO_TAGS.get(slug) or [name]
        if not slug or not genre_id:
            logger.warning(f"[beatport] Skipping genre with missing slug/id: {genre}")
            continue

        attempted += 1
        try:
            raw_tracks = _fetch_genre_top(session, genre_id)
        except Exception as e:
            logger.warning(f"[beatport] {name}: fetch failed: {e}")
            polite_sleep(2.0)
            continue
        completed += 1

        genre_items = []
        for pos, raw in enumerate(raw_tracks, start=1):
            item = _parse_track(raw, fallback_tags, chart_position=pos)
            if item:
                genre_items.append(item)
        logger.info(f"[beatport] {name}: {len(genre_items)} tracks")
        all_items.extend(genre_items)
        polite_sleep(2.0)

    if attempted > 0 and completed == 0:
        raise RuntimeError(f"beatport: all {attempted} genres failed to fetch")

    logger.info(f"[beatport] Total: {len(all_items)} items across {completed}/{attempted} genres")
    return all_items
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_beatport.py -v`
Expected: PASS (all tests). Then full suite: `./venv/bin/pytest tests/ -v` — no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/beatport.py tests/test_beatport.py
git commit -m "feat(beatport): fetch top-100 charts via v4 API (was __NEXT_DATA__ scrape)"
```

---

### Task 5: Enable source + documentation

**Files:**
- Modify: `config/settings.yaml`
- Modify: `README.md`

**Interfaces:** none (config + docs only).

- [ ] **Step 1: Enable the source and drop the dead pattern**

In `config/settings.yaml`, under `sources.beatport`:
- Set `enabled: true`.
- Delete the `chart_pattern: "https://www.beatport.com/genre/{slug}/{id}/top-100"` line.
- Replace the disabled comment on the `enabled` line with: `# v4 API (internal) — requires BEATPORT_USERNAME/PASSWORD in .env`.
- Leave the `genres:` list unchanged.

- [ ] **Step 2: Validate config loads**

Run: `./venv/bin/python -m tunefinder check-config`
Expected: exits 0; no error about the beatport source.

- [ ] **Step 3: Update `README.md` (all six Beatport touchpoints)**

1. **Sources table** row — change `Genre top-100 chart (\`__NEXT_DATA__\` JSON)` / `blocked (Cloudflare…)` to `Genre top-100 chart (v4 API)` / `active`.
2. **Fetch overview** (the "scrapes new releases from Beatport, Bandcamp…" line) — reword so Beatport is "via the v4 API" rather than scraped.
3. **Environment variables** block (near `VOLUMO_API_KEY=`) — add:
   ```
   BEATPORT_USERNAME=       # Beatport login — enables the Beatport source (unofficial API, personal use)
   BEATPORT_PASSWORD=
   ```
4. **Architecture / file-tree** entry `beatport.py # Beatport genre top-100 chart (__NEXT_DATA__)` → `# Beatport genre top-100 chart (v4 API)`; add a line for `beatport_auth.py # Beatport OAuth (login + PKCE + token cache)`.
5. **mix-prep BPM/key note** — the parenthetical "(… Beatport: `bpm`; …)" becomes "Beatport: `bpm` + `key`".
6. **breaks/uk-bass combined-feed note** — "Per-track genre slugs from the page data are used to split them" → "…from the API…".

- [ ] **Step 4: Verify no stale references remain**

Run: `grep -niE "beatport.*__NEXT_DATA__|__NEXT_DATA__.*beatport|beatport.*Cloudflare|beatport.*scrape" README.md`
Expected: no output (or only clearly-unrelated historical notes).

- [ ] **Step 5: Commit**

```bash
git add config/settings.yaml README.md
git commit -m "feat(beatport): re-enable source + update docs for v4 API"
```

---

## Post-plan verification (run once, after all tasks)

- [ ] `./venv/bin/pytest tests/ -v` — full suite green.
- [ ] Add real `BEATPORT_USERNAME`/`BEATPORT_PASSWORD` to `.env`.
- [ ] `./venv/bin/python -m tunefinder check-config` — passes.
- [ ] Live `--dry-run` of the pipeline (or a targeted `fetch`) — confirm Beatport health `count > 0`, tracks carry `key`, and `data/beatport_token.json` is written (mode `0600`).
- [ ] Confirm a second run reuses the cached token (no re-login) and that removing the cache triggers a fresh login.

## Notes for the implementer

- **Pagination:** `_fetch_genre_top` follows the API's own `next` URL, so it's correct regardless of the underlying scheme (page number, cursor, or `per_page` honoured in one shot) and cannot loop the same page into duplicates. Confirm during the live dry-run that a genre yields ~100 tracks and that `next` terminates cleanly.
- **`redirect_uri` / no-scope** were POC-verified 2026-07-12; if `_authorize` ever returns no code after a `200` login, re-scrape flow assumptions against `bp_auth_poc.py`.
- Do **not** commit `.env` or `data/beatport_token.json` (both already gitignored).
