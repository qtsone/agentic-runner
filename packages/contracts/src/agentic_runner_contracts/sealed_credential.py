"""Sealed Credential Delivery: the wire, the binding and the fingerprint (PRD issue 48).

Map ticket 22 A3-A5 and A7. The default for a cross-party credential stays **hand-over**
(22 A2): the funder gives the value to the host operator, who installs it under its
Credential Reference name like any other. This module is the *exception* path, for a
funder who does not host -- user-funded/org-hosted, org-funded/user-hosted, and the chain
case where the holder is an Account Admin (21 A7).

What it buys, stated as honestly here as the console states it to the funder
(:data:`HONEST_STATEMENT`): the value stays out of the Organisation Admin's hands as a
casual matter and out of the platform entirely. It does **not** protect against a
determined host, because the Runner holds the plaintext in memory on the host's machine
either way.

Three rules are the schema's rather than a reviewer's:

* **Ciphertext, never a value.** :class:`SealedCredential` carries base64 ciphertext and
  ids. There is no plaintext field to fill in by accident, in either direction.
* **Bound to the triple.** :func:`delivery_binding` is the AEAD's associated data, so a
  ciphertext lifted from one ``(contract, slot, recipient key)`` and replayed against
  another does not open -- the tag check fails, which is a refusal and not a wrong value.
* **Names only in the clear.** The slot *is* the Credential Reference name from the
  Contract's manifest (ADR-0013 §11: the control plane holds names only).

Dependency-free on purpose: this distribution is the version floor every Runner installs,
so the sealed box's own X25519 lives in the Runner (``agentic_runner.sealed_box``) and the
browser's in ``frontend/src/lib/sealed-credential.ts``. What all three share is here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "CIPHERTEXT_MAX_CHARS",
    "HONEST_STATEMENT",
    "RENEWAL_INTERVAL",
    "SEAL_DOMAIN",
    "SIGN_IN_CODE_DOMAIN",
    "OpenedCredential",
    "SealedCredential",
    "SealedSignInCode",
    "SignInCodeOutcome",
    "SignInCodeRelay",
    "SlotDelivery",
    "delivery_binding",
    "key_fingerprint",
    "sign_in_code_binding",
]

# Domain separation. Version it rather than the wire: a second construction would be a
# second prefix here and a Runner that cannot open it says so, instead of opening
# something it misread.
SEAL_DOMAIN: Final[str] = "agentic-os/sealed-credential/v1"

# A one-time sign-in code is sealed to the same Recipient Key but is not a credential slot
# (local-agents 05): its own prefix means a code can never open as a slot value, nor a slot
# value as a code.
SIGN_IN_CODE_DOMAIN: Final[str] = "agentic-os/sign-in-code/v1"

# 22 A7: the Runner regenerates its keypair on this cadence, re-seals every slot to the
# new key and destroys the old private half only once every slot reports the new key id.
RENEWAL_INTERVAL: Final[timedelta] = timedelta(days=30)

# An API key or a `claude setup-token` bearer, sealed and base64'd -- the two shapes 22
# A3 admits. A device login is never delivered (issue 31 creates it in place), and
# nothing here is a file: the bound is what says so on the wire.
CIPHERTEXT_MAX_CHARS: Final[int] = 8192

# 22 A2, rendered verbatim on every delivery form (the consoles import this string rather
# than restating it, so the honest statement cannot drift between three pages).
HONEST_STATEMENT: Final[str] = (
    "Sealing keeps this value out of the Organisation Admin's hands as a casual matter, "
    "and out of the platform entirely — we store ciphertext we cannot open. It does not "
    "protect you against a determined host: the Runner holds the value in memory on the "
    "host's machine either way."
)

# The Credential Reference name the value fills. Same shape the Contract's credential
# manifest names it by; a path separator is excluded so a slot can never address a file.
SlotName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")]
KeyId = Annotated[str, StringConstraints(min_length=8, max_length=128)]


def delivery_binding(*, contract_id: UUID | str, slot: str, recipient_key_id: str) -> bytes:
    """The AEAD associated data one sealed value is bound to (22 A4).

    Bytes, and assembled in exactly one place, because both ends must produce the same
    string from the same triple or nothing opens: the browser builds it in TypeScript and
    the Runner in Python, and a divergence here would surface as an unopenable value on
    somebody's machine rather than as a failing test.
    """

    return "|".join((SEAL_DOMAIN, str(contract_id), slot, recipient_key_id)).encode("utf-8")


def sign_in_code_binding(
    *, contract_id: UUID | str, sign_in_id: str, recipient_key_id: str
) -> bytes:
    """The AEAD associated data one sealed sign-in code is bound to (local-agents 05).

    Bound to the sign-in the Runner started, so a code lifted from one sign-in and
    replayed against another -- or another Contract's -- does not open. The browser builds
    the same bytes in TypeScript; see :func:`delivery_binding` for why this lives here.
    """

    return "|".join((SIGN_IN_CODE_DOMAIN, str(contract_id), sign_in_id, recipient_key_id)).encode(
        "utf-8"
    )


def key_fingerprint(public_key: str) -> str:
    """The Recipient Key's fingerprint, for out-of-band comparison (22 A4).

    Printed by the console beside the slot and by ``agentic-runner status``; a funder
    sealing to a key compares the two by eye before typing a value. SHA-256 truncated to
    128 bits and grouped, the ssh-keygen shape an operator already reads that way.
    """

    try:
        raw = base64.b64decode(public_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("a Recipient Key's public half must be base64") from error
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return ":".join(digest[index : index + 4] for index in range(0, 32, 4))


class OpenedCredential(BaseModel):
    """One slot a Runner opened, reported on its next heartbeat (22 A10's Evidence list).

    Reported rather than inferred, because only the Runner can know it: the platform
    stores ciphertext it holds no key for, so "did this value open" is a fact about the
    host and nothing on the control plane could derive it. Ids and a version -- there is
    no field here a value or a digest of one could reach.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName
    recipient_key_id: KeyId
    version: int = Field(ge=1)


class SealedCredential(BaseModel):
    """One slot's ciphertext, as the control plane stores it and the stream carries it.

    The platform cannot open this and holds no key that could; it is a courier. The
    ``version`` is what a Runner watches to know a funder replaced the value or a
    renewal re-sealed it, and what makes "pull on boot, on activation and on a version
    change" a comparison rather than a poll.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    slot: SlotName
    recipient_key_id: KeyId
    version: int = Field(ge=1)
    ciphertext: Annotated[str, StringConstraints(min_length=1, max_length=CIPHERTEXT_MAX_CHARS)]

    @property
    def key(self) -> tuple[UUID, str]:
        """What identifies the slot this ciphertext fills, across renewals."""

        return (self.contract_id, self.slot)

    def binding(self) -> bytes:
        return delivery_binding(
            contract_id=self.contract_id, slot=self.slot, recipient_key_id=self.recipient_key_id
        )


class SlotDelivery(BaseModel):
    """What the funder sees for one slot (22 A10) -- never the value, never a digest of it.

    ``installations`` / ``delivered`` are the "Delivered to 2 of 2 Runners" figures: how
    many installations this Contract's Directives may land on, and how many hold a
    ciphertext sealed to their own current Recipient Key. Self-reported, with the host
    named beside it, exactly as every other Runner-sourced fact is.
    """

    model_config = ConfigDict(extra="forbid")

    slot: SlotName
    installations: int = Field(ge=0)
    delivered: int = Field(ge=0)
    stale: bool = False
    host_party: str = ""


SignInId = Annotated[str, StringConstraints(pattern=r"^si_[0-9a-f]{32}$")]


class SealedSignInCode(BaseModel):
    """A one-time browser sign-in code, sealed to the Runner that started the sign-in.

    Carried on the heartbeat ack once and never in a Temporal payload (local-agents 05):
    the platform relays ciphertext it cannot open and deletes it on delivery or expiry.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    sign_in_id: SignInId
    recipient_key_id: KeyId
    ciphertext: Annotated[str, StringConstraints(min_length=1, max_length=2048)]

    def binding(self) -> bytes:
        return sign_in_code_binding(
            contract_id=self.contract_id,
            sign_in_id=self.sign_in_id,
            recipient_key_id=self.recipient_key_id,
        )


class SignInCodeOutcome(StrEnum):
    """What the Runner did with one relayed code -- the reason the console shows."""

    WRITTEN = "written"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    UNOPENABLE = "unopenable"


class SignInCodeRelay(BaseModel):
    """One relayed code's outcome, reported on the next heartbeat. Never the code."""

    model_config = ConfigDict(extra="forbid")

    sign_in_id: SignInId
    outcome: SignInCodeOutcome
