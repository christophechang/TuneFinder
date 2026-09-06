"""Tests for the publish-pool API client (src/publisher/client.py): the
Entra client-credentials token and the four ingest calls with retries.

Every test replaces requests.Session with a MagicMock (request/post/get
return scripted responses) and injects `sleep` so backoff is asserted, never
slept for real. The autouse `_no_sockets` fixture below is a hard guard: if
anything in this file ever falls through to a real `requests.Session`, the
test fails immediately instead of hanging on a real network call.
"""
import socket
from unittest.mock import MagicMock, call

import pytest
import requests

from src.publisher import PUBLISHER_VERSION
from src.publisher.client import PoolApiClient, PoolApiError, TokenProvider
from src.publisher.contract import ContractError


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def _guard(*args, **kwargs):
        raise AssertionError("test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _guard)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status_code=200, json_body=None, raises_on_json=False):
    resp = MagicMock()
    resp.status_code = status_code
    if raises_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _token_provider(session=None, clock=None, **overrides):
    kwargs = dict(
        token_url="https://login.example/token",
        client_id="the-client-id",
        client_secret="s3cret-value",
        scope="api://pool/.default",
        session=session or MagicMock(),
    )
    kwargs.update(overrides)
    if clock is not None:
        kwargs["clock"] = clock
    return TokenProvider(**kwargs)


_TOKEN_BODY = {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}


def _make_client(session=None, tokens=None, sleep=None, backoff=None):
    token_session = MagicMock()
    token_session.post.return_value = _resp(200, _TOKEN_BODY)
    tokens = tokens or _token_provider(session=token_session, clock=lambda: 1000.0)
    kwargs = dict(
        base_url="https://tunefinder-api-dev.setfolio.app",
        tokens=tokens,
        session=session or MagicMock(),
        sleep=sleep or MagicMock(),
    )
    if backoff is not None:
        kwargs["backoff"] = backoff
    return PoolApiClient(**kwargs)


# ---------------------------------------------------------------------------
# TokenProvider
# ---------------------------------------------------------------------------


def test_token_posts_client_credentials_and_caches():
    session = MagicMock()
    session.post.return_value = _resp(200, _TOKEN_BODY)
    clock = MagicMock(return_value=1000.0)
    tokens = _token_provider(session=session, clock=clock, client_id="cid-1", scope="scope-1")

    first = tokens.token()
    second = tokens.token()

    assert first == "tok-1"
    assert second == "tok-1"
    assert session.post.call_count == 1  # cached — no second POST

    url, kwargs = session.post.call_args[0], session.post.call_args[1]
    assert url[0] == "https://login.example/token"
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "client_id": "cid-1",
        "client_secret": "s3cret-value",
        "scope": "scope-1",
    }


def test_token_refreshes_after_expiry():
    session = MagicMock()
    session.post.side_effect = [
        _resp(200, {"access_token": "tok-a", "expires_in": 100}),
        _resp(200, {"access_token": "tok-b", "expires_in": 100}),
    ]
    now = [1000.0]
    tokens = _token_provider(session=session, clock=lambda: now[0])

    first = tokens.token()
    now[0] += 100  # past expires_in - 60s margin (40s window)
    second = tokens.token()

    assert first == "tok-a"
    assert second == "tok-b"
    assert session.post.call_count == 2


def test_token_error_does_not_leak_secret_or_description():
    session = MagicMock()
    session.post.return_value = _resp(
        400,
        {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided: s3cret-value",
        },
    )
    tokens = _token_provider(session=session, client_secret="s3cret-value")

    with pytest.raises(PoolApiError) as excinfo:
        tokens.token()

    err = excinfo.value
    assert err.status == 400
    assert err.error == "token"  # fixed category, never the raw Entra error code
    assert err.detail == "invalid_client"  # the body's `error`, never `error_description`
    text = str(err)
    assert "s3cret-value" not in text
    assert "AADSTS7000215" not in text
    assert "error_description" not in text
    assert repr(tokens).find("s3cret-value") == -1


