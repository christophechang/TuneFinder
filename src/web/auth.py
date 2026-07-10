"""Bearer-secret auth for the web API.

Single-user model, mirroring the MixLab/.NET convention: one shared secret
(TUNEFINDER_API_SECRET), compared in constant time, sent as
`Authorization: Bearer <secret>`. Fail-closed: create_app refuses to start
without a secret unless TUNEFINDER_WEB_INSECURE=1 is set explicitly
(LAN-only / reverse-proxy-authenticated deployments).
"""
from __future__ import annotations

import hmac
from typing import Callable

from fastapi import Header, HTTPException


class AuthConfigError(RuntimeError):
    """Refusing to start without a secret and without the explicit insecure opt-out."""


def check_auth_config(settings) -> None:
    if not settings.web_api_secret and not settings.web_insecure:
        raise AuthConfigError(
            "TUNEFINDER_API_SECRET is not set. Set it (any long random string), "
            "or set TUNEFINDER_WEB_INSECURE=1 to run without auth on a trusted network."
        )


def make_auth_dependency(settings) -> Callable:
    """Build the FastAPI dependency enforcing the bearer secret."""
    secret = settings.web_api_secret
    insecure = settings.web_insecure

    async def require_bearer(authorization: str = Header(default="")) -> None:
        if insecure and not secret:
            return
        expected = f"Bearer {secret}"
        if not secret or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    return require_bearer
