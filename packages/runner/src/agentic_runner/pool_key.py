"""The pool's Pool Key on this Runner, and the LLM Keys it opens (Spec: Runners rev 3).

An Organisation's Admin creates the Pool Key in the console, which shows its private half
once, and gives it to every Runner of the pool at install: one chart Secret for all
replicas, a Docker env file, or ``agentic-runner install --pool-key`` on a workstation.
The Runner reads it once at start and holds it in memory.

It is kept apart from the Runner's credential store on purpose. Every value in that store
can be named by a Contract's credential manifest and handed to a verb seam or an MCP
server; the Pool Key must reach nothing but :class:`LlmKeyOpener`.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agentic_runner.llm_proxy import LLM_SLOT_REFERENCES, CredentialSlot, LlmEndpoint
from agentic_runner.private_state import private_read, private_write
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialError, open_sealed
from agentic_runner_contracts.llm_access import PROVIDER_KEY_NAMES
from agentic_runner_contracts.llm_key import LlmKeyRelease, join_llm_key
from agentic_runner_contracts.sealed_credential import key_fingerprint

__all__ = [
    "POOL_KEY_ENV",
    "POOL_KEY_FILENAME",
    "LlmKeyOpener",
    "LlmKeyUnopenableError",
    "PoolKey",
    "PoolKeyError",
    "load_pool_key",
    "parse_pool_key",
    "save_pool_key",
]

POOL_KEY_ENV: Final[str] = "AGENTIC_RUNNER_POOL_KEY"
# Where a workstation install keeps it, 0600 beside the Runner's identity.
POOL_KEY_FILENAME: Final[str] = "pool-key"

_KEY_BYTES: Final[int] = 32


class PoolKeyError(ValueError):
    """The Pool Key given at install is not one."""


class LlmKeyUnopenableError(ValueError):
    """A released LLM Key does not open on this Runner."""


@dataclass(frozen=True, slots=True)
class PoolKey:
    private_key: str = field(repr=False)
    public_key: str

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.public_key)


def parse_pool_key(value: str) -> PoolKey:
    """The X25519 private half the console showed, base64, as a :class:`PoolKey`."""

    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise PoolKeyError(f"{POOL_KEY_ENV} must be the base64 Pool Key the console showed") from (
            error
        )
    if len(raw) != _KEY_BYTES:
        raise PoolKeyError(f"{POOL_KEY_ENV} must be a 32-byte Pool Key")
    public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    return PoolKey(
        private_key=base64.b64encode(raw).decode("ascii"),
        public_key=base64.b64encode(public).decode("ascii"),
    )


def load_pool_key(
    state_dir: Path | None, environ: MutableMapping[str, str] = os.environ
) -> PoolKey | None:
    """The Pool Key from the environment, else the workstation install's file; None if
    the Runner was given none.

    Taken out of the environment as it is read: a child process started without an
    explicit environment would otherwise inherit it.
    """

    value = environ.pop(POOL_KEY_ENV, "").strip()
    if not value and state_dir is not None:
        stored = private_read(state_dir / POOL_KEY_FILENAME)
        value = stored.decode("utf-8").strip() if stored is not None else ""
    return parse_pool_key(value) if value else None


def save_pool_key(state_dir: Path, value: str) -> PoolKey:
    """Check and keep a workstation's Pool Key, for every later start of its Runner."""

    pool_key = parse_pool_key(value)
    private_write(state_dir / POOL_KEY_FILENAME, pool_key.private_key.encode("ascii"))
    return pool_key


class LlmKeyOpener:
    """Opens a released LLM Key into one proxy slot (Spec: Runners rev 3).

    The slot is handed to one Directive's proxy attempt and to nothing else, so the key
    lives exactly as long as that attempt: never in the proxy's slot store, never on
    disk, never in the Agent Runtime's environment.
    """

    def __init__(
        self,
        *,
        pool_key: PoolKey | None,
        recipient_keys: RecipientKeyStore,
        endpoints: Mapping[str, LlmEndpoint] = LLM_SLOT_REFERENCES,
    ) -> None:
        self._pool_key = pool_key
        self._recipient_keys = recipient_keys
        self._endpoints = endpoints

    @property
    def fingerprint(self) -> str | None:
        return self._pool_key.fingerprint if self._pool_key is not None else None

    @property
    def holds_pool_key(self) -> bool:
        return self._pool_key is not None

    def open(self, release: LlmKeyRelease) -> CredentialSlot:
        """The key the two shares make, as a slot for the release's provider.

        One error for every way a release fails to open, as :func:`open_sealed` does: a
        share sealed to an older Pool Key, to another Runner or bound to another key are
        the same answer to the holder, and telling them apart would be an oracle.
        """

        unopenable = f"LLM Key {release.llm_key_id} does not open with this Runner's keys"
        if self._pool_key is None or release.pool_key_fingerprint != self._pool_key.fingerprint:
            raise LlmKeyUnopenableError(unopenable)
        try:
            pool_share = open_sealed(
                private_key=self._pool_key.private_key,
                binding=release.pool_share_binding(),
                ciphertext=release.pool_share,
            )
            platform_share = open_sealed(
                private_key=self._recipient_private_key(release.recipient_key_id),
                binding=release.platform_share_binding(),
                ciphertext=release.platform_share,
            )
            value = join_llm_key(pool_share, platform_share)
        except (SealedCredentialError, ValueError) as error:
            raise LlmKeyUnopenableError(unopenable) from error
        key_name = PROVIDER_KEY_NAMES[release.provider]
        return self._endpoints[key_name].slot(
            reference=key_name, key_id=release.llm_key_id, value=value
        )

    def _recipient_private_key(self, recipient_key_id: str) -> str:
        # The previous key too: the control plane may have sealed its share just before a
        # renewal.
        for pair in (self._recipient_keys.current(), self._recipient_keys.previous()):
            if pair is not None and pair.key_id == recipient_key_id:
                return pair.private_key
        raise SealedCredentialError("the platform share is sealed to another Recipient Key")
