"""OAuth Connectors on the stream: what the control plane asks, what the Runner answers.

Console-v2 issues 24 and 30, ADR-0017 §3: **the platform never holds a readable OAuth
token.** The Runner makes the PKCE verifier, exchanges the code over its own outbound HTTPS,
seals the tokens to its installation's Recipient Key and refreshes and revokes them. The
platform carries a sealed code one way and token ciphertext the other.

So the rule this module is built around is the one the review checks first: **no item here
has a field the verifier, a plaintext code or a plaintext token could reach.** The code
travels sealed (:class:`SealedOAuthCode`), the tokens travel as a
:class:`~agentic_runner_contracts.sealed_credential.SealedCredential` in the Contract's slot,
and everything else is an id, an endpoint or an outcome. The authorise URL carries the
S256 *challenge* and the ``state`` nonce, which the browser must see anyway; the verifier
stays in the Runner's process memory and is in no model at all.

The client secret, where a template has one, is a Credential Reference slot filled by
sealed Credential Delivery like any key: the items here name the slot, never the value.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from agentic_runner_contracts.sealed_credential import KeyId, SlotName

__all__ = [
    "OAUTH_CODE_DOMAIN",
    "OAUTH_WINDOW",
    "OAuthAuthorizeUrl",
    "OAuthClient",
    "OAuthDisconnect",
    "OAuthOutcome",
    "OAuthOutcomeKind",
    "OAuthStart",
    "SealedOAuthCode",
    "oauth_code_binding",
]

# Its own prefix, for the reason the sign-in code has one: a sealed OAuth code can never
# open as a slot value or as a Claude sign-in code, nor either of those as this.
OAUTH_CODE_DOMAIN: Final[str] = "agentic-os/oauth-code/v1"

# How long an authorisation waits for its code, the Claude sign-in's window: long enough to
# read a consent page, short enough that a verifier does not sit in memory for a day.
OAUTH_WINDOW: Final[timedelta] = timedelta(minutes=10)


def _https(url: str) -> str:
    # The code, the verifier and the tokens cross these endpoints, so a plain-HTTP one is
    # refused at the schema rather than trusted to every caller (RFC 6749 §3.1, §3.2).
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.fragment:
        raise ValueError("an OAuth endpoint must be an https URL with a host and no fragment")
    return url


HttpsUrl = Annotated[str, StringConstraints(max_length=2048), AfterValidator(_https)]
AuthorizationId = Annotated[str, StringConstraints(pattern=r"^oa_[0-9a-f]{32}$")]
# RFC 6749 §3.3 scope-token: printable ASCII without space, quote or backslash.
Scope = Annotated[str, StringConstraints(pattern=r"^[\x21\x23-\x5B\x5D-\x7E]{1,256}$")]
# The registered RFC 6749 §5.2 codes and every extension seen in practice fit this; a
# provider answering anything else is reported without one rather than echoed.
ProviderError = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]


class OAuthClient(BaseModel):
    """One template's provider and the Organisation's OAuth app, as config (slice 24).

    Nothing here is a secret: a client id is public, and the client secret -- for a
    confidential client -- is named by its Credential Reference slot. A public client (PKCE,
    no secret) leaves ``client_secret_slot`` empty.
    """

    model_config = ConfigDict(extra="forbid")

    authorize_endpoint: HttpsUrl
    token_endpoint: HttpsUrl
    revocation_endpoint: HttpsUrl | None = None
    client_id: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    client_secret_slot: SlotName | None = None
    scopes: list[Scope] = Field(default_factory=list, max_length=64)


class OAuthStart(BaseModel):
    """Ack: start an authorisation for (Contract, slot, template) -- flow step 2.

    The platform names the authorisation, so it can match the URL that comes back to the
    Connect it is waiting on; the Runner makes the verifier, the challenge and ``state``.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName
    authorization_id: AuthorizationId
    client: OAuthClient
    redirect_uri: HttpsUrl


class OAuthAuthorizeUrl(BaseModel):
    """Envelope: where to send the person, on the beat after the start.

    The URL carries the S256 challenge, ``state``, the client id, the scopes and the
    console callback -- what the provider and the browser see anyway. Not the verifier.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName
    authorization_id: AuthorizationId
    authorize_url: HttpsUrl


def oauth_code_binding(
    *, contract_id: UUID | str, authorization_id: str, recipient_key_id: str
) -> bytes:
    """The AEAD associated data one sealed OAuth code is bound to (slice 24, step 3).

    Bound to the authorisation the Runner started, so a code lifted from one Connect and
    replayed against another -- or another Contract's -- does not open. The browser builds
    the same bytes in TypeScript; see ``sealed_credential.delivery_binding`` for why the
    one definition lives here.
    """

    return "|".join(
        (OAUTH_CODE_DOMAIN, str(contract_id), authorization_id, recipient_key_id)
    ).encode("utf-8")


class SealedOAuthCode(BaseModel):
    """Ack: the provider's code, sealed in the browser to this installation (step 4).

    Relayed once and never a Temporal payload, like a sign-in code. ``state`` travels
    beside it in the clear: it is the nonce the provider echoed on the callback URL, and the
    Runner compares it with the one it made before it spends the code.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    authorization_id: AuthorizationId
    recipient_key_id: KeyId
    state: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    ciphertext: Annotated[str, StringConstraints(min_length=1, max_length=4096)]

    def binding(self) -> bytes:
        return oauth_code_binding(
            contract_id=self.contract_id,
            authorization_id=self.authorization_id,
            recipient_key_id=self.recipient_key_id,
        )


class OAuthDisconnect(BaseModel):
    """Ack: Disconnect -- drop the tokens and revoke them where the provider can (step 6).

    Carries the client again because the Runner that receives it may not be the one that
    connected: it revokes with whatever its own stream opened for the slot.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName
    client: OAuthClient


class OAuthOutcomeKind(StrEnum):
    """What happened to one authorisation or one slot -- the Evidence, as a code."""

    CONNECTED = "connected"
    REFRESHED = "refreshed"
    REVOKED = "revoked"
    # Disconnected with no revocation endpoint: the plaintext is gone, the grant stands at
    # the provider until it expires or the person removes it there.
    DROPPED = "dropped"
    STATE_MISMATCH = "state_mismatch"
    # No authorisation waiting under that id: never started, already spent (a replay), or
    # another Contract's.
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    UNOPENABLE = "unopenable"
    CLIENT_SECRET_MISSING = "client_secret_missing"
    TOKEN_TOO_LARGE = "token_too_large"
    EXCHANGE_FAILED = "exchange_failed"
    REFRESH_FAILED = "refresh_failed"
    REVOKE_FAILED = "revoke_failed"


class OAuthOutcome(BaseModel):
    """Envelope: one outcome, ids only. ``provider_error`` is the RFC 6749 §5.2 ``error``
    code a provider answered, never its description or any body."""

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName | None = None
    authorization_id: AuthorizationId | None = None
    outcome: OAuthOutcomeKind
    provider_error: ProviderError | None = None
