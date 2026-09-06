"""The thin impure layer of publish-pool: an Entra client-credentials token
and the four calls the publisher makes to TuneFinder's multi-tenant API
(tools/publish-pool-contract/README.md, CONTRACTS.md §2, §3, §8).

Everything here is deliberately dumb — no batching, no payload shaping, no
retention policy. It knows how to get a bearer token, how to retry a
transient failure, how to turn a refusal body into `PoolApiError`, and
nothing about *what* it is posting.

Secrecy: the client secret lives on `TokenProvider` only, passed to the token
endpoint's own form body and nowhere else. Neither `TokenProvider.__repr__`
nor `PoolApiError.__str__` (nor any log line here) ever includes the secret,
the bearer token, a url, or a request/response body beyond `error`/`detail`.
"""
import time

import requests

from src.logger import get_logger
from src.publisher import PUBLISHER_VERSION
from src.publisher.contract import validate

logger = get_logger(__name__)

_TOKEN_TIMEOUT_S = 30
_TOKEN_EXPIRY_MARGIN_S = 60

_ROUTES = {
    "config": "/api/ingest/config",
    "batch": "/api/ingest/batch",
    "manifest": "/api/ingest/manifest",
    "artists": "/api/ingest/artists",
}

# 429/502/503/504 and a connection error or timeout share one retry budget:
# sleep backoff[i] then retry, three retries maximum (four attempts total).
_RETRYABLE_STATUSES = (429, 502, 503, 504)
# Refused immediately, with the parsed IngestError body — never retried.
_IMMEDIATE_ERROR_STATUSES = (400, 403, 404, 409)


class PoolApiError(RuntimeError):
    """A refusal or transport failure from the ingest API.

    `status` is the HTTP status (None for a transport failure with no
    response at all). `error`/`detail`/`missing_batches` mirror the API's
    `IngestError` body (README "Rejection reasons"); `error="transport"` is
    this client's own classification for connection/timeout/retry-exhausted
    failures, which never carry a body.

    str()/repr() never include a url or any part of a request/response body
    beyond `error`/`detail` — this exception is always safe to log verbatim.
    """

    def __init__(
        self,
        status: int | None,
        error: str | None,
        detail: str | None = None,
        missing_batches: list[int] | None = None,
    ):
        self.status = status
        self.error = error
        self.detail = detail
        self.missing_batches = missing_batches if missing_batches is not None else []
        super().__init__(self._message())

    def _message(self) -> str:
        bits = [f"status={self.status}", f"error={self.error}"]
        if self.detail:
            bits.append(f"detail={self.detail}")
        return " ".join(bits)

    def __str__(self) -> str:
        return self._message()


class TokenProvider:
    """An Entra client-credentials token, cached until shortly before expiry.

    `token()` posts `grant_type=client_credentials`, `client_id`,
    `client_secret`, `scope` as a form to `token_url`, and caches the
    resulting `access_token` until `expires_in - 60s` from the time of the
    response (`clock`, injectable, default `time.time`). A non-200 response
    raises `PoolApiError(status, "token", <error code>)` — never the
    `error_description` (which can echo the secret back verbatim on some
    Entra failures) and never the secret itself.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str,
        session: requests.Session | None = None,
        clock=time.time,
    ):
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._session = session or requests.Session()
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    def __repr__(self) -> str:
        return f"TokenProvider(token_url={self._token_url!r}, client_id={self._client_id!r})"

    def token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._expires_at:
            return self._token
        self._refresh()
        return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next token() call fetches a fresh one."""
        self._token = None
        self._expires_at = 0.0

    def _refresh(self) -> None:
        resp = self._session.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scope,
            },
            timeout=_TOKEN_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise PoolApiError(resp.status_code, "token", _error_code(resp))
        body = resp.json()
        now = self._clock()
        self._token = body["access_token"]
        self._expires_at = now + body.get("expires_in", 0) - _TOKEN_EXPIRY_MARGIN_S


def _error_code(resp) -> str | None:
    """The body's `error` field, never `error_description` — see TokenProvider."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


class PoolApiClient:
    """The four ingest calls (README "The live endpoints"), each carrying a
    bearer token and retrying transient failures the same way.
    """

    def __init__(
        self,
        base_url: str,
        tokens: TokenProvider,
        session: requests.Session | None = None,
        timeout: int = 120,
        backoff: tuple[float, ...] = (5, 15, 45),
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.session = session or requests.Session()
        self.timeout = timeout
        self.backoff = backoff
        self.sleep = sleep

    def get_config(self) -> dict:
        """GET /api/ingest/config, validated against the ingest-config schema."""
        body = self._call("GET", "config")
        validate("ingest-config", body)
        return body

    def post_batch(self, payload: dict) -> dict:
        body = self._call("POST", "batch", json_body=payload)
        logger.info(
            "[publisher] batch run_id=%s batch_no=%s upserted=%s updated=%s "
            "unchanged=%s obsolete=%s rejected=%s request_charge=%s",
            body.get("run_id"),
            body.get("batch_no"),
            body.get("upserted"),
            body.get("updated"),
            body.get("unchanged"),
            body.get("obsolete"),
            len(body.get("rejected") or []),
            body.get("request_charge"),
        )
        return body

    def post_manifest(self, payload: dict) -> dict:
        return self._call("POST", "manifest", json_body=payload)

    def post_artists(self, payload: dict) -> dict:
        return self._call("POST", "artists", json_body=payload)

    def _call(self, method: str, route: str, json_body: dict | None = None) -> dict:
        path = _ROUTES[route]
        url = f"{self.base_url}{path}"
        attempts = 0
        retried_401 = False
        while True:
            start = time.time()
            try:
                headers = {
                    "Authorization": f"Bearer {self.tokens.token()}",
                    "Content-Type": "application/json",
                    "User-Agent": f"tunefinder-publisher/{PUBLISHER_VERSION}",
                }
                resp = self.session.request(
                    method, url, json=json_body, headers=headers, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.warning(
                    "[publisher] %s %s -- transport error %s (%dms)",
                    method, path, type(exc).__name__, elapsed_ms,
                )
                if attempts < len(self.backoff):
                    self.sleep(self.backoff[attempts])
                    attempts += 1
                    continue
                raise PoolApiError(None, "transport", type(exc).__name__) from exc

            elapsed_ms = int((time.time() - start) * 1000)
            status = resp.status_code
            logger.info("[publisher] %s %s -> %d (%dms)", method, path, status, elapsed_ms)

            if status == 200:
                return resp.json()

            if status in _RETRYABLE_STATUSES:
                if attempts < len(self.backoff):
                    self.sleep(self.backoff[attempts])
                    attempts += 1
                    continue
                raise PoolApiError(status, "transport", str(status))

            if status == 401:
                if not retried_401:
                    retried_401 = True
                    self.tokens.invalidate()
                    continue
                raise self._parsed_error(resp, status)

            if status in _IMMEDIATE_ERROR_STATUSES:
                raise self._parsed_error(resp, status)

            # Any other 2xx/3xx (or an otherwise-unhandled status) is not a
            # shape this client understands — refuse rather than guess.
            raise PoolApiError(status, "unexpected_status")

    @staticmethod
    def _parsed_error(resp, status: int) -> PoolApiError:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return PoolApiError(status, f"http_{status}", None)
        return PoolApiError(
            status,
            body.get("error"),
            body.get("detail"),
            body.get("missing_batches") or [],
        )
