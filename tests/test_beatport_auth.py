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
