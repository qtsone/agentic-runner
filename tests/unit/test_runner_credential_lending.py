"""Whose values a Runner lends, and the heartbeat's report of them (ADR-0022 §7, rev 3).

What this pins:

* the Runner's own store serves every Contract it runs, whatever host it is and whoever
  pays: ADR-0022 §36's funding limit went with `funding` (Spec: Runners rev 3), and a
  sealed value still wins over the store;
* an ack from a control plane on 3.23, whose manifests still say `funding_party`, is read
  rather than refused;
* the heartbeat lists each Contract the last ack named, every reference with
  usable/unusable, read through the same resolver a Directive uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.credentials import (
    CredentialResolver,
    FakeCredentialStore,
    UnresolvableCredentialReferenceError,
)
from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    CredentialManifest,
    CredentialValidity,
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    IsolationMode,
)

ORG_CONTRACT = "0b6c1c2e-4f0a-4d55-9f43-6a1f7c3e2b10"
CONTRACTOR_CONTRACT = "7d2e9a41-1c3b-4e8f-a0d6-5b9c8e7f6a21"
REFERENCE = "acme_deploy_token"
INSTALLED = "the-organisations-own-key"
SEALED = "the-contractors-sealed-key"


def _runner() -> CredentialResolver:
    return CredentialResolver(store=FakeCredentialStore({REFERENCE: INSTALLED}))


def test_the_runners_own_store_serves_every_contract_it_runs() -> None:
    resolver = _runner()

    for contract_id in (ORG_CONTRACT, CONTRACTOR_CONTRACT):
        resolved = resolver.resolve(contract_id=contract_id, manifest=[REFERENCE])
        assert resolved.for_verb_seam(REFERENCE) == INSTALLED
        assert resolver.usable(contract_id, REFERENCE)


def test_a_sealed_value_wins_over_the_store() -> None:
    resolver = _runner()
    resolver.deliver(CONTRACTOR_CONTRACT, REFERENCE, SEALED)

    sealed = resolver.resolve(contract_id=CONTRACTOR_CONTRACT, manifest=[REFERENCE])
    installed = resolver.resolve(contract_id=ORG_CONTRACT, manifest=[REFERENCE])

    assert sealed.for_verb_seam(REFERENCE) == SEALED
    assert installed.for_verb_seam(REFERENCE) == INSTALLED


def test_a_reference_neither_sealed_nor_installed_fails_closed() -> None:
    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        _runner().resolve(contract_id=ORG_CONTRACT, manifest=["never_installed"])
    assert refusal.value.reason == "not_installed"


@pytest.mark.asyncio
async def test_a_directive_resolves_from_the_store_whatever_its_contract() -> None:
    class _Evidence:
        async def append_evidence(self, *args: Any, **kwargs: Any) -> None:
            return None

    activities = RunnerRalphActivities(
        _Evidence(),  # type: ignore[arg-type]
        credentials=_runner(),
    )
    state = _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id=CONTRACTOR_CONTRACT,
        credential_references=(REFERENCE,),
    )

    credentials = await activities._resolve_credentials("wr-1", state)
    assert credentials is not None
    assert credentials.for_verb_seam(REFERENCE) == INSTALLED


class _Registration:
    server_date = None

    def __init__(self, manifests: list[CredentialManifest]) -> None:
        self.sent: list[HeartbeatEnvelope] = []
        self._manifests = manifests

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            credential_manifests=self._manifests,
        )


def _stream(tmp_path: Path, registration: _Registration) -> Any:
    runner_id = uuid4()
    return service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
            host_party="organisation",
        ),
        isolation=IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path)),
        credentials=_runner(),
        load=service._LoadInterceptor(),
    )


@pytest.mark.asyncio
async def test_the_heartbeat_reports_each_contracts_references_as_usable_or_not(
    tmp_path: Path,
) -> None:
    registration = _Registration(
        [
            CredentialManifest(
                contract_id=UUID(ORG_CONTRACT), references=[REFERENCE, "never_installed"]
            ),
            CredentialManifest(contract_id=UUID(CONTRACTOR_CONTRACT), references=[REFERENCE]),
        ]
    )
    stream = _stream(tmp_path, registration)

    await stream.exchange([])
    await stream.exchange([])

    first, second = registration.sent
    # Nothing to report before an ack has named a Contract.
    assert first.credentials == []
    assert sorted(
        second.credentials, key=lambda entry: (str(entry.contract_id), entry.reference)
    ) == [
        CredentialValidity(contract_id=UUID(ORG_CONTRACT), reference=REFERENCE, valid=True),
        CredentialValidity(
            contract_id=UUID(ORG_CONTRACT), reference="never_installed", valid=False
        ),
        CredentialValidity(contract_id=UUID(CONTRACTOR_CONTRACT), reference=REFERENCE, valid=True),
    ]
    # Names and booleans only: the value never rides the stream.
    assert INSTALLED not in second.model_dump_json()


def test_an_ack_with_nothing_to_ask_omits_the_key_a_318_runner_refuses() -> None:
    def ack(manifests: list[CredentialManifest]) -> dict[str, Any]:
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            credential_manifests=manifests,
        ).model_dump(mode="json")

    assert "credential_manifests" not in ack([])
    manifest = CredentialManifest(contract_id=UUID(ORG_CONTRACT), references=[REFERENCE])
    assert ack([manifest])["credential_manifests"] == [
        {"contract_id": ORG_CONTRACT, "references": [REFERENCE]}
    ]


def test_a_323_ack_naming_who_funds_a_contract_is_still_read() -> None:
    """A control plane on 3.23 still sends `funding_party`; the Runner drops it rather
    than refusing every beat until the control plane is on 3.24."""

    ack = HeartbeatAck.model_validate(
        {
            "runner_id": str(uuid4()),
            "floor_state": "ok",
            "contracts_floor": contracts_version,
            "tag_set_version": 1,
            "accepts_new_directives": True,
            "credential_manifests": [
                {"contract_id": ORG_CONTRACT, "funding_party": "user", "references": [REFERENCE]}
            ],
        }
    )

    assert ack.credential_manifests == [
        CredentialManifest(contract_id=UUID(ORG_CONTRACT), references=[REFERENCE])
    ]
    assert ack.llm_contracts is None
