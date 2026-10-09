"""A fake OAuth provider that enforces what a real one does (issue 30's Runner tests).

Strict on purpose: a code is single-use and bound to its client, redirect and S256
challenge; a refresh token rotates and the old one stops working; revocation ends both.
A Runner that sent the wrong verifier, replayed a code or kept an old refresh token fails
here the way it would fail against a provider.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from agentic_runner.integrations.oauth import ProviderResponse

__all__ = ["FakeOAuthProvider"]

AUTHORIZE = "https://provider.test/authorize"
TOKEN = "https://provider.test/token"
REVOKE = "https://provider.test/revoke"


@dataclass
class _Grant:
    client_id: str
    redirect_uri: str
    challenge: str
    scope: str


@dataclass
class FakeOAuthProvider:
    client_id: str = "client-123"
    client_secret: str | None = None
    expires_in: int | None = 3600
    authorize_endpoint: str = AUTHORIZE
    token_endpoint: str = TOKEN
    revocation_endpoint: str = REVOKE
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)
    _codes: dict[str, _Grant] = field(default_factory=dict)
    # token -> the grant it belongs to; a refresh token also keeps the grant's scope.
    _access: dict[str, str] = field(default_factory=dict)
    _refresh: dict[str, tuple[str, str]] = field(default_factory=dict)

    def consent(self, authorize_url: str) -> tuple[str, str]:
        """The person approves: the ``(code, state)`` the callback URL would carry."""

        query = {key: values[0] for key, values in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        code = f"code-{secrets.token_hex(8)}"
        self._codes[code] = _Grant(
            client_id=query["client_id"],
            redirect_uri=query["redirect_uri"],
            challenge=query["code_challenge"],
            scope=query.get("scope", ""),
        )
        return code, query["state"]

    def access_valid(self, token: str) -> bool:
        return token in self._access and token not in self.revoked

    async def post_form(self, url: str, form: Mapping[str, str]) -> ProviderResponse:
        self.requests.append((url, dict(form)))
        if url == self.revocation_endpoint:
            return self._revoke(form)
        if url != self.token_endpoint:
            return ProviderResponse(404)
        if not self._client_ok(form):
            return _error(401, "invalid_client")
        if form.get("grant_type") == "authorization_code":
            return self._exchange(form)
        if form.get("grant_type") == "refresh_token":
            return self._rotate(form)
        return _error(400, "unsupported_grant_type")

    def _client_ok(self, form: Mapping[str, str]) -> bool:
        if form.get("client_id") != self.client_id:
            return False
        return self.client_secret is None or form.get("client_secret") == self.client_secret

    def _exchange(self, form: Mapping[str, str]) -> ProviderResponse:
        grant = self._codes.pop(form.get("code", ""), None)
        if grant is None or grant.redirect_uri != form.get("redirect_uri"):
            return _error(400, "invalid_grant")
        verifier = form.get("code_verifier", "")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii") != grant.challenge:
            return _error(400, "invalid_grant")
        return self._tokens(f"grant-{secrets.token_hex(8)}", grant.scope)

    def _rotate(self, form: Mapping[str, str]) -> ProviderResponse:
        held = self._refresh.pop(form.get("refresh_token", ""), None)
        if held is None:
            return _error(400, "invalid_grant")
        return self._tokens(*held)

    def _tokens(self, grant_id: str, scope: str) -> ProviderResponse:
        access = f"at-{secrets.token_hex(16)}"
        refresh = f"rt-{secrets.token_hex(16)}"
        self._access[access] = grant_id
        self._refresh[refresh] = (grant_id, scope)
        self.issued += [access, refresh]
        body: dict[str, object] = {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "scope": scope,
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return ProviderResponse(200, body)

    def _revoke(self, form: Mapping[str, str]) -> ProviderResponse:
        if not self._client_ok(form):
            return _error(401, "invalid_client")
        token = form.get("token", "")
        self.revoked.add(token)
        # RFC 7009 §2.1: revoking a refresh token may revoke the grant's access tokens;
        # this provider does, so the test can see the access token stop working.
        held = self._refresh.pop(token, None)
        if held is not None:
            self.revoked.update(t for t, grant_id in self._access.items() if grant_id == held[0])
        return ProviderResponse(200)


def _error(status: int, code: str) -> ProviderResponse:
    return ProviderResponse(status, {"error": code, "error_description": "fake provider"})
