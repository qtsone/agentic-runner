"""The operator's User Key on their own Runner (Spec: Agent Guardrails rev 7, S3, S5).

A person's Runner gets their User Key once, at ``agentic-runner login``: the browser that
confirmed the login unwrapped it with the person's passkey and sealed it to this Runner's
Recipient Key (:class:`~agentic_runner_contracts.user_key.SealedUserKey`). The Runner
opens it, checks the private half makes the public half and fingerprint it came with, and
keeps it 0600 beside its identity, apart from the credential store for the reason
:mod:`.pool_key` gives: no Contract's manifest may name it.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agentic_runner.pool_key import PoolKey
from agentic_runner.private_state import private_read, private_write
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialError, open_sealed
from agentic_runner_contracts.user_key import SealedUserKey

__all__ = [
    "USER_KEY_FILENAME",
    "UserKeyError",
    "load_user_key",
    "open_user_key",
    "save_user_key",
]

USER_KEY_FILENAME: Final[str] = "user-key"

_KEY_BYTES: Final[int] = 32


class UserKeyError(ValueError):
    """The User Key handed over at login does not open here, or is not the key it says."""


def _holder_key(private_key: str) -> PoolKey:
    try:
        raw = base64.b64decode(private_key.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise UserKeyError("the User Key is not base64") from error
    if len(raw) != _KEY_BYTES:
        raise UserKeyError("the User Key is not 32 bytes")
    public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    # The same shape as the Organisation Key's: both are X25519 holder keys.
    return PoolKey(
        private_key=base64.b64encode(raw).decode("ascii"),
        public_key=base64.b64encode(public).decode("ascii"),
    )


def open_user_key(
    sealed: SealedUserKey, *, user_profile_id: str, recipient_keys: RecipientKeyStore
) -> PoolKey:
    """The User Key ``sealed`` carries, opened with this Runner's Recipient Key.

    Refused unless it opens under the binding to this person and this Recipient Key, and
    its private half makes the public half and fingerprint beside it: the fingerprint is
    what this Runner reports and what routing trusts.
    """

    pair = next(
        (
            candidate
            for candidate in (recipient_keys.current(), recipient_keys.previous())
            if candidate is not None and candidate.key_id == sealed.recipient_key_id
        ),
        None,
    )
    if pair is None:
        raise UserKeyError("the User Key is sealed to another Recipient Key")
    try:
        private_key = open_sealed(
            private_key=pair.private_key,
            binding=sealed.binding(user_profile_id),
            ciphertext=sealed.sealed_private,
        )
    except SealedCredentialError as error:
        raise UserKeyError("the User Key does not open with this Runner's Recipient Key") from (
            error
        )
    key = _holder_key(private_key)
    if key.public_key != sealed.public_key or key.fingerprint != sealed.fingerprint:
        raise UserKeyError("the User Key is not the key its fingerprint names")
    return key


def save_user_key(state_dir: Path, key: PoolKey) -> None:
    private_write(state_dir / USER_KEY_FILENAME, key.private_key.encode("ascii"))


def load_user_key(state_dir: Path | None) -> PoolKey | None:
    """The User Key ``login`` kept, or None for a Runner given none."""

    if state_dir is None:
        return None
    stored = private_read(state_dir / USER_KEY_FILENAME)
    return _holder_key(stored.decode("utf-8")) if stored is not None else None
