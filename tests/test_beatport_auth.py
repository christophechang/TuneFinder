import json
import os
import re
from unittest.mock import MagicMock, patch

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


# --- orchestration and flow tests ---


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


def test_client_id_scrape_failure_raises_auth_error(tmp_path):
    # No cache in tmp_path, so get_access_token proceeds past the cache check
    # to the scrape, which fails with a raw requests exception (e.g. a 5xx /
    # Cloudflare-challenge response from the docs page via raise_for_status()).
    # get_access_token must not let that raw exception escape.
    import requests as _rq
    with patch.object(ba, "_scrape_client_id", side_effect=_rq.HTTPError("docs 503")):
        try:
            ba.get_access_token(_settings(data_dir=str(tmp_path)))
            assert False, "expected BeatportAuthError"
        except ba.BeatportAuthError:
            pass


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
