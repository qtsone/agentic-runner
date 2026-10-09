"""OAuth Connectors, the Runner's half: start, exchange, refresh, revoke (console-v2 issue 30).

ADR-0017 §3 and slice 24's flow: the platform never holds a readable OAuth token, so every
step that touches the verifier, the code or a token runs here.

* **Start** (step 2). A PKCE verifier and its S256 challenge (RFC 7636) and a ``state``
  nonce, kept in process memory only; the authorise URL goes out on the next beat.
* **Exchange** (step 4). The code arrives sealed to this installation's Recipient Key. It is
  spent only if ``state`` matches and the authorisation is still waiting, and spending it
  ends the authorisation, so a replay finds nothing to spend. The tokens are sealed to the
  Recipient Key as the slot's next version and only that ciphertext leaves.
* **Refresh** (step 5), when the access token is within :data:`REFRESH_MARGIN` of expiry or
  when a caller saw a 401: re-sealed and uploaded the same way.
* **Revoke** (step 6). On Disconnect the plaintext goes, and the provider's revocation
  endpoint is called where the template has one (RFC 7009).

**Backed by the sealed slot, not by a file.** The token set is the slot's plaintext, so the
ack hands it back after a restart and to every other Runner of the installation, which open
it with the same key. The Runner writes no token to disk and does not write the host store:
``put`` there stays the host operator's act (``host_store``).

**Only a token set this installation sealed is one.** A funder can seal any value to the
same slot through Credential Delivery, and it opens exactly as the Runner's own upload does,
so the Runner adopts a slot as a token set only when its tag, keyed off the Recipient
private key, verifies (``SealedCredentialStream.authored``). Anything else is served as the
funder delivered it and is never refreshed or revoked -- a forged token set would otherwise
name the endpoint the Runner posts the host's client secret to.

**What a Tool Server sees** is the access token, under the slot's name, through the same
resolver as any delivered value. The token set's own shape -- refresh token, endpoint,
client -- is Runner-private and is in no contracts model.

**Two Runners, one slot.** Both may refresh near expiry. The upload is the slot's next
version, and the first one the platform stores wins; the other Runner adopts it from the
next ack (:meth:`OAuthConnectors.resolved`). A provider that revokes a whole grant on refresh
token reuse can still end the loser's grant -- the slot then reports ``refresh_failed`` and
the Connector card asks for a reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, ValidationError

from agentic_runner.credentials import CredentialResolver, UnresolvableCredentialReferenceError
from agentic_runner.integrations.oauth import OAuthProvider, OAuthProviderError, ProviderResponse
from agentic_runner.sealed_box import SealedCredentialStream
from agentic_runner_contracts.oauth_connector import (
    OAUTH_WINDOW,
    HttpsUrl,
    OAuthAuthorizeUrl,
    OAuthClient,
    OAuthDisconnect,
    OAuthOutcome,
    OAuthOutcomeKind,
    OAuthStart,
    SealedOAuthCode,
)
from agentic_runner_contracts.sealed_credential import (
    CIPHERTEXT_MAX_CHARS,
    SealedCredential,
    SlotName,
)

__all__ = ["REFRESH_MARGIN", "TOKEN_SET_PREFIX", "OAuthConnectors", "TokenSet"]

_logger = logging.getLogger(__name__)

# Marks an authored slot value (``SealedCredentialStream.authored``) as a token set.
# Versioned like a seal domain, so a later shape is a new prefix. Not what tells a token
# set from a funder's key -- anyone can write a prefix; only the authored tag says that.
TOKEN_SET_PREFIX: Final[str] = "agentic-os/oauth-token/v1:"

# Two beats and change: a token refreshed this early is never spent expired by a Tool
# Server between beats, and a provider's clock skew of a minute or two does not matter.
REFRESH_MARGIN: Final[timedelta] = timedelta(minutes=5)

# Bounds memory against a control plane that keeps starting authorisations nobody finishes.
_PENDING_MAX: Final[int] = 64

_PROVIDER_ERROR = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

SlotKey = tuple[str, str]


class _TokenSetBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    access_token: Annotated[str, StringConstraints(min_length=1)]
    refresh_token: Annotated[str, StringConstraints(min_length=1)] | None
    expires_at: AwareDatetime | None
    token_endpoint: HttpsUrl
    client_id: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    client_secret_slot: SlotName | None


@dataclass(frozen=True, slots=True)
class TokenSet:
    """One slot's tokens and what refreshing them needs. Process memory and sealed only."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime | None
    token_endpoint: str
    client_id: str
    client_secret_slot: str | None

    def encode(self) -> str:
        return TOKEN_SET_PREFIX + json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "token_endpoint": self.token_endpoint,
                "client_id": self.client_id,
                "client_secret_slot": self.client_secret_slot,
            },
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, value: str) -> TokenSet | None:
        """The token set an authored slot value holds, or None for anything malformed.

        Validated although only an authored value reaches here: the endpoint is where a
        refresh sends the refresh token and the client secret, so it meets the same https
        rule an :class:`OAuthClient` does, and a body this Runner cannot read must cost one
        slot, never the beat.
        """

        if not value.startswith(TOKEN_SET_PREFIX):
            return None
        try:
            body = _TokenSetBody.model_validate_json(value[len(TOKEN_SET_PREFIX) :])
        except ValidationError:
            return None
        return cls(**body.model_dump())


