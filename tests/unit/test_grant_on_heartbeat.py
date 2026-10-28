"""The Effective Grant refreshed on heartbeat (Spec: Agent Guardrails rev 7, A3).

Each verb is decided on the narrower of the Directive's own snapshot and the one the
heartbeat last pushed: a narrowing in a heartbeat answer refuses the verb on the next call
within that Directive, and a widening never lets through what the Directive's snapshot
refuses. The Runner computes, widens and narrows no Grant of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.heartbeat_link import HeartbeatLink
from agentic_runner.mcp import plan_mcp
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.grants import PR_MERGE_VERB, PUSH_VERB, Decision, GrantSnapshot
from agentic_runner_contracts.runner_registration import FloorState, GrantPush, HeartbeatAck
from agentic_runner_contracts.runtime_context import McpServerSpec

RUNNER_ID = UUID("7c6d5e4f-3a2b-4c1d-8e9f-0a1b2c3d4e5f")
AGENT = UUID("8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f")
CONTRACT = UUID("1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d")
REPOSITORY = "qts/agentic-os"
WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"


def _snapshot(*, mcp: str = "allow", **verbs: str) -> dict[str, object]:
    def grant(**decided: str) -> dict[str, object]:
        return {
            "entries": [
                {"resource_type": "repo", "selector": REPOSITORY, "verbs": decided},
                {"resource_type": "mcp:linear", "selector": "linear", "verbs": {"*": mcp}},
            ]
        }

    wide = dict.fromkeys((PUSH_VERB, PR_MERGE_VERB), "allow")
    return {
        "agent_id": str(AGENT),
        "contract_id": str(CONTRACT),
        "contract_state": "active",
        "dispatchable": True,
        "root_grant": grant(**wide),
        "agent_grant": grant(**{**wide, **verbs}),
        "persona_allow_list": grant(**wide),
        "resources": [
            {"resource_type": "repo", "selector": REPOSITORY, "in_contract_scope": True},
            {"resource_type": "mcp:linear", "selector": "linear", "in_contract_scope": True},
        ],
    }


def _ack(snapshot: dict[str, object], version: str) -> HeartbeatAck:
    return HeartbeatAck(
        runner_id=RUNNER_ID,
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=1,
        accepts_new_directives=True,
        grant_pushes=[
            GrantPush(agent_id=AGENT, contract_id=CONTRACT, version=version, snapshot=snapshot)
        ],
    )


class _Stream:
    def __init__(self, *acks: HeartbeatAck) -> None:
        self.queued = list(acks)

    async def exchange(self, applied: object) -> HeartbeatAck:
        return self.queued.pop(0)


class _Client:
    def __init__(self) -> None:
        self.evaluations: list[dict[str, Any]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: Mapping[str, Any], **_: Any
    ) -> dict[str, Any]:
        if source == "ralph.grant_evaluation":
            self.evaluations.append(dict(payload))
        return {}


def _directive(payload: dict[str, object]) -> _RuntimeContextState:
    return _RuntimeContextState(
        reviewer=None,
        repository=REPOSITORY,
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="ship it",
        agent_id=str(AGENT),
        contract_id=str(CONTRACT),
        grant_snapshot=GrantSnapshot.from_payload(payload),
        grant_snapshot_payload=payload,
    )


@pytest.mark.asyncio
async def test_a_narrowing_in_a_heartbeat_answer_refuses_the_verb_on_the_next_call() -> None:
    link = HeartbeatLink(
        _Stream(
            _ack(_snapshot(), "version-0001"),
            _ack(_snapshot(**{PUSH_VERB: "deny"}), "version-0002"),
        )
    )
    client = _Client()
    activities = RunnerRalphActivities(client, heartbeat_link=link)  # type: ignore[arg-type]
    state = _directive(_snapshot())
    await link.exchange()

    before = await activities._evaluate_verb(
        WORK_RECORD_ID, state, verb=PUSH_VERB, identifier=REPOSITORY
    )
    await link.exchange()
    after = await activities._evaluate_verb(
        WORK_RECORD_ID, state, verb=PUSH_VERB, identifier=REPOSITORY
    )

    assert before.decision is Decision.ALLOW
    assert after.decision is Decision.DENY
    assert "decided_on" not in client.evaluations[0]
    assert client.evaluations[1]["decided_on"] == "heartbeat"


@pytest.mark.asyncio
async def test_a_widening_in_a_heartbeat_answer_waits_for_the_next_directive() -> None:
    link = HeartbeatLink(_Stream(_ack(_snapshot(), "version-0001")))
    client = _Client()
    activities = RunnerRalphActivities(client, heartbeat_link=link)  # type: ignore[arg-type]
    state = _directive(_snapshot(**{PR_MERGE_VERB: "deny"}))
    await link.exchange()

    decision = await activities._evaluate_verb(
        WORK_RECORD_ID, state, verb=PR_MERGE_VERB, identifier=REPOSITORY
    )

    assert decision.decision is Decision.DENY
    assert "decided_on" not in client.evaluations[0]


def test_an_mcp_server_takes_the_narrower_of_the_two_snapshots() -> None:
    spec = McpServerSpec(slug="linear", transport="stdio", tools=["create_issue"])
    directive = GrantSnapshot.from_payload(_snapshot())
    narrowed = GrantSnapshot.from_payload(_snapshot(mcp="deny"))

    assert len(plan_mcp(directive, [spec]).granted) == 1
    assert plan_mcp(directive, [spec], narrowed_by=narrowed).granted == ()
    assert len(plan_mcp(narrowed, [spec], narrowed_by=directive).withheld) == 1
