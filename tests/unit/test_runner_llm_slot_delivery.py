"""An LLM key reaches the proxy: delivered (local-agents 04b) or from the host's own
store (Spec: Runners rev 3), and the heartbeat reports it.

The sealed stream opens a value into the Credential Reference resolver; these pin the
second half -- the references named for a provider's key also fill the proxy's
:class:`SlotStore`, one slot per Contract and provider, probe-gated, once per value, and
are wiped when the ack drops them. A key put straight into the Runner's own store fills
the provider's host slot for every Contract, and the LLM access report says so once an
ack has named the Contracts. The Directive that then spends the slot is in
``tests/integration/test_runner_auth_mode_directives.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import (
    CredentialResolver,
    EmptyCredentialStore,
    FakeCredentialStore,
    HostCredentialStore,
)
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
    ContractLlmAccess,
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    InstallTarget,
    ProviderKeyReport,
    RunnerInstall,
    SlotProbe,
    SubscriptionRight,
)
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_CONTRACT = UUID("22222222-2222-4222-8222-222222222222")


class _Registration:
    """The control plane's half of the stream: whatever sealed rows it holds, every ack."""

    server_date = None

    def __init__(self) -> None:
        self.rows: list[SealedCredential] = []
        self.sent: list[HeartbeatEnvelope] = []
        # None is a control plane below 3.25, which names no Contract terms.
        self.llm_contracts: list[ContractLlmTerms] | None = None

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            sealed_credentials=list(self.rows),
            llm_contracts=self.llm_contracts,
        )


class _Probe:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.probed: list[CredentialSlot] = []

    async def __call__(self, slot: CredentialSlot) -> bool:
        self.probed.append(slot)
        return self.valid


def _stream(
    tmp_path: Path,
    registration: _Registration,
    probe: _Probe,
    *,
    store: HostCredentialStore | None = None,
    **overrides: Any,
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
        credentials=CredentialResolver(store=store or EmptyCredentialStore()),
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

    slot = stream.proxy.slots.resolve(CONTRACT, "openai")
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

    slot = stream.proxy.slots.resolve(CONTRACT, "anthropic")
    assert slot is not None
    assert (slot.provider_name, slot.runtime_kind) == ("anthropic", "claude_code")
    assert slot.headers()["x-api-key"] == "sk-ant-api03-key"


@pytest.mark.asyncio
async def test_any_other_reference_stays_out_of_the_proxy(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "ghp_token", slot="GITHUB_TOKEN")]

    await stream.exchange([])

    assert not stream.proxy.slots.holds(CONTRACT, None)
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

    assert not stream.proxy.slots.holds(CONTRACT, None)
    assert len(probe.probed) == 1, "a steady ack spends no probe"
    assert registration.sent[-1].slots[0].probe == SlotProbe.INVALID

    probe.valid = True
    registration.rows = [_sealed(stream, "sk-replacement", slot="OPENAI_API_KEY", version=2)]
    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT, "openai")
    assert slot is not None and slot.value == "sk-replacement"
    assert slot.key_id == "OPENAI_API_KEY@v2"