def test_token_transport_error_becomes_pool_api_error():
    """A transport failure fetching the token must not escape `_call` as a bare
    `requests` exception — it shares the call's own retry budget and comes out
    as a `PoolApiError` like any other transport failure."""
    token_session = MagicMock()
    token_session.post.side_effect = requests.ConnectionError("boom")
    tokens = _token_provider(session=token_session, clock=lambda: 1000.0)

    session = MagicMock()  # the API session — must never be reached
    sleep = MagicMock()
    client = _make_client(session=session, tokens=tokens, sleep=sleep)

    with pytest.raises(PoolApiError) as excinfo:
        client.get_config()

    err = excinfo.value
    assert err.status is None
    assert err.error == "transport"
    text = str(err)
    assert "boom" not in text
    assert "://" not in text
    assert "login.example" not in text

    # Retried under the same backoff budget as any other transport failure.
    assert sleep.call_args_list == [call(5), call(15), call(45)]
    assert token_session.post.call_count == 4  # initial attempt + 3 retries
    assert session.request.call_count == 0  # never got past the token


# ---------------------------------------------------------------------------
# PoolApiClient — happy paths
# ---------------------------------------------------------------------------


def test_get_config_validates_shape():
    valid_config = {
        "taxonomy_version": 1,
        "schema_versions": [1],
        "batch_size": 200,
        "sources": {
            "beatport": {"fetch": True, "display": True, "preview": True},
            "bandcamp": {"fetch": False, "display": False, "preview": False},
        },
    }
    session = MagicMock()
    session.request.return_value = _resp(200, valid_config)
    client = _make_client(session=session)

    result = client.get_config()

    assert result == valid_config
    method, url = session.request.call_args[0][0], session.request.call_args[0][1]
    assert method == "GET"
    assert url.endswith("/api/ingest/config")

    # An invalid shape (missing every required key) must not be silently accepted.
    session.request.return_value = _resp(200, {"nonsense": True})
    with pytest.raises(ContractError):
        client.get_config()


def test_post_batch_sends_bearer_and_returns_counts():
    batch_response = {
        "run_id": "2026-09-06T06:00:00Z-a3f9c1",
        "batch_no": 1,
        "upserted": 150,
        "updated": 30,
        "unchanged": 15,
        "obsolete": 5,
        "rejected": [{"key_v2": "x||y", "family": "dnb", "reason": "bad_bpm"}],
        "request_charge": 12.34,
    }
    session = MagicMock()
    session.request.return_value = _resp(200, batch_response)
    client = _make_client(session=session)

    payload = {"run_id": "2026-09-06T06:00:00Z-a3f9c1", "batch_no": 1, "items": []}
    result = client.post_batch(payload)

    assert result == batch_response
    args, kwargs = session.request.call_args
    assert args[0] == "POST"
    assert args[1].endswith("/api/ingest/batch")
    assert kwargs["json"] == payload
    assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_user_agent_carries_publisher_version():
    session = MagicMock()
    session.request.return_value = _resp(200, {})
    client = _make_client(session=session)

    client.post_manifest({"run_id": "x", "per_source": {}, "batches": 1})

    headers = session.request.call_args[1]["headers"]
    assert headers["User-Agent"] == f"tunefinder-publisher/{PUBLISHER_VERSION}"


# ---------------------------------------------------------------------------
# PoolApiClient — retries
# ---------------------------------------------------------------------------


def test_429_backs_off_then_succeeds():
    session = MagicMock()
    session.request.side_effect = [_resp(429, {"error": "throttled"}), _resp(200, {"ok": True})]
    sleep = MagicMock()
    client = _make_client(session=session, sleep=sleep)

    result = client.post_artists({"run_id": "x", "family": "dnb", "weeks": []})

    assert result == {"ok": True}
    assert session.request.call_count == 2
    sleep.assert_called_once_with(5)


def test_503_three_times_then_raises_transport():
    session = MagicMock()
    session.request.return_value = _resp(503, {"error": "unavailable"})
    sleep = MagicMock()
    client = _make_client(session=session, sleep=sleep)

    with pytest.raises(PoolApiError) as excinfo:
        client.post_manifest({"run_id": "x", "per_source": {}, "batches": 1})

    assert session.request.call_count == 4  # initial attempt + 3 retries
    assert sleep.call_args_list == [call(5), call(15), call(45)]
    err = excinfo.value
    assert err.status == 503
    assert err.error == "transport"


# ---------------------------------------------------------------------------
# PoolApiClient — 401 token refresh
# ---------------------------------------------------------------------------


