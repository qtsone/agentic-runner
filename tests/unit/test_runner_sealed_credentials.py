"""Sealed Credential Delivery on the Runner (PRD issue 48; map 22 A4, A5, A7, A9).

The round trip these tests exist for crosses a language boundary, so it is asserted
against a **fixture produced by the browser sealer**
(``tests/fixtures/sealed-credential.fixture.json``, copied verbatim from the platform
console's ``frontend/src/lib/sealed-credential.fixture.json``, which its
``sealed-credential.ts`` writes; regenerate it there and copy it here): a Python test that
seals and opens with its own code proves the two halves of one implementation agree, which
is not the claim. The claim is that what a funder's browser produces is what this Runner
opens.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agentic_runner.sealed_box import (
    RecipientKeyStore,
    SealedCredentialError,
    SealedCredentialStream,
    generate_recipient_key,
    open_sealed,
    seal,
)
from agentic_runner_contracts.runner_registration import HeartbeatAck, RecipientKey
from agentic_runner_contracts.sealed_credential import (
    RENEWAL_INTERVAL,
    SealedCredential,
    delivery_binding,
    key_fingerprint,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sealed-credential.fixture.json"

CONTRACT_A = UUID("6f1d4f3a-2b58-4c7e-9a10-0d5e8c3b7f42")
CONTRACT_B = UUID("9c2e7a11-4d3f-4b6c-8e05-1a7f2d9c4b30")


def _fixture() -> dict[str, str]:
    return dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_a_value_sealed_in_the_browser_opens_with_the_runners_private_key() -> None:
    fixture = _fixture()

    opened = open_sealed(
        private_key=fixture["recipient_private_key"],
        binding=delivery_binding(
            contract_id=fixture["contract_id"],
            slot=fixture["slot"],
            recipient_key_id=fixture["recipient_key_id"],
        ),
        ciphertext=fixture["ciphertext"],
    )

    assert opened == fixture["plaintext"]


@pytest.mark.parametrize(
    "swap",
    (
        {"contract_id": str(CONTRACT_B)},
        {"slot": "another_reference"},
        {"recipient_key_id": "rk_00000000000000000000000000000000"},
    ),
)
def test_a_ciphertext_bound_to_another_triple_is_refused(swap: dict[str, str]) -> None:
    """22 A4's binding, one part at a time.

    A sealed value is not a bearer blob: lifting one off another Contract's slot, or off
    the same slot on another installation, fails the AEAD's tag check. Each part of the
    triple is varied on its own so a binding that silently stopped including one of them
    still fails here.
    """

    fixture = _fixture()
    triple = {
        "contract_id": fixture["contract_id"],
        "slot": fixture["slot"],
        "recipient_key_id": fixture["recipient_key_id"],
        **swap,
    }

    with pytest.raises(SealedCredentialError):
        open_sealed(
            private_key=fixture["recipient_private_key"],
            binding=delivery_binding(**triple),
            ciphertext=fixture["ciphertext"],
        )


def test_a_ciphertext_sealed_to_another_recipient_key_is_refused() -> None:
    fixture = _fixture()
    stranger = generate_recipient_key()

    with pytest.raises(SealedCredentialError):
        open_sealed(
            private_key=stranger.private_key,
            binding=delivery_binding(
                contract_id=fixture["contract_id"],
                slot=fixture["slot"],
                recipient_key_id=fixture["recipient_key_id"],
            ),
            ciphertext=fixture["ciphertext"],
        )


def test_the_fingerprint_is_the_same_string_on_both_sides() -> None:
    """22 A4: the console and ``agentic-runner status`` print one thing to compare."""

    fixture = _fixture()

    assert key_fingerprint(fixture["recipient_public_key"]) == key_fingerprint(
        fixture["recipient_public_key"]
    )
    assert len(key_fingerprint(fixture["recipient_public_key"]).split(":")) == 8


def test_the_recipient_key_file_is_generated_once_and_kept_private(tmp_path: Path) -> None:
    store = RecipientKeyStore(tmp_path)

    first = store.current()
    assert RecipientKeyStore(tmp_path).current() == first
    assert (tmp_path / "recipient-key.json").stat().st_mode & 0o077 == 0


def _deliver(
    store: RecipientKeyStore, contract_id: UUID, slot: str, value: str, *, version: int = 1
) -> SealedCredential:
    key = store.current()
    return SealedCredential(
        contract_id=contract_id,
        slot=slot,
        recipient_key_id=key.key_id,
        version=version,
        ciphertext=seal(
            public_key=key.public_key,
            binding=delivery_binding(
                contract_id=contract_id, slot=slot, recipient_key_id=key.key_id
            ),
            plaintext=value,
        ),
    )


def test_the_stream_opens_delivered_slots_and_holds_the_plaintext_in_memory(
    tmp_path: Path,
) -> None:
    store = RecipientKeyStore(tmp_path)
    stream = SealedCredentialStream(store)

    refused = stream.apply(
        [
            _deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a"),
            _deliver(store, CONTRACT_B, "anthropic_api_key", "sk-b"),
        ]
    )

    assert refused == []
    assert stream.plaintext == {
        (str(CONTRACT_A), "anthropic_api_key"): "sk-a",
        (str(CONTRACT_B), "anthropic_api_key"): "sk-b",
    }


def test_an_open_is_reported_once_and_drained(tmp_path: Path) -> None:
    """22 A10's "opened by Runner", the half the next heartbeat carries.

    One event per open: a steady state re-applies the same ack every 30 s and opens
    nothing, so a report that survived the drain would turn one delivery into a Evidence
    event every beat forever.
    """

    store = RecipientKeyStore(tmp_path)
    stream = SealedCredentialStream(store)
    served = [_deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a")]

    stream.apply(served)
    opened = stream.take_opened()

    assert [(item.contract_id, item.slot, item.version) for item in opened] == [
        (CONTRACT_A, "anthropic_api_key", 1)
    ]
    assert stream.take_opened() == []
    stream.apply(served)
    assert stream.take_opened() == []


def test_termination_drops_the_value_and_suspension_leaves_it(tmp_path: Path) -> None:
    """22 A9, on the Runner side. The ack is authoritative, so absence *is* the delete.

    Suspension keeps ciphertext and value, so the ack still carries the slot and nothing
    happens; termination deletes the row, so the next ack omits it and the Runner drops
    the plaintext on that message.
    """

    store = RecipientKeyStore(tmp_path)
    stream = SealedCredentialStream(store)
    both = [
        _deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a"),
        _deliver(store, CONTRACT_B, "anthropic_api_key", "sk-b"),
    ]
    stream.apply(both)

    # Suspension: the control plane keeps both rows, so both stay.
    stream.apply(both)
    assert len(stream.plaintext) == 2

    # Termination of A: its row is gone from the ack.
    stream.apply([both[1]])
    assert stream.plaintext == {(str(CONTRACT_B), "anthropic_api_key"): "sk-b"}


def test_a_ciphertext_this_installation_cannot_open_is_refused_not_raised(
    tmp_path: Path,
) -> None:
    """One unopenable slot must not stop the others: the caller records Evidence for it."""

    store = RecipientKeyStore(tmp_path)
    stream = SealedCredentialStream(store)
    stranger = generate_recipient_key()
    mine = _deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a")
    theirs = SealedCredential(
        contract_id=CONTRACT_B,
        slot="anthropic_api_key",
        recipient_key_id=stranger.key_id,
        version=1,
        ciphertext=seal(
            public_key=stranger.public_key,
            binding=delivery_binding(
                contract_id=CONTRACT_B,
                slot="anthropic_api_key",
                recipient_key_id=stranger.key_id,
            ),
            plaintext="sk-b",
        ),
    )

    refused = stream.apply([mine, theirs])

    assert [(str(item.contract_id), item.slot) for item in refused] == [
        (str(CONTRACT_B), "anthropic_api_key")
    ]
    assert stream.plaintext == {(str(CONTRACT_A), "anthropic_api_key"): "sk-a"}


class _FakeControlPlane:
    """The half of the heartbeat this test needs: it stores what is pushed and echoes it.

    Exactly what ``services/sealed_credentials.record_reseal`` then ``for_installation``
    do over a real database — the echo is the Runner's confirmation, so a fake that did
    not echo would make the renewal test pass for the wrong reason.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], SealedCredential] = {}

    def deliver(self, sealed: SealedCredential) -> None:
        self.rows[(str(sealed.contract_id), sealed.slot)] = sealed

    def beat(self, envelope_resealed: list[SealedCredential]) -> HeartbeatAck:
        for sealed in envelope_resealed:
            self.rows[(str(sealed.contract_id), sealed.slot)] = sealed
        return HeartbeatAck(
            runner_id=UUID("00000000-0000-0000-0000-0000000000aa"),
            floor_state="ok",
            contracts_floor="0.3.0",
            tag_set_version=1,
            accepts_new_directives=True,
            sealed_credentials=sorted(self.rows.values(), key=lambda row: str(row.contract_id)),
        )


