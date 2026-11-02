"""Secrets: the two shares, their bindings and the release (Spec: Agent Guardrails rev 7, S3-S4).

A Secret is a named value stored once at one scope -- the Organisation's or a Product's
in this release -- and never readable back. It generalises the pool's LLM Key
(:mod:`.llm_key`): the browser where the value is typed splits it into two shares, the
value XOR a random share, seals the random one to a **holder key** and sends the control
plane the other, which the control plane encrypts with its own platform key. The holder
key of an Organisation or Product secret is the **Organisation Key** (the Pool Key of
Spec: Runners, same key); a member's own secrets will seal to their User Key.

At Directive start the Runner asks for the secrets the work needs that it does not hold
on its own host (A2: a value the host installed under the same name still counts). The
control plane answers with :class:`SecretRelease`: each holder share as stored, and its
own share sealed to that Runner's Recipient Key. The Runner opens both, joins them, and
holds the value in memory for that one Directive. A copy of the control plane's data, the
platform key, or a stopped Runner's disk opens nothing on its own (S7).

The sealed box is the one :mod:`.llm_key` and :mod:`.sealed_credential` use, unchanged;
the bindings are under their own domain :data:`SECRET_DOMAIN`, so a secret's share never
opens as an LLM Key's, a sealed credential or an OAuth code, nor as another secret's.

``fixtures/secret.fixture.json`` is one secret split, sealed and bound by these rules.
The contracts tests open it, and the console's tests read the same file.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from agentic_runner_contracts.llm_key import Fingerprint, Identifier, SealedShare
from agentic_runner_contracts.sealed_credential import KeyId, SlotName

__all__ = [
    "SECRET_DOMAIN",
    "HolderKind",
    "ReleasedSecret",
    "SecretName",
    "SecretRelease",
    "SecretReleaseRequest",
    "SecretScope",
    "holder_share_binding",
    "join_secret",
    "platform_share_binding",
    "split_secret",
]

SECRET_DOMAIN: Final[str] = "agentic-os/secret/v1"

# A secret fills the Credential Reference of the same name, so it has that name's shape.
SecretName = SlotName

# One Directive's references fit well inside this; the bound keeps a release request small.
_NAMES_MAX: Final[int] = 64
_HOLDERS_MAX: Final[int] = 16


class HolderKind(StrEnum):
    """Which key opens a secret's holder share on a Runner."""

    ORGANISATION_KEY = "organisation_key"
    USER_KEY = "user_key"


class SecretScope(StrEnum):
    """Where a secret is set. The more specific scope wins for one name (S1)."""

    ORGANISATION = "organisation"
    PRODUCT = "product"


def holder_share_binding(
    *,
    organisation_id: str,
    secret_id: str,
    holder_kind: HolderKind,
    holder_fingerprint: str,
) -> bytes:
    """The AEAD associated data of the share sealed to a holder key.

    Bound to the Organisation, the secret and the holder key: a share lifted onto another
    secret's row, another Organisation's, or kept across a new holder key fails the tag.
    """

    return "|".join(
        (
            SECRET_DOMAIN,
            "holder-share",
            organisation_id,
            secret_id,
            holder_kind.value,
            holder_fingerprint,
        )
    ).encode("utf-8")


def platform_share_binding(*, secret_id: str, recipient_key_id: str) -> bytes:
    """The AEAD associated data of the control plane's share, sealed to one Runner's
    Recipient Key."""

    return "|".join((SECRET_DOMAIN, "platform-share", secret_id, recipient_key_id)).encode("utf-8")


def split_secret(value: str) -> tuple[str, str]:
    """``(holder_share, platform_share)``, each base64: a random share, and the value XOR
    it. The console splits in the browser; this is the same rule, for tests and clients."""

    raw = value.encode("utf-8")
    holder = secrets.token_bytes(len(raw))
    platform = bytes(left ^ right for left, right in zip(raw, holder, strict=True))
    return _b64(holder), _b64(platform)


def join_secret(holder_share: str, platform_share: str) -> str:
    """The value the two shares make, or ``ValueError`` when they cannot be one value's."""

    holder = _unb64(holder_share)
    platform = _unb64(platform_share)
    if not holder:
        raise ValueError("a secret's share is empty")
    if len(holder) != len(platform):
        raise ValueError("the two shares are not one secret's: their lengths differ")
    try:
        return bytes(left ^ right for left, right in zip(holder, platform, strict=True)).decode(
            "utf-8"
        )
    except UnicodeDecodeError as error:
        raise ValueError("the two shares are not one secret's") from error


class SecretReleaseRequest(BaseModel):
    """What a Runner sends, with its Directive token, to have the work's secrets released.

    ``names`` are the Credential References the Directive needs and the Runner's host does
    not hold. ``holder_fingerprints`` are the holder keys this Runner holds: a release
    sealed to any other would not open.
    """

    model_config = ConfigDict(extra="forbid")

    directive_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    names: list[SecretName] = Field(min_length=1, max_length=_NAMES_MAX)
    holder_fingerprints: list[Fingerprint] = Field(min_length=1, max_length=_HOLDERS_MAX)

    @field_validator("names")
    @classmethod
    def _distinct(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("a release names each secret once")
        return names


class ReleasedSecret(BaseModel):
    """One secret, as two sealed shares. Ciphertext, ids and the name only."""

    model_config = ConfigDict(extra="forbid")

    secret_id: Identifier
    name: SecretName
    scope: SecretScope
    holder_kind: HolderKind
    holder_fingerprint: Fingerprint
    holder_share: SealedShare
    platform_share: SealedShare


class SecretRelease(BaseModel):
    """Every secret a Directive asked for, each released to this Runner's Recipient Key."""

    model_config = ConfigDict(extra="forbid")

    organisation_id: Identifier
    recipient_key_id: KeyId
    secrets: list[ReleasedSecret] = Field(max_length=_NAMES_MAX)

    def holder_share_binding(self, secret: ReleasedSecret) -> bytes:
        return holder_share_binding(
            organisation_id=self.organisation_id,
            secret_id=secret.secret_id,
            holder_kind=secret.holder_kind,
            holder_fingerprint=secret.holder_fingerprint,
        )

    def platform_share_binding(self, secret: ReleasedSecret) -> bytes:
        return platform_share_binding(
            secret_id=secret.secret_id, recipient_key_id=self.recipient_key_id
        )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("a secret's share must be base64") from error
