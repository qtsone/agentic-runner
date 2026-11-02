"""A person's User Key: wrapped for their passkeys, handed to their Runner at ``login``
(Spec: Agent Guardrails rev 7, S3, S5).

The User Key is the holder key of a member's own Secrets, as the Organisation Key is of
the Organisation's: an X25519 key pair generated in the person's browser, whose private
half the control plane never sees. The control plane keeps its public half, and the
private half once per passkey, **wrapped** under a key the browser derives from that
passkey's WebAuthn PRF output. A passkey authenticates nothing here (the Clerk sign-in
does that): it is the one thing the control plane never holds, so it is what opens the
User Key, and only in that browser.

**Enrolment.** When the person confirms ``agentic-runner login``, the browser unwraps the
User Key with their passkey and seals the private half to the new Runner's Recipient Key,
bound to the person, the User Key and that Recipient Key (:func:`enrolment_binding`). The
control plane relays it as :attr:`runner_login.LoginResponse.user_key`; the Runner opens
it, checks it is the key it says it is, and keeps it beside its identity. Every Secret of
theirs then opens there with no retyping.

**Wrap format** (the browser's; written down here so the fixture holds both sides to it):
AES-256-GCM under ``HKDF-SHA256(prf_output, salt="", info=binding)``, the binding
:func:`passkey_wrap_binding` as the associated data too, the 12-byte nonce first, base64.
The PRF input is :data:`PASSKEY_PRF_INPUT`, the same for every passkey, so any of a
person's passkeys can be asked for it with no lookup first.

``fixtures/user-key.fixture.json`` is one User Key wrapped for a passkey, sealed to a
Runner, and a member Secret sealed to it; the Runner's tests open it and the console's
tests read the same file.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from agentic_runner_contracts.llm_key import Fingerprint, SealedShare
from agentic_runner_contracts.sealed_credential import KeyId

__all__ = [
    "PASSKEY_PRF_INPUT",
    "PASSKEY_WRAP_DOMAIN",
    "USER_KEY_DOMAIN",
    "SealedUserKey",
    "enrolment_binding",
    "passkey_wrap_binding",
]

USER_KEY_DOMAIN: Final[str] = "agentic-os/user-key/v1"
PASSKEY_WRAP_DOMAIN: Final[str] = "agentic-os/user-key-wrap/v1"
# What the browser asks every passkey's PRF to evaluate (WebAuthn `prf.eval.first`).
PASSKEY_PRF_INPUT: Final[bytes] = b"agentic-os/passkey-prf/v1"

_KEY_BYTES: Final[int] = 32

PublicKey = Annotated[str, StringConstraints(min_length=44, max_length=44)]


def enrolment_binding(
    *, user_profile_id: str, user_key_fingerprint: str, recipient_key_id: str
) -> bytes:
    """The AEAD associated data of a User Key sealed to one Runner's Recipient Key.

    Bound to the person, so a login relayed to another person's Runner fails the tag; to
    the User Key, so the Runner holds the key it reports; and to the Recipient Key the
    login presented.
    """

    return "|".join(
        (USER_KEY_DOMAIN, "enrolment", user_profile_id, user_key_fingerprint, recipient_key_id)
    ).encode("utf-8")


def passkey_wrap_binding(
    *, user_profile_id: str, user_key_fingerprint: str, credential_id: str
) -> bytes:
    """The HKDF info and AEAD associated data of a User Key wrapped for one passkey.

    ``credential_id`` is the passkey's WebAuthn credential id, base64url: a wrap moved onto
    another passkey's row, another person's or another User Key's does not open.
    """

    return "|".join(
        (PASSKEY_WRAP_DOMAIN, user_profile_id, user_key_fingerprint, credential_id)
    ).encode("utf-8")


class SealedUserKey(BaseModel):
    """A User Key's private half, sealed in the person's browser to one Runner's Recipient
    Key. The public half and its fingerprint travel beside it, for the Runner to check."""

    model_config = ConfigDict(extra="forbid")

    public_key: PublicKey
    fingerprint: Fingerprint
    recipient_key_id: KeyId
    sealed_private: SealedShare

    @field_validator("public_key")
    @classmethod
    def _x25519(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("a User Key's public half must be base64") from error
        if len(raw) != _KEY_BYTES:
            raise ValueError("a User Key's public half is 32 bytes")
        return value

    def binding(self, user_profile_id: str) -> bytes:
        return enrolment_binding(
            user_profile_id=user_profile_id,
            user_key_fingerprint=self.fingerprint,
            recipient_key_id=self.recipient_key_id,
        )