@pytest.mark.asyncio
async def test_a_slot_the_ack_no_longer_carries_is_wiped_from_the_proxy(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [_sealed(stream, "sk-funder-key", slot="OPENAI_API_KEY")]
    await stream.exchange([])
    assert stream.proxy.slots.holds(CONTRACT, "openai")

    registration.rows = []
    await stream.exchange([])

    assert not stream.proxy.slots.holds(CONTRACT, None)
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

    providers = {"OPENAI_API_KEY": "openai", "ANTHROPIC_API_KEY": "anthropic"}
    assert stream.proxy.slots.resolve(CONTRACT, providers[withdrawn]) is None
    slot = stream.proxy.slots.resolve(CONTRACT, providers[kept])
    assert slot is not None
    assert (slot.reference, slot.value) == (kept, kept_value)
    assert slot.key_id == f"{kept}@v1"


@pytest.mark.asyncio
async def test_a_contract_delivered_both_keys_serves_each_harness_its_own(tmp_path: Path) -> None:
    """Rev 3: one slot per Contract and provider, so neither key hides the other."""

    registration, probe = _Registration(), _Probe()
    stream = _stream(tmp_path, registration, probe)
    registration.rows = [
        _sealed(stream, "sk-openai", slot="OPENAI_API_KEY"),
        _sealed(stream, "sk-ant-api03-key", slot="ANTHROPIC_API_KEY"),
    ]

    await stream.exchange([])

    openai = stream.proxy.slots.resolve(CONTRACT, "openai")
    anthropic = stream.proxy.slots.resolve(CONTRACT, "anthropic")
    assert openai is not None and openai.value == "sk-openai"
    assert anthropic is not None and anthropic.value == "sk-ant-api03-key"


# ------------------------------------------------------------------ the host's own store


@pytest.mark.asyncio
async def test_a_key_put_straight_into_the_store_reaches_the_proxy_for_every_contract(
    tmp_path: Path,
) -> None:
    """`credential set`, the chart's Secret, a mounted folder or `sign-in`: one store, read
    by the provider's key name, serving every Contract this Runner runs."""

    registration, probe = _Registration(), _Probe()
    store = FakeCredentialStore({"ANTHROPIC_API_KEY": "sk-ant-api03-host"})
    stream = _stream(tmp_path, registration, probe, store=store)

    await stream.exchange([])
    await stream.exchange([])

    for contract_id in (CONTRACT, OTHER_CONTRACT):
        slot = stream.proxy.slots.resolve(contract_id, "anthropic", host_key_only=True)
        assert slot is not None and slot.value == "sk-ant-api03-host"
    assert stream.proxy.slots.resolve(CONTRACT, "openai") is None
    assert [p.value for p in probe.probed] == ["sk-ant-api03-host"], "probed once per value"

    store.put("ANTHROPIC_API_KEY", "sk-ant-api03-rotated")
    await stream.exchange([])
    slot = stream.proxy.slots.resolve(CONTRACT, "anthropic")
    assert slot is not None and slot.value == "sk-ant-api03-rotated"


@pytest.mark.asyncio
async def test_a_refused_store_key_is_probed_again_after_a_while(tmp_path: Path) -> None:
    """The probe says no on a network fault too, and nobody re-delivers a store key: a
    refused one is retried after `HOST_KEY_REPROBE_SECONDS`, not left out until restart."""

    registration, probe = _Registration(), _Probe(valid=False)
    now = [1_000.0]
    store = FakeCredentialStore({"OPENAI_API_KEY": "sk-host"})
    stream = _stream(tmp_path, registration, probe, store=store, monotonic=lambda: now[0])

    await stream.exchange([])
    await stream.exchange([])
    assert len(probe.probed) == 1
    assert not stream.proxy.slots.holds_host("openai")

    probe.valid = True
    now[0] += service.HOST_KEY_REPROBE_SECONDS
    await stream.exchange([])

    assert len(probe.probed) == 2
    assert stream.proxy.slots.holds_host("openai")
    now[0] += service.HOST_KEY_REPROBE_SECONDS
    await stream.exchange([])
    assert len(probe.probed) == 2, "a key in service is not probed again"


@pytest.mark.asyncio
async def test_a_key_taken_out_of_the_store_stops_serving(tmp_path: Path) -> None:
    registration, probe = _Registration(), _Probe()
    store = FakeCredentialStore({"OPENAI_API_KEY": "sk-host"})
    stream = _stream(tmp_path, registration, probe, store=store)
    await stream.exchange([])
    assert stream.proxy.slots.holds_host("openai")

    store._values.clear()
    await stream.exchange([])

    assert not stream.proxy.slots.holds_host("openai")
    assert stream.proxy.slots.host_probe("openai") is None


@pytest.mark.asyncio
async def test_the_llm_access_report_names_the_stores_keys_for_each_contract_served(
    tmp_path: Path,
) -> None:
    registration, probe = _Registration(), _Probe()
    probe_valid_for = {"sk-host-openai"}

    async def by_value(slot: CredentialSlot) -> bool:
        probe.probed.append(slot)
        return slot.value in probe_valid_for

    store = FakeCredentialStore(
        {"OPENAI_API_KEY": "sk-host-openai", "ANTHROPIC_API_KEY": "sk-ant-typo"}
    )
    stream = _stream(tmp_path, registration, probe, store=store)
    stream.proxy.slots._probe = by_value
    registration.llm_contracts = [
        ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.NO_RIGHT),
        ContractLlmTerms(contract_id=OTHER_CONTRACT, subscription=SubscriptionRight.NO_RIGHT),
    ]

    await stream.exchange([])
    await stream.exchange([])

    keys = [
        ProviderKeyReport(provider="anthropic", held=True, probe=SlotProbe.INVALID),
        ProviderKeyReport(provider="openai", held=True, probe=SlotProbe.VALID),
    ]
    assert registration.sent[-1].llm_access == [
        ContractLlmAccess(contract_id=CONTRACT, keys=keys),
        ContractLlmAccess(contract_id=OTHER_CONTRACT, keys=keys),
    ]
    # Names, booleans and probes: no value rides the stream.
    body = registration.sent[-1].model_dump_json()
    assert "sk-host-openai" not in body and "sk-ant-typo" not in body


@pytest.mark.asyncio
async def test_no_report_and_no_install_until_an_ack_names_the_contracts(tmp_path: Path) -> None:
    """A control plane below 3.25 refuses the two fields, so the Runner waits for an ack
    that carries `llm_contracts` -- the sign that its control plane parses them."""

    registration, probe = _Registration(), _Probe()
    install = RunnerInstall(target=InstallTarget.DOCKER, hostname="3f2a1b4c5d6e")
    stream = _stream(tmp_path, registration, probe, install=install)

    await stream.exchange([])
    await stream.exchange([])
    old = registration.sent[-1].model_dump(mode="json")
    assert "llm_access" not in old and "install" not in old

    registration.llm_contracts = []
    await stream.exchange([])
    await stream.exchange([])
    new = registration.sent[-1].model_dump(mode="json")
    assert new["llm_access"] == []
    assert new["install"] == {"target": "docker", "hostname": "3f2a1b4c5d6e"}


@pytest.mark.asyncio
async def test_the_acks_contract_terms_are_kept_for_sign_in(tmp_path: Path) -> None:
    from agentic_runner.llm_contracts import load_llm_contracts

    registration, probe = _Registration(), _Probe()
    state_dir = tmp_path / "state"
    stream = _stream(tmp_path, registration, probe, state_dir=state_dir)
    assert load_llm_contracts(state_dir) is None

    terms = [ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.ALLOWED)]
    registration.llm_contracts = terms
    await stream.exchange([])

    assert load_llm_contracts(state_dir) == terms
    assert oct((state_dir / "llm-contracts.json").stat().st_mode & 0o777) == "0o600"
