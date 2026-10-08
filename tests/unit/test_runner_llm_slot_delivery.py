"""A delivered API key becomes the Contract's LLM proxy slot (local-agents 04b).

The sealed stream opens a value into the Credential Reference resolver; these pin the
second half -- the references named for a provider's key also fill the proxy's
:class:`SlotStore`, probe-gated, once per value, and are wiped when the ack drops them.
The Directive that then spends the slot is in
``tests/integration/test_runner_auth_mode_directives.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.llm_proxy import (
    CredentialSlot,
    LlmProxy,
    SlotStore,
    llm_slot_references,
)
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream, seal
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    SlotProbe,
)
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")


class _Registration:
    """The control plane's half of the stream: whatever sealed rows it holds, every ack."""

    server_date = None

    def __init__(self) -> None:
        self.rows: list[SealedCredential] = []
        self.sent: list[HeartbeatEnvelope] = []

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            sealed_credentials=list(self.rows),
        )


class _Probe:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.probed: list[CredentialSlot] = []

    async def __call__(self, slot: CredentialSlot) -> bool:
        self.probed.append(slot)
        return self.valid


def _stream(
    tmp_path: Path, registration: _Registration, probe: _Probe, **overrides: Any
) -> service.ControlPlaneStream:
    runner_id = uuid4()
    return service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore(probe=probe)),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path)),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        **overrides,
    )


def _sealed(
    stream: service.ControlPlaneStream, value: str, *, slot: str, version: int = 1
) -> SealedCredential:
    key = stream.sealed._keys.current()
    return SealedCredential(
        contract_id=CONTRACT,
        slot=slot,
        recipient_key_id=key.key_id,
        version=version,
        ciphertext=seal(
            public_key=key.public_key,
            binding=delivery_binding(contract_id=CONTRACT, slot=slot, recipient_key_id=key.key_id),
            plaintext=value,
        ),
    )


@pytest.mark.asyncio
async def test_a_delivered_openai_key_fills_the_contracts_slot_for_codex(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "sk-funder-key", slot="OPENAI_API_KEY")]

    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT)
    assert slot is not None
    assert (slot.provider_name, slot.runtime_kind, slot.auth_style) == (
        "openai",
        "codex_cli",
        "bearer",
    )
    assert (slot.base_url, slot.value) == ("https://api.openai.com/v1", "sk-funder-key")
    # The funder sees which delivery is in service, never the value.
    assert slot.key_id == "OPENAI_API_KEY@v1"
    assert [p.value for p in probe.probed] == ["sk-funder-key"]

    await stream.exchange([])
    [status] = registration.sent[-1].slots
    assert (status.present, status.probe, status.key_id) == (
        True,
        SlotProbe.VALID,
        "OPENAI_API_KEY@v1",
    )
    assert [h.auth_mode for h in registration.sent[-1].harnesses] == []


@pytest.mark.asyncio
async def test_an_anthropic_key_is_presented_as_x_api_key_to_claude(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "sk-ant-api03-key", slot="ANTHROPIC_API_KEY")]

    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT)
    assert slot is not None
    assert (slot.provider_name, slot.runtime_kind) == ("anthropic", "claude_code")
    assert slot.headers()["x-api-key"] == "sk-ant-api03-key"


@pytest.mark.asyncio
async def test_any_other_reference_stays_out_of_the_proxy(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "ghp_token", slot="GITHUB_TOKEN")]

    await stream.exchange([])

    assert not stream.proxy.slots.holds(CONTRACT)
    assert probe.probed == []
    # Still delivered where every reference goes: the resolver, for the verb seams.
    resolved = stream.credentials.resolve(contract_id=str(CONTRACT), manifest=["GITHUB_TOKEN"])
    assert resolved.for_verb_seam("GITHUB_TOKEN") == "ghp_token"


@pytest.mark.asyncio
async def test_a_value_is_probed_once_and_a_refused_one_waits_for_a_new_delivery(
    tmp_path: Path,
) -> None:
    registration, probe = _Registration(), _Probe(valid=False)
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "sk-revoked", slot="OPENAI_API_KEY")]

    await stream.exchange([])
    await stream.exchange([])

    assert not stream.proxy.slots.holds(CONTRACT)
    assert len(probe.probed) == 1, "a steady ack spends no probe"
    assert registration.sent[-1].slots[0].probe == SlotProbe.INVALID

    probe.valid = True
    registration.rows = [_sealed(stream, "sk-replacement", slot="OPENAI_API_KEY", version=2)]
    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT)
    assert slot is not None and slot.value == "sk-replacement"
    assert slot.key_id == "OPENAI_API_KEY@v2"


@pytest.mark.asyncio
async def test_a_slot_the_ack_no_longer_carries_is_wiped_from_the_proxy(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "sk-funder-key", slot="OPENAI_API_KEY")]
    await stream.exchange([])
    assert stream.proxy.slots.holds(CONTRACT)

    registration.rows = []
    await stream.exchange([])

    assert not stream.proxy.slots.holds(CONTRACT)
    assert stream.proxy.slots.statuses() == []


@pytest.mark.asyncio
async def test_the_operator_points_a_provider_at_its_own_gateway(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(
        tmp_path,
        registration,
        probe,
        llm_slots=llm_slot_references({"openai": "http://gateway.internal/v1"}),
    )
    registration.rows = [_sealed(stream, "sk-funder-key", slot="OPENAI_API_KEY")]

    await stream.exchange([])

    [probed] = probe.probed
    assert probed.base_url == "http://gateway.internal/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("withdrawn", "kept", "kept_value"),
    [
        ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "sk-ant"),
        ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "sk-openai"),
    ],
)
async def test_withdrawing_one_of_two_llm_keys_leaves_only_the_other_serving(
    tmp_path: Path, withdrawn: str, kept: str, kept_value: str
) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    values = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-openai"}
    registration.rows = [_sealed(stream, value, slot=ref) for ref, value in values.items()]
    await stream.exchange([])

    registration.rows = [_sealed(stream, kept_value, slot=kept)]
    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT)
    assert slot is not None
    assert (slot.reference, slot.value) == (kept, kept_value)
    assert slot.key_id == f"{kept}@v1"
