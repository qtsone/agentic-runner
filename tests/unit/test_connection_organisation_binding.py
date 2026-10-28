"""A connection serves only its own Organisation's Contracts (Spec: Runners rev 14, QTS-2252).

One process holds every connected Organisation's credentials, harness roots and Workspaces,
so each connection's Worker is bound to its Organisation. What this pins:

* a Directive delivered on connection A that names a Contract of Organisation B -- or a
  Contract the ack does not place in any Organisation -- is refused, not retryable, with an
  Evidence Event and before any Workspace path is chosen; on connection B it is served;
* a Workspace path on the input outside the Contract's own Workspaces is refused too;
* a triage turn on connection A spends none of B's LLM slot;
* a Runner on the single namespace it registered in (no bound Organisation) keeps today's
  behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from agentic_runner.activities import RunnerRalphActivities
from agentic_runner.connections import contract_refusal
from agentic_runner.triage_activities import RunnerTriageActivities
from agentic_runner_contracts import activity_io as contracts
from agentic_runner_contracts.guardrails import Guardrails
from agentic_runner_contracts.runner_registration import ContractLlmTerms, SubscriptionRight
from agentic_runner_contracts.runtime_context import WorkerRuntimeContext

ORG_A, ORG_B = UUID(int=0xA), UUID(int=0xB)
CONTRACT_A, CONTRACT_B, CONTRACT_ORPHAN = uuid4(), uuid4(), uuid4()
TERMS = [
    ContractLlmTerms(
        contract_id=CONTRACT_A, subscription=SubscriptionRight.NO_RIGHT, organisation_id=ORG_A
    ),
    ContractLlmTerms(
        contract_id=CONTRACT_B, subscription=SubscriptionRight.NO_RIGHT, organisation_id=ORG_B
    ),
    ContractLlmTerms(contract_id=CONTRACT_ORPHAN, subscription=SubscriptionRight.NO_RIGHT),
]


class _Client:
    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.evidence.append({"work_record_id": work_record_id, "payload": dict(payload)})
        return {}


class _Resolver:
    def __init__(self, contract_id: UUID) -> None:
        self._contract_id = contract_id

    async def resolve(self, work_record_id: str, *, agent_id: str | None = None) -> Any:
        return WorkerRuntimeContext(
            work_record_id=work_record_id,
            work_branch="agent/wr-builder",
            guardrails=Guardrails(),
            cli_kind="codex_cli",
            repo="acme/app",
            base_branch="main",
            contract_id=str(self._contract_id),
            task_queue="runner.r-1",
            worker_secret_refs=[],
            product_verifier_command_source={
                "source": "work_record_payload",
                "available": True,
                "metadata": {"command": "uv run pytest"},
            },
        )


def _activities(
    client: _Client,
    *,
    organisation: UUID | None,
    contract_id: UUID,
    workspace_root: Path,
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,
        runtime_context_resolver=_Resolver(contract_id),  # type: ignore[arg-type]
        workspace_root=workspace_root,
        llm_contracts=lambda: TERMS,
        connection_organisation=lambda: organisation,
    )


@pytest.mark.parametrize(
    ("contract_id", "reason"),
    [
        (CONTRACT_B, "contract_of_another_organisation"),
        (CONTRACT_ORPHAN, "contract_not_served"),
        (uuid4(), "contract_not_served"),
    ],
)
@pytest.mark.asyncio
async def test_a_directive_on_connection_a_naming_another_contract_is_refused_with_evidence(
    tmp_path: Path, contract_id: UUID, reason: str
) -> None:
    client = _Client()
    activities = _activities(
        client, organisation=ORG_A, contract_id=contract_id, workspace_root=tmp_path
    )

    with pytest.raises(ApplicationError) as refused:
        await activities._runtime_state("wr-1")

    assert refused.value.non_retryable
    assert refused.value.type == reason
    [evidence] = client.evidence
    assert evidence["work_record_id"] == "wr-1"
    assert evidence["payload"] == {
        "event": "directive.routing_refused",
        "reason": reason,
        "organisation_id": str(ORG_A),
        "contract_id": str(contract_id),
    }
    assert list(tmp_path.iterdir()) == [], "no Workspace was chosen or made"


@pytest.mark.asyncio
async def test_the_same_directive_on_its_own_organisations_connection_is_served(
    tmp_path: Path,
) -> None:
    client = _Client()
    activities = _activities(
        client, organisation=ORG_B, contract_id=CONTRACT_B, workspace_root=tmp_path
    )

    state = await activities._runtime_state("wr-1")

    assert state.contract_id == str(CONTRACT_B)
    assert state.workspace_path == tmp_path.resolve() / str(CONTRACT_B) / "wr-1"
    assert client.evidence == []


@pytest.mark.asyncio
async def test_a_workspace_path_of_another_contract_is_refused(tmp_path: Path) -> None:
    client = _Client()
    activities = _activities(
        client, organisation=ORG_A, contract_id=CONTRACT_A, workspace_root=tmp_path
    )

    with pytest.raises(ApplicationError) as refused:
        await activities._runtime_state(
            "wr-1", workspace_path=str(tmp_path / str(CONTRACT_A) / ".." / str(CONTRACT_B))
        )
    assert refused.value.type == "workspace_of_another_contract"

    own = await activities._runtime_state(
        "wr-1", workspace_path=str(tmp_path / str(CONTRACT_A) / "wr-1")
    )
    assert own.contract_id == str(CONTRACT_A)


@pytest.mark.asyncio
async def test_a_contract_wipe_on_connection_a_for_a_contract_of_b_is_refused() -> None:
    activities = RunnerRalphActivities(
        _Client(), llm_contracts=lambda: TERMS, connection_organisation=lambda: ORG_A
    )

    with pytest.raises(ApplicationError) as refused:
        await activities.wipe_contract_residue(
            contracts.ContractResidueInput(contract_id=str(CONTRACT_B))
        )

    assert refused.value.type == "contract_of_another_organisation"


@pytest.mark.asyncio
async def test_a_runner_on_its_registered_namespace_alone_is_not_bound(tmp_path: Path) -> None:
    activities = _activities(
        _Client(), organisation=None, contract_id=CONTRACT_ORPHAN, workspace_root=tmp_path
    )

    state = await activities._runtime_state("wr-1")

    assert state.contract_id == str(CONTRACT_ORPHAN)


class _Proxy:
    def __init__(self) -> None:
        self.attempts: list[UUID | None] = []

    def attempt(self, **kwargs: Any) -> Any:
        self.attempts.append(kwargs["contract_id"])
        raise AssertionError("no LLM slot is spent")


@pytest.mark.asyncio
async def test_a_triage_turn_on_connection_a_spends_none_of_bs_llm_slot() -> None:
    proxy = _Proxy()
    triage = RunnerTriageActivities(
        proxy=proxy,  # type: ignore[arg-type]
        model="m",
        timeout_seconds=1,
        llm_contracts=lambda: TERMS,
        connection_organisation=lambda: ORG_A,
    )

    answer = await triage.run_triage_turn(
        contracts.TriageTurnInput(
            prompt="classify",
            directive_id="d-1",
            intake_lead_contract_id=str(CONTRACT_B),
            intake_lead_agent_id=str(uuid4()),
            reserve_max_tokens=100,
        )
    )

    assert answer.raw_text is None
    assert proxy.attempts == []


def test_only_the_acks_own_organisation_for_a_contract_lets_a_connection_serve_it() -> None:
    assert contract_refusal(ORG_A, TERMS, CONTRACT_A) is None
    assert contract_refusal(ORG_B, TERMS, CONTRACT_A) == "contract_of_another_organisation"
    assert contract_refusal(ORG_A, TERMS, None) == "contract_not_served"
    assert contract_refusal(ORG_A, [], CONTRACT_A) == "contract_not_served"