@dataclass(slots=True)
class _Pending:
    contract_id: str
    slot: str
    client: OAuthClient
    redirect_uri: str
    started_at: datetime
    verifier: str = field(repr=False)
    state: str = field(repr=False)


@dataclass(slots=True)
class _Held:
    tokens: TokenSet
    version: int
    # The ciphertext this Runner sealed and has not yet seen the ack echo back. Re-sent
    # every beat until it is, so a lost heartbeat cannot lose a refreshed token.
    upload: SealedCredential | None = None


class OAuthConnectors:
    """Every OAuth step on this Runner, driven by the heartbeat (``service``)."""

    def __init__(
        self,
        *,
        sealed: SealedCredentialStream,
        provider: OAuthProvider,
        credentials: CredentialResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sealed = sealed
        self._provider = provider
        self._credentials = credentials
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._held: dict[SlotKey, _Held] = {}
        # The version a Disconnect saw; an ack still carrying that ciphertext is not
        # re-adopted, and a reconnect seals above it.
        self._dropped: dict[SlotKey, int] = {}
        # A failed refresh is not retried every beat; a new version, or a 401, retries it.
        self._failed: set[SlotKey] = set()
        self._authorizations: list[OAuthAuthorizeUrl] = []
        self._outcomes: list[OAuthOutcome] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ the envelope

    def take_authorizations(self) -> list[OAuthAuthorizeUrl]:
        taken, self._authorizations = self._authorizations, []
        return taken

    def take_outcomes(self) -> list[OAuthOutcome]:
        taken, self._outcomes = self._outcomes, []
        return taken

    def uploads(self) -> list[SealedCredential]:
        """Token ciphertext the control plane has not stored yet. Not drained (see _Held)."""

        return [held.upload for _, held in sorted(self._held.items()) if held.upload is not None]

    # ------------------------------------------------------------------ the ack

    async def apply(
        self,
        *,
        starts: list[OAuthStart],
        codes: list[SealedOAuthCode],
        disconnects: list[OAuthDisconnect],
    ) -> None:
        """One ack's OAuth items. Disconnects first: a slot connected and disconnected in
        quick succession must end disconnected, whatever order the platform queued them."""

        async with self._lock:
            for disconnect in disconnects:
                await self._disconnect(disconnect)
            for start in starts:
                self._start(start)
            for code in codes:
                await self._exchange(code)

    def _start(self, start: OAuthStart) -> None:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        self._pending[start.authorization_id] = _Pending(
            contract_id=str(start.contract_id),
            slot=start.slot,
            client=start.client,
            redirect_uri=start.redirect_uri,
            started_at=self._clock(),
            verifier=verifier,
            state=state,
        )
        while len(self._pending) > _PENDING_MAX:
            self._pending.pop(next(iter(self._pending)))
        query = {
            "response_type": "code",
            "client_id": start.client.client_id,
            "redirect_uri": start.redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        if start.client.scopes:
            query["scope"] = " ".join(start.client.scopes)
        self._authorizations.append(
            OAuthAuthorizeUrl(
                contract_id=start.contract_id,
                slot=start.slot,
                authorization_id=start.authorization_id,
                authorize_url=_with_query(start.client.authorize_endpoint, query),
            )
        )

    async def _exchange(self, sealed: SealedOAuthCode) -> None:
        authorization_id = sealed.authorization_id
        pending = self._pending.get(authorization_id)
        if pending is None or pending.contract_id != str(sealed.contract_id):
            self._report(str(sealed.contract_id), None, authorization_id, OAuthOutcomeKind.UNKNOWN)
            return
        if self._clock() - pending.started_at > OAUTH_WINDOW:
            del self._pending[authorization_id]
            self._report(
                pending.contract_id, pending.slot, authorization_id, OAuthOutcomeKind.EXPIRED
            )
            return
        # A wrong `state` leaves the authorisation waiting: the code was injected, or is
        # another tab's, and either way the person's own callback can still land.
        if not hmac.compare_digest(sealed.state.encode(), pending.state.encode()):
            self._report(
                pending.contract_id,
                pending.slot,
                authorization_id,
                OAuthOutcomeKind.STATE_MISMATCH,
            )
            return
        code = self._sealed.open_oauth_code(sealed)
        if code is None:
            self._report(
                pending.contract_id, pending.slot, authorization_id, OAuthOutcomeKind.UNOPENABLE
            )
            return
        # Spent from here on, whatever the provider says: a code is single-use at the
        # provider, and the verifier must not outlive the one exchange it was made for.
        del self._pending[authorization_id]
        client = pending.client
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": client.client_id,
            "code_verifier": pending.verifier,
        }
        del code
        key = (pending.contract_id, pending.slot)
        response = await self._post(key, client.client_secret_slot, client.token_endpoint, form)
        if response is None:
            self._report(*key, authorization_id, OAuthOutcomeKind.CLIENT_SECRET_MISSING)
            return
        tokens = _token_set(response, client=client, now=self._clock())
        if tokens is None:
            self._report(
                *key,
                authorization_id,
                OAuthOutcomeKind.EXCHANGE_FAILED,
                provider_error=_provider_error(response),
            )
            return
        outcome = self._seal(key, tokens)
        self._report(*key, authorization_id, outcome)

    async def _disconnect(self, disconnect: OAuthDisconnect) -> None:
        key = (str(disconnect.contract_id), disconnect.slot)
        held = self._held.pop(key, None)
        self._failed.discard(key)
        self._dropped[key] = max(
            held.version if held is not None else 0, self._sealed.version(*key) or 0
        )
        for authorization_id in [
            authorization_id
            for authorization_id, pending in self._pending.items()
            if (pending.contract_id, pending.slot) == key
        ]:
            del self._pending[authorization_id]
        client = disconnect.client
        if held is None or client.revocation_endpoint is None:
            self._report(*key, None, OAuthOutcomeKind.DROPPED)
            return
        # The refresh token, where there is one: RFC 7009 §2.1 lets the provider end the
        # whole grant from it, which an access token's revocation does not promise.
        token, hint = (
            (held.tokens.refresh_token, "refresh_token")
            if held.tokens.refresh_token
            else (held.tokens.access_token, "access_token")
        )
        form = {"token": token, "token_type_hint": hint, "client_id": client.client_id}
        response = await self._post(
            key, client.client_secret_slot, client.revocation_endpoint, form
        )
        if response is None:
            self._report(*key, None, OAuthOutcomeKind.CLIENT_SECRET_MISSING)
        elif response.status == 200:
            self._report(*key, None, OAuthOutcomeKind.REVOKED)
        else:
            self._report(
                *key,
                None,
                OAuthOutcomeKind.REVOKE_FAILED,
                provider_error=_provider_error(response),
            )

    # ------------------------------------------------------------------ refresh

    async def before_beat(self) -> None:
        """Forget authorisations past their window, then refresh every token set within
        :data:`REFRESH_MARGIN` of expiry (step 5) -- so the upload rides this beat."""

        async with self._lock:
            now = self._clock()
            for authorization_id, pending in list(self._pending.items()):
                if now - pending.started_at > OAUTH_WINDOW:
                    del self._pending[authorization_id]
                    self._report(
                        pending.contract_id,
                        pending.slot,
                        authorization_id,
                        OAuthOutcomeKind.EXPIRED,
                    )
            for key, held in sorted(self._held.items()):
                expires_at = held.tokens.expires_at
                if key in self._failed or held.tokens.refresh_token is None:
                    continue
                if expires_at is not None and expires_at - now <= REFRESH_MARGIN:
                    await self._refresh(key)

    async def refresh(self, contract_id: UUID | str, slot: str) -> bool:
        """Refresh now, because a provider answered 401 to the access token (step 5).

        Retries a slot whose last refresh failed, unlike :meth:`before_beat`: a 401 is new
        information that the token is dead.
        """

        async with self._lock:
            return await self._refresh((str(contract_id), slot))

    async def _refresh(self, key: SlotKey) -> bool:
        held = self._held.get(key)
        if held is None or held.tokens.refresh_token is None:
            return False
        tokens = held.tokens
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
        }
        response = await self._post(key, tokens.client_secret_slot, tokens.token_endpoint, form)
        if response is None:
            self._failed.add(key)
            self._report(*key, None, OAuthOutcomeKind.CLIENT_SECRET_MISSING)
            return False
        fresh = _token_set(response, client=None, now=self._clock(), previous=tokens)
        if fresh is None:
            self._failed.add(key)
            self._report(
                *key,
                None,
                OAuthOutcomeKind.REFRESH_FAILED,
                provider_error=_provider_error(response),
            )
            return False
        outcome = self._seal(key, fresh)
        if outcome is OAuthOutcomeKind.CONNECTED:
            outcome = OAuthOutcomeKind.REFRESHED
        else:
            self._failed.add(key)
        self._report(*key, None, outcome)
        return outcome is OAuthOutcomeKind.REFRESHED

    # ------------------------------------------------------------------ the resolver

    def resolved(self) -> dict[SlotKey, str]:
        """What the resolver serves: every opened slot, a token set as its access token.

        Also where the ack's token sets are adopted. A version at or above what this
        Runner holds replaces it -- another Runner of the installation refreshed, or the
        upload landed; a lower one is the ack not yet carrying this Runner's own upload
        and is ignored. A slot the ack no longer carries is dropped once its upload was
        confirmed, which is how a Disconnect handled by another Runner reaches this one.
        """

        served: dict[SlotKey, str] = {}
        carried: set[SlotKey] = set()
        for key, value in self._sealed.plaintext.items():
            # Only a value this installation authored is ever read as a token set: its
            # endpoint and client secret slot steer where the Runner sends a secret, and a
            # funder can seal anything to the slot. Any other value is served as delivered.
            body = self._sealed.authored(*key, value)
            if body is None:
                served[key] = value
                continue
            tokens = TokenSet.decode(body)
            if tokens is None:
                _logger.warning("OAuth token set for %s/%s is unreadable; not served", *key)
                continue
            version = self._sealed.version(*key) or 0
            if version <= self._dropped.get(key, 0):
                continue
            carried.add(key)
            held = self._held.get(key)
            if held is None or version >= held.version:
                if held is None or held.version != version:
                    self._failed.discard(key)
                self._held[key] = _Held(tokens=tokens, version=version)
        for key in [key for key, held in self._held.items() if key not in carried]:
            if self._held[key].upload is None:
                del self._held[key]
        for key, held in self._held.items():
            served[key] = held.tokens.access_token
        return served

    # ------------------------------------------------------------------ helpers

    def _seal(self, key: SlotKey, tokens: TokenSet) -> OAuthOutcomeKind:
        held = self._held.get(key)
        version = 1 + max(
            held.version if held is not None else 0,
            self._sealed.version(*key) or 0,
            self._dropped.get(key, 0),
        )
        upload = self._sealed.seal_authored(*key, version=version, value=tokens.encode())
        if len(upload.ciphertext) > CIPHERTEXT_MAX_CHARS:
            return OAuthOutcomeKind.TOKEN_TOO_LARGE
        self._held[key] = _Held(tokens=tokens, version=version, upload=upload)
        self._failed.discard(key)
        return OAuthOutcomeKind.CONNECTED

    async def _post(
        self, key: SlotKey, secret_slot: str | None, url: str, form: Mapping[str, str | None]
    ) -> ProviderResponse | None:
        """POST to the provider with client authentication; None if the secret is missing.

        ``client_secret_post`` (RFC 6749 §2.3.1): every provider that issues a secret
        accepts it, and it keeps the secret out of a header a proxy might log.
        """

        body = {name: value for name, value in form.items() if value is not None}
        if secret_slot is not None:
            try:
                credentials = self._credentials.resolve(contract_id=key[0], manifest=[secret_slot])
            except UnresolvableCredentialReferenceError:
                return None
            body["client_secret"] = credentials.for_runner_hosted_server(secret_slot)
        try:
            return await self._provider.post_form(url, body)
        except OAuthProviderError as error:
            _logger.warning("OAuth provider unreachable for %s/%s: %s", *key, error)
            return ProviderResponse(status=0)

    def _report(
        self,
        contract_id: str,
        slot: str | None,
        authorization_id: str | None,
        outcome: OAuthOutcomeKind,
        *,
        provider_error: str | None = None,
    ) -> None:
        _logger.info("OAuth %s for %s/%s (%s)", outcome.value, contract_id, slot, authorization_id)
        self._outcomes.append(
            OAuthOutcome(
                contract_id=UUID(contract_id),
                slot=slot,
                authorization_id=authorization_id,
                outcome=outcome,
                provider_error=provider_error,
            )
        )


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _with_query(endpoint: str, query: Mapping[str, str]) -> str:
    # An authorize endpoint may carry its own query (a tenant, an audience); RFC 6749
    # §3.1 says to keep it and add ours.
    parts = urlsplit(endpoint)
    joined = f"{parts.query}&{urlencode(query)}" if parts.query else urlencode(query)
    return urlunsplit(parts._replace(query=joined))


def _token_set(
    response: ProviderResponse,
    *,
    client: OAuthClient | None,
    now: datetime,
    previous: TokenSet | None = None,
) -> TokenSet | None:
    """RFC 6749 §5.1, or None. A refresh that returns no new refresh token keeps the old
    one (§6: the provider *may* issue a new one)."""

    body = response.body
    access_token = body.get("access_token")
    if response.status != 200 or not isinstance(access_token, str) or not access_token:
        return None
    token_type = body.get("token_type")
    if isinstance(token_type, str) and token_type.lower() != "bearer":
        return None
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    expires_at = (
        now + timedelta(seconds=expires_in)
        if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0
        else None
    )
    if previous is not None:
        return replace(
            previous,
            access_token=access_token,
            refresh_token=refresh_token
            if isinstance(refresh_token, str)
            else previous.refresh_token,
            expires_at=expires_at,
        )
    assert client is not None
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=expires_at,
        token_endpoint=client.token_endpoint,
        client_id=client.client_id,
        client_secret_slot=client.client_secret_slot,
    )


def _provider_error(response: ProviderResponse) -> str | None:
    error = response.body.get("error")
    return error if isinstance(error, str) and _PROVIDER_ERROR.match(error) else None
