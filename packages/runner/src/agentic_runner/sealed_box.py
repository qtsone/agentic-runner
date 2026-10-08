"""The installation's Recipient Key, and the sealed box it opens (PRD issue 48, 22 A4-A7).

One X25519 keypair per **installation** -- a Helm release's replicas share the host Secret
and register the same key, a workstation process is its own installation (issues 46, 47).
The public half is registered at bootstrap (issue 41) and relayed by a platform that did
not mint it; the private half never leaves this state directory, and the plaintext it
opens never leaves process memory.

**The construction**, and why this one. Research 18's precedents are HPKE Base, the age
stanza and libsodium's ``crypto_box_seal`` -- all the same sealed-box shape: an ephemeral
X25519 keypair, one Diffie-Hellman against the recipient's public key, a KDF over the
result, and an AEAD. We build that shape out of primitives both ends already have rather
than shipping a fourth implementation of it:

    ikm   = X25519(ephemeral_private, recipient_public)
    key   = HKDF-SHA256(ikm, salt="", info=SEAL_DOMAIN || ephemeral_public
                                          || recipient_public, length=32)
    body  = AES-256-GCM(key, nonce=0, plaintext, aad=delivery_binding(...))
    blob  = base64(ephemeral_public || body)

The all-zero nonce is safe and is the reason the ephemeral public key is in the KDF info:
the key is derived per sealing from a fresh ephemeral scalar, so it encrypts exactly one
message. The binding triple is the AEAD's associated data, which is what makes a
ciphertext lifted from another ``(contract, slot, recipient key)`` fail the tag check
rather than decrypt to something.

That choice is what lets the **browser** seal with no library at all: Web Crypto does
X25519, HKDF and AES-GCM natively (ADR-0009's constraint is that the console pulls in no
new dependency, and the native platform feature is the smallest way to honour it), and
``frontend/src/lib/sealed-credential.ts`` is the same five lines in TypeScript.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentic_runner.auth_mode import SETUP_TOKEN_REFUSED_RULE, is_setup_token
from agentic_runner.private_state import private_read, private_write
from agentic_runner_contracts.runner_registration import RecipientKey
from agentic_runner_contracts.sealed_credential import (
    RENEWAL_INTERVAL,
    SEAL_DOMAIN,
    OpenedCredential,
    SealedCredential,
    delivery_binding,
    key_fingerprint,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "RECIPIENT_KEY_FILENAME",
    "RecipientKeyPair",
    "RecipientKeyStore",
    "SealedCredentialError",
    "SealedCredentialStream",
    "generate_recipient_key",
    "open_sealed",
    "seal",
]

RECIPIENT_KEY_FILENAME: Final[str] = "recipient-key.json"

_PUBLIC_KEY_BYTES: Final[int] = 32
_KEY_BYTES: Final[int] = 32
_NONCE: Final[bytes] = bytes(12)


class SealedCredentialError(ValueError):
    """A sealed value could not be opened: wrong key, wrong binding, or malformed."""


@dataclass(frozen=True, slots=True)
class RecipientKeyPair:
    """One installation's Recipient Key. ``private_key`` is memory and 0600 disk only."""

    key_id: str
    public_key: str
    private_key: str

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.public_key)


def generate_recipient_key() -> RecipientKeyPair:
    """A fresh X25519 keypair and the id the platform will relay it under.

    The id is random rather than derived from the key: a derived id would let anyone
    holding the public half recompute it, and the id is what a delivered ciphertext is
    bound to -- it has to change on every renewal even in the (impossible, but) case of a
    repeated key.
    """

    private = X25519PrivateKey.generate()
    return RecipientKeyPair(
        key_id=f"rk_{secrets.token_hex(16)}",
        public_key=_b64(private.public_key().public_bytes_raw()),
        private_key=_b64(private.private_bytes_raw()),
    )


def seal(*, public_key: str, binding: bytes, plaintext: str) -> str:
    """Seal one value to a Recipient Key. The Runner's own half of 22 A7's renewal.

    Delivery itself happens in the browser; this exists because a renewal re-seals values
    the Runner already holds, and because a test that seals in Python and opens in Python
    proves nothing about the wire -- the fixture the round-trip test opens is produced by
    the *TypeScript* sealer for exactly that reason.
    """

    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes_raw()
    recipient = _public_key(public_key)
    shared = ephemeral.exchange(recipient)
    key = _derive(shared, ephemeral_public, recipient.public_bytes_raw())
    body = AESGCM(key).encrypt(_NONCE, plaintext.encode("utf-8"), binding)
    return _b64(ephemeral_public + body)