def test_the_thirty_day_renewal_reseals_both_slots_before_the_old_key_is_destroyed(
    tmp_path: Path,
) -> None:
    """22 A7, end to end on a fake clock.

    The ordering is the safety property: a Runner that destroyed the old private half
    before every slot confirmed would strand any slot whose re-seal the control plane
    never stored, with nothing left to open the value it still holds.
    """

    day_one = datetime(2026, 1, 1, tzinfo=UTC)
    store = RecipientKeyStore(tmp_path, clock=lambda: day_one)
    stream = SealedCredentialStream(store)
    plane = _FakeControlPlane()
    original = store.current()
    for contract in (CONTRACT_A, CONTRACT_B):
        plane.deliver(_deliver(store, contract, "anthropic_api_key", f"sk-{contract.hex[:4]}"))
    stream.apply(plane.beat([]).sealed_credentials)

    assert stream.renew(day_one) == (None, []), "nothing is due on the first day"
    assert store.previous() is None

    due = day_one + RENEWAL_INTERVAL + timedelta(seconds=1)
    key, resealed = stream.renew(due)

    assert isinstance(key, RecipientKey)
    assert key.key_id != original.key_id
    assert sorted(item.slot for item in resealed) == [
        "anthropic_api_key",
        "anthropic_api_key",
    ]
    assert all(item.recipient_key_id == key.key_id for item in resealed)
    assert all(item.version == 2 for item in resealed)
    # The old private half is still here: no slot has confirmed yet.
    previous = store.previous()
    assert previous is not None and previous.key_id == original.key_id

    # Only one re-seal landed: the other slot is still stored under the old key, which
    # must survive to open it.
    stream.apply(plane.beat(resealed[:1]).sealed_credentials)
    assert store.previous() is not None

    # The ack echoes both re-sealed rows under the new key: now it may go.
    stream.apply(plane.beat(resealed[1:]).sealed_credentials)

    assert store.previous() is None
    assert stream.plaintext == {
        (str(CONTRACT_A), "anthropic_api_key"): f"sk-{CONTRACT_A.hex[:4]}",
        (str(CONTRACT_B), "anthropic_api_key"): f"sk-{CONTRACT_B.hex[:4]}",
    }


