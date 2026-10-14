"""Whose values a Runner lends, and the heartbeat's report of them (ADR-0022 §7, §36, §42).

What this pins, per acceptance criterion of QTS-1531:

* an Organisation Runner refuses to lend a key installed on it to a contractor-funded
  Contract and lends it to an Organisation-funded one, and a sealed value always wins;
* a personal Runner lends its own values to the Contract it runs, whoever funds it;
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

ORG_FUNDED = "0b6c1c2e-4f0a-4d55-9f43-6a1f7c3e2b10"
CONTRACTOR_FUNDED = "7d2e9a41-1c3b-4e8f-a0d6-5b9c8e7f6a21"
REFERENCE = "acme_deploy_token"
INSTALLED = "the-organisations-own-key"
SEALED = "the-contractors-sealed-key"


def _organisation_runner() -> CredentialResolver:
    return CredentialResolver(
        store=FakeCredentialStore({REFERENCE: INSTALLED}), host_party="organisation"
    )


def test_an_organisation_runner_lends_its_key_only_to_organisation_funded_work() -> None:
    resolver = _organisation_runner()

    lent = resolver.resolve(
        contract_id=ORG_FUNDED, manifest=[REFERENCE], funding_party="organisation"
    )
    assert lent.for_verb_seam(REFERENCE) == INSTALLED

    with pytest.raises(UnresolvableCredentialReferenceError) as refusal:
        resolver.resolve(contract_id=CONTRACTOR_FUNDED, manifest=[REFERENCE], funding_party="user")
    assert refusal.value.reason == "not_installed"
    assert INSTALLED not in str(refusal.value.evidence())


def test_a_sealed_value_wins_whoever_funds_the_contract() -> None:
    resolver = _organisation_runner()
    resolver.deliver(CONTRACTOR_FUNDED, REFERENCE, SEALED)
    resolver.deliver(ORG_FUNDED, REFERENCE, SEALED)

    for contract_id, funding_party in ((CONTRACTOR_FUNDED, "user"), (ORG_FUNDED, "organisation")):
        resolved = resolver.resolve(
            contract_id=contract_id, manifest=[REFERENCE], funding_party=funding_party
        )
        assert resolved.for_verb_seam(REFERENCE) == SEALED


def test_an_organisation_runner_told_no_funding_party_lends_nothing() -> None:
    """A control plane that predates the field sends it blank; blank is never the host."""

    with pytest.raises(UnresolvableCredentialReferenceError):
        _organisation_runner().resolve(
            contract_id=ORG_FUNDED, manifest=[REFERENCE], funding_party=""
        )
    with pytest.raises(UnresolvableCredentialReferenceError):
        _organisation_runner().resolve(contract_id=ORG_FUNDED, manifest=[REFERENCE])


def test_an_account_runner_does_not_lend_to_organisation_funded_work() -> None:
    resolver = CredentialResolver(
        store=FakeCredentialStore({REFERENCE: INSTALLED}), host_party="account"
    )

    with pytest.raises(UnresolvableCredentialReferenceError):
        resolver.resolve(contract_id=ORG_FUNDED, manifest=[REFERENCE], funding_party="organisation")


def test_a_personal_runner_lends_its_own_values_to_the_contract_it_runs() -> None:
    """§42 holds by §2: a personal Runner runs only its person's own Contract."""

    resolver = CredentialResolver(
        store=FakeCredentialStore({REFERENCE: INSTALLED}), host_party="user"
    )

    for funding_party in ("user", "organisation"):
        resolved = resolver.resolve(
            contract_id=ORG_FUNDED, manifest=[REFERENCE], funding_party=funding_party
        )
        assert resolved.for_verb_seam(REFERENCE) == INSTALLED


def test_a_reader_without_a_directive_takes_the_funding_party_from_the_last_ack() -> None:
    resolver = _organisation_runner()
    resolver.fund({ORG_FUNDED: "organisation", CONTRACTOR_FUNDED: "user"})

    assert resolver.usable(ORG_FUNDED, REFERENCE)
    assert not resolver.usable(CONTRACTOR_FUNDED, REFERENCE)
    assert (
        resolver.resolve(contract_id=ORG_FUNDED, manifest=[REFERENCE]).for_verb_seam(REFERENCE)
        == INSTALLED
    )

    # Authoritative: a Contract the next ack leaves out is lent nothing.
    resolver.fund({})
    assert not resolver.usable(ORG_FUNDED, REFERENCE)


@pytest.mark.asyncio
async def test_a_directive_resolves_with_its_own_funding_party() -> None:
    class _Evidence:
        async def append_evidence(self, *args: Any, **kwargs: Any) -> None:
            return None

    activities = RunnerRalphActivities(
        _Evidence(),  # type: ignore[arg-type]
        credentials=_organisation_runner(),
    )
    state = _RuntimeContextState(
        reviewer=None,
        repository="acme/repo",
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="do the work",
        contract_id=CONTRACTOR_FUNDED,
        credential_references=(REFERENCE,),
    )

    with pytest.raises(UnresolvableCredentialReferenceError):
        await activities._resolve_credentials("wr-1", state, funding_party="user")
    credentials = await activities._resolve_credentials("wr-1", state, funding_party="organisation")
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
        credentials=_organisation_runner(),
        load=service._LoadInterceptor(),
    )


@pytest.mark.asyncio
async def test_the_heartbeat_reports_each_contracts_references_as_usable_or_not(
    tmp_path: Path,
) -> None:
    registration = _Registration(
        [
            CredentialManifest(
                contract_id=UUID(ORG_FUNDED),
                funding_party="organisation",
                references=[REFERENCE, "never_installed"],
            ),
            CredentialManifest(
                contract_id=UUID(CONTRACTOR_FUNDED), funding_party="user", references=[REFERENCE]
            ),
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
        CredentialValidity(contract_id=UUID(ORG_FUNDED), reference=REFERENCE, valid=True),
        CredentialValidity(contract_id=UUID(ORG_FUNDED), reference="never_installed", valid=False),
        CredentialValidity(contract_id=UUID(CONTRACTOR_FUNDED), reference=REFERENCE, valid=False),
    ]
    # Names and booleans only: the value never rides the stream.
    assert INSTALLED not in second.model_dump_json()


@pytest.mark.asyncio
async def test_a_sealed_value_makes_the_contractor_funded_reference_usable(
    tmp_path: Path,
) -> None:
    registration = _Registration(
        [
            CredentialManifest(
                contract_id=UUID(CONTRACTOR_FUNDED), funding_party="user", references=[REFERENCE]
            )
        ]
    )
    stream = _stream(tmp_path, registration)
    stream.credentials.deliver(CONTRACTOR_FUNDED, REFERENCE, SEALED)

    await stream.exchange([])
    await stream.exchange([])

    assert registration.sent[-1].credentials == [
        CredentialValidity(contract_id=UUID(CONTRACTOR_FUNDED), reference=REFERENCE, valid=True)
    ]


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
    manifest = CredentialManifest(
        contract_id=UUID(ORG_FUNDED), funding_party="organisation", references=[REFERENCE]
    )
    assert ack([manifest])["credential_manifests"] == [
        {"contract_id": ORG_FUNDED, "funding_party": "organisation", "references": [REFERENCE]}
    ]