def open_sealed(*, private_key: str, binding: bytes, ciphertext: str) -> str:
    """Open a sealed value, or refuse.

    Every failure is one exception type: a ciphertext bound to another triple, one sealed
    to a different Recipient Key and one that is simply corrupt are indistinguishable to
    the holder, and telling them apart would be an oracle. The caller's answer to all
    three is the same -- refuse the slot and say which reference could not be opened.
    """

    try:
        blob = base64.b64decode(ciphertext, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SealedCredentialError("the sealed value is not base64") from error
    if len(blob) <= _PUBLIC_KEY_BYTES:
        raise SealedCredentialError("the sealed value is too short to carry an ephemeral key")
    ephemeral_public, body = blob[:_PUBLIC_KEY_BYTES], blob[_PUBLIC_KEY_BYTES:]
    private = _private_key(private_key)
    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    key = _derive(shared, ephemeral_public, private.public_key().public_bytes_raw())
    try:
        opened = AESGCM(key).decrypt(_NONCE, body, binding)
    except InvalidTag as error:
        raise SealedCredentialError(
            "the sealed value does not open with this Recipient Key and binding"
        ) from error
    return opened.decode("utf-8")


class RecipientKeyStore:
    """The installation's key on disk, plus the 30-day renewal's two-key window (22 A7).

    A renewal is the Runner's own act on its own schedule -- nothing on the platform side
    triggers it and no funder is asked for anything. It generates a new keypair, keeps the
    old one beside it, re-seals every slot and only then destroys the old private half.
    The window is what makes the sequence safe to interrupt: a Runner that crashes between
    the push and the confirmation re-reads both keys and opens whatever it is handed.

    A **reinstall** is this state file being gone. The new process generates a key with no
    previous half, so nothing it is handed opens -- which is precisely the signal the
    control plane turns into "every slot of that installation is stale, notify each
    funder" (22 A7's last clause).
    """

    def __init__(
        self, state_dir: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._path = state_dir / RECIPIENT_KEY_FILENAME
        self._state: dict[str, object] | None = None
        # Injected so the renewal window is testable without waiting 30 days. Only the
        # *first* generation reads it; every rotation is stamped with the caller's `now`.
        self._clock = clock

    def current(self) -> RecipientKeyPair:
        """This installation's Recipient Key, generated on first call."""

        state = self._load()
        return _pair(state["current"])

    def previous(self) -> RecipientKeyPair | None:
        """The key being retired, while a renewal is in flight."""

        raw = self._load().get("previous")
        return None if raw is None else _pair(raw)

    def rotated_at(self) -> datetime:
        return datetime.fromisoformat(str(self._load()["rotated_at"]))

    def managed_by(self) -> str | None:
        """Who owns rotation: ``None`` for this process, else the installer (issue 46).

        A Helm release's replicas share one key through a Secret, so a rotation by one of
        them would strand the others on a key the platform no longer relays to. There the
        installer rotates, by replacing the Secret, and :meth:`install` hands the new
        key in with the old one kept as ``previous`` -- the same two-key window a
        process-driven renewal walks through.
        """

        raw = self._load().get("managed_by")
        return None if raw is None else str(raw)

    def due(self, now: datetime) -> bool:
        """Whether the renewal window has passed. An installation offline past it is due
        the moment it wakes, which is 22 A7's "re-seals on wake" with no extra state."""

        return (
            self.managed_by() is None
            and self.previous() is None
            and now - self.rotated_at() >= RENEWAL_INTERVAL
        )

    def install(self, pair: RecipientKeyPair, *, managed_by: str, now: datetime) -> bool:
        """Adopt an installer-held key; returns whether anything changed.

        The same key again is a no-op, so a pod restart re-reads its Secret without
        touching the file. A *different* key is the installer's rotation: the key on
        disk becomes ``previous`` so every value sealed to it still opens until the
        re-seal confirms (22 A7), and the process never rotates on its own after this.
        """

        if private_read(self._path) is None:
            # A first boot: adopt outright, rather than let `_load` mint a key nobody
            # registered only to demote it.
            self._save(
                {
                    "current": _raw(pair),
                    "previous": None,
                    "rotated_at": now.isoformat(),
                    "managed_by": managed_by,
                }
            )
            return True
        state = self._load()
        current = _pair(state["current"])
        if current.key_id == pair.key_id and state.get("managed_by") == managed_by:
            return False
        if current.key_id != pair.key_id:
            state["previous"] = state["current"]
            state["current"] = _raw(pair)
            state["rotated_at"] = now.isoformat()
        state["managed_by"] = managed_by
        self._save(state)
        return True

    def rotate(self, now: datetime) -> RecipientKeyPair:
        """Generate the next key and keep the old one openable until every slot confirms."""

        state = self._load()
        fresh = generate_recipient_key()
        state["previous"] = state["current"]
        state["current"] = _raw(fresh)
        state["rotated_at"] = now.isoformat()
        self._save(state)
        return fresh

    def retire_previous(self) -> None:
        """Destroy the old private key. Called only once every slot reports the new id."""

        state = self._load()
        if state.get("previous") is None:
            return
        state["previous"] = None
        self._save(state)

    def _load(self) -> dict[str, object]:
        if self._state is not None:
            return self._state
        raw = private_read(self._path)
        if raw is not None:
            self._state = json.loads(raw)
        else:
            self._state = {
                "current": _raw(generate_recipient_key()),
                "previous": None,
                "rotated_at": self._clock().isoformat(),
            }
            self._save(self._state)
        return self._state

    def _save(self, state: dict[str, object]) -> None:
        private_write(self._path, json.dumps(state).encode("utf-8"))
        self._state = state


def _raw(pair: RecipientKeyPair) -> dict[str, str]:
    return {
        "key_id": pair.key_id,
        "public_key": pair.public_key,
        "private_key": pair.private_key,
    }


def _pair(raw: object) -> RecipientKeyPair:
    if not isinstance(raw, dict):
        raise SealedCredentialError("the Recipient Key state file is malformed")
    return RecipientKeyPair(
        key_id=str(raw["key_id"]),
        public_key=str(raw["public_key"]),
        private_key=str(raw["private_key"]),
    )


def _derive(shared: bytes, ephemeral_public: bytes, recipient_public: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=b"",
        info=SEAL_DOMAIN.encode("utf-8") + ephemeral_public + recipient_public,
    ).derive(shared)


def _public_key(value: str) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(_raw_key(value, "public"))


def _private_key(value: str) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(_raw_key(value, "private"))


def _raw_key(value: str, half: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SealedCredentialError(f"a Recipient Key's {half} half must be base64") from error
    if len(raw) != _PUBLIC_KEY_BYTES:
        raise SealedCredentialError(f"a Recipient Key's {half} half must be 32 bytes")
    return raw


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class SealedCredentialStream:
    """The sealed half of the heartbeat, as one testable step (22 A5, A7, A9).

    The Runner has no long-running heartbeat driver of its own yet (PRD issue 44 owns
    that loop), so this is written as a pure step over one ack: hand it what the control
    plane sent, take back the plaintext it opened and the fields the *next* envelope must
    carry. A loop calls it every 30 s; a test calls it twice with a fake clock.

    Three behaviours fall out of the ack being **authoritative** rather than incremental:

    * **Pull on boot, on activation, on a version change.** The Runner opens a slot only
      when its version or key id differs from what it holds, so a steady state costs no
      X25519 at all and a re-delivery lands at the next beat.
    * **Termination drops the value.** A slot absent from the ack is forgotten here and
      dropped from the resolver -- wiped, not listed (22 A9; the revocation checklist is
      for org-provisioned credentials, which this is not).
    * **Suspension keeps it.** Suspension does not delete the ciphertext row, so the ack
      still carries the slot and nothing above happens.
    """

    def __init__(self, keys: RecipientKeyStore) -> None:
        self._keys = keys
        self._plaintext: dict[tuple[str, str], str] = {}
        self._held: dict[tuple[str, str], tuple[int, str]] = {}
        self._awaiting: set[tuple[str, str]] = set()
        self._opened: list[OpenedCredential] = []

    @property
    def plaintext(self) -> dict[tuple[str, str], str]:
        """``{(contract_id, slot): value}`` -- process memory only, never disk (22 A5)."""

        return dict(self._plaintext)

    def version(self, contract_id: str, slot: str) -> int | None:
        """The delivery version this installation last opened for a slot."""

        held = self._held.get((contract_id, slot))
        return held[0] if held is not None else None

    def apply(self, credentials: Sequence[SealedCredential]) -> list[SealedCredential]:
        """Open what is new, forget what is gone; return what failed to open.

        A failure is not an exception: one slot sealed to a Recipient Key this
        installation no longer has (a half-finished renewal on the other side, or a
        reinstall) must not stop the other slots from opening. The caller records
        Evidence naming each refused slot -- ids only, never a fingerprint of the value.
        """

        refused: list[SealedCredential] = []
        seen: set[tuple[str, str]] = set()
        current = self._keys.current().key_id
        # The confirmation a renewal waits for is read off the ack itself, never carried
        # in memory: a Runner that crashed after `rotate()` persisted but before the push
        # landed comes back with nothing awaited, and an in-memory set would let its first
        # ack -- still all under the old key -- retire the only key that opens it.
        self._awaiting = {
            (str(sealed.contract_id), sealed.slot)
            for sealed in credentials
            if sealed.recipient_key_id != current
        }
        for sealed in credentials:
            key = (str(sealed.contract_id), sealed.slot)
            seen.add(key)
            if self._held.get(key) == (sealed.version, sealed.recipient_key_id):
                continue
            opened = self._open(sealed)
            if opened is None:
                refused.append(sealed)
                continue
            if is_setup_token(opened):
                # Refused on every Runner, user-hosted too (local-agents 04): a long-lived
                # subscription bearer is never a delivered credential. Held, so the same
                # version is not re-opened every beat, but never served or reported opened;
                # a funder's replacement is a new version and opens normally.
                self._held[key] = (sealed.version, sealed.recipient_key_id)
                self._plaintext.pop(key, None)
                _logger.warning(
                    "sealed credential %s/%s refused (%s)",
                    sealed.contract_id,
                    sealed.slot,
                    SETUP_TOKEN_REFUSED_RULE,
                )
                continue
            self._plaintext[key] = opened
            self._held[key] = (sealed.version, sealed.recipient_key_id)
            self._opened.append(
                OpenedCredential(
                    contract_id=sealed.contract_id,
                    slot=sealed.slot,
                    recipient_key_id=sealed.recipient_key_id,
                    version=sealed.version,
                )
            )
        for gone in set(self._held) - seen:
            self._held.pop(gone, None)
            self._plaintext.pop(gone, None)
        self._retire_if_confirmed()
        return refused

    def take_opened(self) -> list[OpenedCredential]:
        """What the next envelope reports as opened, drained (22 A10's Evidence).

        Drained, not read: "opened by Runner" is one event per open, and a beat that
        reported it must not report it again thirty seconds later. A heartbeat that fails
        loses the report rather than the value -- this is Evidence, not state, and the
        plaintext it describes is already held.
        """

        opened, self._opened = self._opened, []
        return opened

    def renew(self, now: datetime) -> tuple[RecipientKey | None, list[SealedCredential]]:
        """22 A7, the Runner-driven renewal: a new key and every slot re-sealed to it.

        Nothing in flight is affected -- the provider values themselves do not change, so
        a running Directive keeps spending the same key while this happens, and a funder
        sees only the fingerprint change. Returns what the next heartbeat envelope carries;
        the old private half is destroyed in :meth:`apply`, once the ack shows every slot
        stored under the new id.

        While a previous key is still held, every call re-announces the current key and
        re-seals whatever the last ack still showed under the old one: the push that
        registers it may never have landed, and nothing else would ever send it again.
        """

        if self._keys.due(now):
            fresh = self._keys.rotate(now)
            pending = set(self._plaintext)
        elif self._keys.previous() is not None:
            fresh = self._keys.current()
            pending = self._awaiting & set(self._plaintext)
        else:
            return None, []
        resealed = [
            SealedCredential(
                contract_id=UUID(contract_id),
                slot=slot,
                recipient_key_id=fresh.key_id,
                version=self._held[(contract_id, slot)][0] + 1,
                ciphertext=seal(
                    public_key=fresh.public_key,
                    binding=delivery_binding(
                        contract_id=contract_id, slot=slot, recipient_key_id=fresh.key_id
                    ),
                    plaintext=value,
                ),
            )
            for (contract_id, slot), value in sorted(self._plaintext.items())
            if (contract_id, slot) in pending
        ]
        # `_held` is deliberately left at what was *opened*, not what was pushed (PRD
        # issue 70): a funder delivery landing between this seal and the push can take
        # the very (version, key) this re-seal names, and marking it held here would skip
        # opening the funder's value. The echo of a re-seal that did land costs one open.
        return RecipientKey(key_id=fresh.key_id, public_key=fresh.public_key), resealed

    def _retire_if_confirmed(self) -> None:
        """Destroy the old private key only once every slot reports the new key id.

        Order matters and is the whole safety property: retiring first would strand any
        slot whose re-seal the control plane never stored, with no key left to open the
        value it still holds.
        """

        if not self._awaiting:
            self._keys.retire_previous()

    def _open(self, sealed: SealedCredential) -> str | None:
        for pair in (self._keys.current(), self._keys.previous()):
            if pair is None or pair.key_id != sealed.recipient_key_id:
                continue
            try:
                return open_sealed(
                    private_key=pair.private_key,
                    binding=sealed.binding(),
                    ciphertext=sealed.ciphertext,
                )
            except SealedCredentialError:
                return None
        return None