def test_401_once_refreshes_token_and_retries_once():
    token_session = MagicMock()
    token_session.post.side_effect = [
        _resp(200, {"access_token": "tok-old", "expires_in": 3600}),
        _resp(200, {"access_token": "tok-new", "expires_in": 3600}),
    ]
    tokens = _token_provider(session=token_session, clock=lambda: 1000.0)

    session = MagicMock()
    session.request.side_effect = [_resp(401, {"error": "invalid_token"}), _resp(200, {"ok": True})]
    client = _make_client(session=session, tokens=tokens)

    result = client.post_batch({"run_id": "x", "batch_no": 1, "items": []})

    assert result == {"ok": True}
    assert session.request.call_count == 2
    assert token_session.post.call_count == 2  # dropped and re-fetched once
    first_headers = session.request.call_args_list[0][1]["headers"]
    second_headers = session.request.call_args_list[1][1]["headers"]
    assert first_headers["Authorization"] == "Bearer tok-old"
    assert second_headers["Authorization"] == "Bearer tok-new"

    # A second consecutive 401 must not loop forever — it raises. Two token
    # fetches happen (the initial one, then the one after invalidate()); the
    # second 401 is not retried, so no third fetch occurs.
    token_session.post.side_effect = [
        _resp(200, {"access_token": "tok-3", "expires_in": 3600}),
        _resp(200, {"access_token": "tok-4", "expires_in": 3600}),
    ]
    session.request.side_effect = [_resp(401, {"error": "invalid_token"}), _resp(401, {"error": "invalid_token"})]
    tokens2 = _token_provider(session=token_session, clock=lambda: 2000.0)
    client2 = _make_client(session=session, tokens=tokens2)

    with pytest.raises(PoolApiError) as excinfo:
        client2.post_batch({"run_id": "x", "batch_no": 1, "items": []})
    assert excinfo.value.status == 401


# ---------------------------------------------------------------------------
# PoolApiClient — immediate refusals
# ---------------------------------------------------------------------------


def test_409_raises_with_missing_batches():
    session = MagicMock()
    session.request.return_value = _resp(
        409,
        {
            "error": "batches_missing",
            "detail": "batches 3, 4 were never acknowledged",
            "missing_batches": [3, 4],
        },
    )
    client = _make_client(session=session)

    with pytest.raises(PoolApiError) as excinfo:
        client.post_manifest({"run_id": "x", "per_source": {}, "batches": 4})

    err = excinfo.value
    assert err.status == 409
    assert err.error == "batches_missing"
    assert err.missing_batches == [3, 4]
    assert session.request.call_count == 1  # never retried


def test_400_raises_with_reason():
    session = MagicMock()
    session.request.return_value = _resp(400, {"error": "bad_run_id", "detail": "not ISO 8601"})
    client = _make_client(session=session)

    with pytest.raises(PoolApiError) as excinfo:
        client.post_batch({"run_id": "not-a-run-id", "batch_no": 1, "items": []})

    err = excinfo.value
    assert err.status == 400
    assert err.error == "bad_run_id"
    assert err.detail == "not ISO 8601"
    assert err.missing_batches == []


def test_immediate_error_non_json_body_falls_back():
    session = MagicMock()
    session.request.return_value = _resp(404, raises_on_json=True)
    client = _make_client(session=session)

    with pytest.raises(PoolApiError) as excinfo:
        client.post_manifest({"run_id": "x", "per_source": {}, "batches": 1})

    err = excinfo.value
    assert err.status == 404
    assert err.error == "http_404"
    assert err.detail is None


# ---------------------------------------------------------------------------
# PoolApiError.__str__ safety
# ---------------------------------------------------------------------------


def test_error_str_has_no_url():
    session = MagicMock()
    session.request.return_value = _resp(
        404,
        {
            "error": "unknown_run",
            "detail": "no such run",
            "internal_trace": "https://tunefinder-api-dev.setfolio.app/internal/trace/abc?secret=xyz",
        },
    )
    client = _make_client(session=session)

    with pytest.raises(PoolApiError) as excinfo:
        client.post_manifest({"run_id": "ghost", "per_source": {}, "batches": 1})

    text = str(excinfo.value)
    assert "https://" not in text
    assert "tunefinder-api-dev.setfolio.app" not in text
    assert "/api/ingest/manifest" not in text
    assert "secret=xyz" not in text