def test_a_renewal_interrupted_before_the_push_keeps_the_old_key_and_resends(
    tmp_path: Path,
) -> None:
    """The window 22 A7 relies on survives a crash between ``rotate()`` and the push.

    A fresh process has no memory of what it was waiting for, so the confirmation must be
    read off the ack: an ack still showing the old key id retires nothing, and the next
    beat re-announces the current key with the slot re-sealed to it.
    """

    day_one = datetime(2026, 1, 1, tzinfo=UTC)
    store = RecipientKeyStore(tmp_path, clock=lambda: day_one)
    plane = _FakeControlPlane()
    plane.deliver(_deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a"))
    before_crash = SealedCredentialStream(store)
    before_crash.apply(plane.beat([]).sealed_credentials)
    before_crash.renew(day_one + RENEWAL_INTERVAL + timedelta(seconds=1))
    # ...and the process dies here, the envelope never sent.

    restarted_store = RecipientKeyStore(tmp_path)
    restarted = SealedCredentialStream(restarted_store)
    restarted.apply(plane.beat([]).sealed_credentials)

    assert restarted_store.previous() is not None
    assert restarted.plaintext == {(str(CONTRACT_A), "anthropic_api_key"): "sk-a"}

    key, resealed = restarted.renew(day_one + RENEWAL_INTERVAL + timedelta(seconds=31))
    assert key is not None and key.key_id == restarted_store.current().key_id
    assert [(item.recipient_key_id, item.version) for item in resealed] == [(key.key_id, 2)]

    restarted.apply(plane.beat(resealed).sealed_credentials)
    assert restarted_store.previous() is None
    assert restarted.plaintext == {(str(CONTRACT_A), "anthropic_api_key"): "sk-a"}


def test_a_delivery_taking_the_version_a_pushed_reseal_named_is_still_opened(
    tmp_path: Path,
) -> None:
    """PRD issue 70: the Runner holds what it opened, never what it merely pushed.

    The funder's delivery to the new key lands between the seal and the push and takes
    the (version, key) the stale re-seal names. Were the push counted as held, the
    equality skip would keep the stale value while the console reads it delivered.
    """

    day_one = datetime(2026, 1, 1, tzinfo=UTC)
    store = RecipientKeyStore(tmp_path, clock=lambda: day_one)
    stream = SealedCredentialStream(store)
    stream.apply([_deliver(store, CONTRACT_A, "anthropic_api_key", "stale")])

    key, [pushed] = stream.renew(day_one + RENEWAL_INTERVAL + timedelta(seconds=1))
    assert key is not None
    fresh = _deliver(store, CONTRACT_A, "anthropic_api_key", "fresh", version=pushed.version)
    assert (fresh.version, fresh.recipient_key_id) == (pushed.version, pushed.recipient_key_id)

    stream.apply([fresh])

    assert stream.plaintext == {(str(CONTRACT_A), "anthropic_api_key"): "fresh"}


def test_an_installation_offline_past_the_window_reseals_on_wake(tmp_path: Path) -> None:
    """22 A7's last clause, and the reason ``due`` is a comparison and not a timer."""

    asleep_since = datetime(2026, 1, 1, tzinfo=UTC)
    store = RecipientKeyStore(tmp_path, clock=lambda: asleep_since)
    stream = SealedCredentialStream(store)
    stream.apply([_deliver(store, CONTRACT_A, "anthropic_api_key", "sk-a")])

    key, resealed = stream.renew(asleep_since + timedelta(days=97))

    assert key is not None
    assert len(resealed) == 1


def test_a_reinstall_holds_no_old_key_so_nothing_it_is_handed_opens(tmp_path: Path) -> None:
    """The Runner side of "reinstall = a new Recipient Key with no old key" (22 A7).

    The reinstalled process cannot tell it is a reinstall -- the state directory is gone
    either way -- so what it does is refuse every slot it is handed. Turning that into
    "mark stale, notify each funder" is the control plane's job, because only it can see
    that this Organisation already had an installation.
    """

    old_store = RecipientKeyStore(tmp_path / "before")
    delivered = _deliver(old_store, CONTRACT_A, "anthropic_api_key", "sk-a")

    reinstalled = SealedCredentialStream(RecipientKeyStore(tmp_path / "after"))
    refused = reinstalled.apply([delivered])

    assert refused == [delivered]
    assert reinstalled.plaintext == {}
