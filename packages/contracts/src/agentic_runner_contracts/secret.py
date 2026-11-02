"""Secrets: the two shares, their bindings and the release (Spec: Agent Guardrails rev 7, S3-S4).

A Secret is a named value stored once at one scope (:class:`SecretScope`) and never
readable back. It generalises the pool's LLM Key
(:mod:`.llm_key`): the browser where the value is typed splits it into two shares, the
value XOR a random share, seals the random one to a **holder key** and sends the control
plane the other, which the control plane encrypts with its own platform key. The holder
key of an Organisation or Product secret is the **Organisation Key** (the Pool Key of
Spec: Runners, same key); a member's own secrets seal to their **User Key**.

At Directive start the Runner asks for the secrets the work needs that it does not hold
on its own host (A2: a value the host installed under the same name still counts). The
control plane answers with :class:`SecretRelease`: each holder share as stored, and its
own share sealed to that Runner's Recipient Key. The Runner opens both, joins them, and
holds the value in memory for that one Directive. A copy of the control plane's data, the
platform key, or a stopped Runner's disk opens nothing on its own (S7).

The sealed box is the one :mod:`.llm_key` and :mod:`.sealed_credential` use, unchanged;
the bindings are under their own domain :data:`SECRET_DOMAIN`, so a secret's share never
opens as an LLM Key's, a sealed credential or an OAuth code, nor as another secret's.

An Organisation or Product secret an Admin marks "Also members' Runners" (A1) has its
holder share sealed also to each eligible member's User Key. The browser that typed it
sealed it to the Organisation Key only, so for each member it is resealed later (S5): an
Organisation Runner, which holds the Organisation Key, is handed :class:`HolderReseal` on
its heartbeat ack, opens the share and seals it to the member's User Key, and reports
:class:`ResealedHolderShare` on its next envelope. Only the holder share moves: the
platform's share never reaches the Runner this way, so the reseal opens no value.

Whoever reseals seals only to a User Key an Admin approved (rev 10, S5): the reseal
carries the key's approval tag (:mod:`.user_key_approval`), and one with no valid tag is
refused and reported back as :class:`UnapprovedUserKey`. And only a Secret an Admin
marked (rev 12, S5): the reseal carries the Secret's availability tag
(:mod:`.secret_availability`), checked against the Secret the share is bound to; one with
no valid tag is refused and only logged, since the control plane already shows an
untagged Secret as waiting for an Admin.

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
from agentic_runner_contracts.sealed_credential import KeyId, SlotName, key_fingerprint
from agentic_runner_contracts.secret_availability import AvailabilityTag, availability_tag_valid
from agentic_runner_contracts.user_key import PublicKey
from agentic_runner_contracts.user_key_approval import ApprovalTag, approval_tag_valid

__all__ = [
    "SECRET_DOMAIN",
    "RESEALS_MAX",
    "HolderKind",
    "HolderReseal",
    "ReleasedSecret",
    "ResealedHolderShare",
    "SecretName",
    "SecretRelease",
    "SecretReleaseRequest",
    "SecretScope",
    "UnapprovedUserKey",
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
# Reseals ride the heartbeat, so one beat carries a bounded batch; the rest wait a beat.
RESEALS_MAX: Final[int] = 64


class HolderKind(StrEnum):
    """Which key opens a secret's holder share on a Runner."""

    ORGANISATION_KEY = "organisation_key"
    USER_KEY = "user_key"


class SecretScope(StrEnum):
    """Where a secret is set. The more specific scope wins for one name (S1): a Runner's,
    then an Agent's, then a member's for a Product, then a Product's, then the
    Organisation's. The last two are an Admin's and sealed to the Organisation Key; the
    first three are a member's own and sealed to their User Key (:mod:`.user_key`)."""

    ORGANISATION = "organisation"
    PRODUCT = "product"
    MEMBER_PRODUCT = "member_product"
    AGENT = "agent"
    RUNNER = "runner"


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


class HolderReseal(BaseModel):
    """One holder share an Organisation Runner reseals to a member's User Key (S5, A1).

    ``holder_share`` is the share as stored, sealed to the Organisation Key of
    ``holder_fingerprint``; it opens only under its own binding, so a share of another
    secret or Organisation does not. The resealed share is bound to the fingerprint the
    Runner computes from ``user_key_public_key`` itself, never to one it is told.

    ``user_id`` and ``approval_tag`` are the member and the tag an Admin wrote when they
    approved this User Key (rev 10, S5). Optional only so a body from a control plane on
    4.4 still parses: with either missing, :meth:`approved` is false and nothing is
    resealed.

    ``availability_tag`` is the tag an Admin wrote when they marked this Secret "Also
    members' Runners" (rev 12, S5); optional so a 4.5 body still parses, and with it
    missing :meth:`available` is false.
    """

    model_config = ConfigDict(extra="forbid")

    organisation_id: Identifier
    secret_id: Identifier
    holder_fingerprint: Fingerprint
    holder_share: SealedShare
    user_key_public_key: PublicKey
    user_id: Identifier | None = None
    approval_tag: ApprovalTag | None = None
    availability_tag: AvailabilityTag | None = None

    @property
    def user_key_fingerprint(self) -> str:
        return key_fingerprint(self.user_key_public_key)

    def approved(self, organisation_key_private: str) -> bool:
        """Whether the tag approves this User Key for this member in this Organisation,
        under the Organisation Key whose private half is given."""

        return self.user_id is not None and approval_tag_valid(
            self.approval_tag,
            organisation_key_private=organisation_key_private,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            user_key_fingerprint=self.user_key_fingerprint,
        )

    def available(self, organisation_key_private: str) -> bool:
        """Whether the tag marks this Secret "Also members' Runners" in this Organisation,
        under the Organisation Key whose private half is given. Checked on the ids the
        share is bound to (:meth:`holder_share_binding`), so a tag lifted from another
        Secret cannot open this one's share."""

        return availability_tag_valid(
            self.availability_tag,
            organisation_key_private=organisation_key_private,
            organisation_id=self.organisation_id,
            secret_id=self.secret_id,
        )

    def holder_share_binding(self) -> bytes:
        return holder_share_binding(
            organisation_id=self.organisation_id,
            secret_id=self.secret_id,
            holder_kind=HolderKind.ORGANISATION_KEY,
            holder_fingerprint=self.holder_fingerprint,
        )

    def resealed_binding(self) -> bytes:
        return holder_share_binding(
            organisation_id=self.organisation_id,
            secret_id=self.secret_id,
            holder_kind=HolderKind.USER_KEY,
            holder_fingerprint=self.user_key_fingerprint,
        )


class ResealedHolderShare(BaseModel):
    """A holder share resealed to the User Key of ``holder_fingerprint``. Ciphertext only."""

    model_config = ConfigDict(extra="forbid")

    organisation_id: Identifier
    secret_id: Identifier
    holder_fingerprint: Fingerprint
    holder_share: SealedShare


class UnapprovedUserKey(BaseModel):
    """A member's User Key a reseal was refused for: its approval tag is missing or does
    not verify under this Runner's Organisation Key (rev 10, S5). ``approval_tag`` is the
    tag refused, so the control plane drops that approval and never a newer one."""

    model_config = ConfigDict(extra="forbid")

    organisation_id: Identifier
    user_id: Identifier
    user_key_fingerprint: Fingerprint
    approval_tag: ApprovalTag | None = None


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("a secret's share must be base64") from error
