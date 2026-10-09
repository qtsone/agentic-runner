"""The OAuth provider port: one form POST to a token or revocation endpoint (issue 30).

RFC 6749 §4.1.3 / §6 and RFC 7009 are each a form POST answered with JSON, so the port is
exactly that and the protocol lives in :mod:`agentic_runner.oauth_connectors`, where a
fake provider exercises all of it. ``HttpOAuthProvider`` is the Runner's own outbound HTTPS
-- the exchange never touches the control plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx

__all__ = [
    "HttpOAuthProvider",
    "OAuthProvider",
    "OAuthProviderError",
    "ProviderResponse",
]

# One beat's budget: the exchange and a refresh run on the heartbeat, so a provider that
# hangs must cost a failed step and not a stale link.
_TIMEOUT: Final[float] = 10.0


class OAuthProviderError(RuntimeError):
    """The provider could not be reached or did not answer HTTP. Never carries a body."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    status: int
    # repr=False: a token response is the one body that must never reach a log line.
    body: Mapping[str, object] = field(default_factory=dict, repr=False)


class OAuthProvider(Protocol):
    async def post_form(self, url: str, form: Mapping[str, str]) -> ProviderResponse: ...


class HttpOAuthProvider:
    """The real provider: HTTPS only, no redirects, JSON asked for.

    Redirects are not followed because a 30x on a token endpoint would re-send the code
    and the verifier to wherever it points. ``Accept: application/json`` because GitHub,
    among others, answers form-encoded unless asked.
    """

    async def post_form(self, url: str, form: Mapping[str, str]) -> ProviderResponse:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                response = await client.post(
                    url, data=dict(form), headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as error:
            # The type only: an httpx error's text can carry the request URL, and a
            # refresh's form is in no URL, but nothing here needs more than the class.
            raise OAuthProviderError(type(error).__name__) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        return ProviderResponse(
            status=response.status_code, body=body if isinstance(body, dict) else {}
        )
