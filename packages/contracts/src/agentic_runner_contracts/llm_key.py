"""A pool's LLM Keys: the two shares, their bindings and the release (Spec: Runners rev 3).

An Organisation (or an Account) stores a provider's API key once for its pool of Runners.
The admin's browser splits it into two shares -- a random share and the key XOR that
share -- and seals the random one to the pool's **Pool Key**, an X25519 key pair whose
private half only the pool's Runners hold. QTS keeps the sealed share and the other share,
which it encrypts with a platform secret held outside its database. Neither share alone
says anything about the key.

At Directive start a pool Runner asks the Runner API for the LLM Key its ``llm_access``
names. QTS answers with :class:`LlmKeyRelease`: the sealed share as stored, and its own
share sealed to that Runner's Recipient Key. The Runner opens the first with the Pool Key
and the second with its Recipient Key, joins them, and holds the key in memory for that
one Directive. So a copy of QTS's data opens nothing, a leaked Pool Key opens nothing
without QTS's share, and QTS alone cannot open a key.

Both shares travel in the console's existing sealed box (X25519, HKDF-SHA256, AES-GCM:
``agentic_runner.sealed_box`` and the console's ``sealed-credential.ts``) unchanged. What
is new is the AEAD binding, under its own domain :data:`LLM_KEY_DOMAIN`, as the OAuth
code's is: a sealed share can never open as a sealed credential or a code, nor one LLM
Key's share as another's.

``fixtures/llm-key.fixture.json`` is one key split, sealed and bound by these rules. The
contracts tests open it here, and the console's tests read the same file, so the browser
and the Runner cannot drift apart. Dependency-free, like :mod:`.sealed_credential`: the
sealed box itself lives in the Runner.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

from agentic_runner_contracts.llm_access import LlmProvider
from agentic_runner_contracts.sealed_credential import CIPHERTEXT_MAX_CHARS, KeyId

__all__ = [
    "LLM_KEY_DOMAIN",
    "Fingerprint",
    "LlmKeyRelease",
    "LlmKeyReleaseRequest",
    "join_llm_key",
    "platform_share_binding",
    "pool_share_binding",
    "split_llm_key",
]

LLM_KEY_DOMAIN: Final[str] = "agentic-os/llm-key/v1"

# `sealed_credential.key_fingerprint`'s shape: the Pool Key's is computed the same way, so
# the console shows both the way an operator already compares them.
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{4}(?::[0-9a-f]{4}){7}$")]

# No `|`, so the joined binding cannot be read two ways.
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]

SealedShare = Annotated[str, StringConstraints(min_length=1, max_length=CIPHERTEXT_MAX_CHARS)]


def pool_share_binding(*, pool_id: str, llm_key_id: str, pool_key_fingerprint: str) -> bytes:
    """The AEAD associated data of the share sealed to the Pool Key.

    Bound to the pool, the LLM Key and the Pool Key's fingerprint: a share lifted onto
    another key's row, or kept across a new Pool Key, fails the tag check.
    """

    return "|".join(
        (LLM_KEY_DOMAIN, "pool-share", pool_id, llm_key_id, pool_key_fingerprint)
    ).encode("utf-8")


def platform_share_binding(*, llm_key_id: str, recipient_key_id: str) -> bytes:
    """The AEAD associated data of QTS's share, sealed to one Runner's Recipient Key."""

    return "|".join((LLM_KEY_DOMAIN, "platform-share", llm_key_id, recipient_key_id)).encode(
        "utf-8"
    )


def split_llm_key(value: str) -> tuple[str, str]:
    """``(pool_share, platform_share)``, each base64: a random share, and the key XOR it.

    The console splits in the browser; this is the same rule, for the tests and for any
    other client of the contracts.
    """

    raw = value.encode("utf-8")
    pool = secrets.token_bytes(len(raw))
    platform = bytes(left ^ right for left, right in zip(raw, pool, strict=True))
    return _b64(pool), _b64(platform)


def join_llm_key(pool_share: str, platform_share: str) -> str:
    """The key the two shares make, or ``ValueError`` when they cannot be one key's."""

    pool = _unb64(pool_share)
    platform = _unb64(platform_share)
    if not pool or len(pool) != len(platform):
        raise ValueError("the two shares are not one LLM Key's: their lengths differ")
    try:
        return bytes(left ^ right for left, right in zip(pool, platform, strict=True)).decode(
            "utf-8"
        )
    except UnicodeDecodeError as error:
        raise ValueError("the two shares are not one LLM Key's") from error


class LlmKeyReleaseRequest(BaseModel):
    """What a Runner sends, with its Directive token, to have one LLM Key released.

    The token says which Runner and which Directive; ``directive_id`` names the same
    Directive so the control plane can check that the two agree.
    """

    model_config = ConfigDict(extra="forbid")

    directive_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    # The Pool Key this Runner holds: a release sealed for another one would not open.
    pool_key_fingerprint: Fingerprint


class LlmKeyRelease(BaseModel):
    """The two shares of one LLM Key, sealed: one to the Pool Key, one to this Runner.

    Ciphertext and ids only. ``provider`` is the key's provider, which the Runner checks
    against the Directive's Agent Runtime before it serves the key.
    """

    model_config = ConfigDict(extra="forbid")

    llm_key_id: Identifier
    pool_id: Identifier
    provider: LlmProvider
    pool_key_fingerprint: Fingerprint
    pool_share: SealedShare
    recipient_key_id: KeyId
    platform_share: SealedShare

    def pool_share_binding(self) -> bytes:
        return pool_share_binding(
            pool_id=self.pool_id,
            llm_key_id=self.llm_key_id,
            pool_key_fingerprint=self.pool_key_fingerprint,
        )

    def platform_share_binding(self) -> bytes:
        return platform_share_binding(
            llm_key_id=self.llm_key_id, recipient_key_id=self.recipient_key_id
        )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("an LLM Key share must be base64") from error
