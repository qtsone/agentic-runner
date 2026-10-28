"""A Runner routes on the Tags the console edited, not the ones it bootstrapped with
(ADR-0022 §13, QTS-1553).

The control plane routes on a Tag edit at once. A Runner that kept checking its bootstrap
Tags would refuse work routed on an added Tag with ``selector_unsatisfied`` on every
retry, so the heartbeat ack carries the current set and the Runner adopts it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agentic_runner.activities import RunnerRalphActivities
from agentic_runner.heartbeat_link import HeartbeatLink
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts import activity_io as contracts
from agentic_runner_contracts.routing import (
    DirectiveRouting,
    RoutingRefusedError,
    RunnerRoutingIdentity,
    SelectorOperator,
    SelectorRequirement,
    assert_routed,
)
from agentic_runner_contracts.runner_registration import FloorState, HeartbeatAck

RUNNER_ID = "11111111-1111-4111-8111-111111111111"
BOOTSTRAPPED = RunnerRoutingIdentity(runner_id=RUNNER_ID, tags={"region": "eu"})
ON_A100 = DirectiveRouting(
    runner_id=RUNNER_ID,
    requirements=(SelectorRequirement(key="gpu", operator=SelectorOperator.IN, values=("a100",)),),
)


def _ack(tags: dict[str, str] | None) -> HeartbeatAck:
    return HeartbeatAck(
        runner_id=UUID(RUNNER_ID),
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=2,
        accepts_new_directives=True,
        tags=tags,
    )


class _Stream:
    def __init__(self, *acks: HeartbeatAck) -> None:
        self.queued = list(acks)

    async def exchange(self, applied: Any) -> HeartbeatAck:
        return self.queued.pop(0)


class _NoEvidence:
    async def append_evidence(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}


def _directive() -> contracts.FixDirectiveInput:
    return contracts.FixDirectiveInput(
        work_record_id="wr-10",
        repository="qts/agentic-os",
        pr_number=42,
        base_ref="main",
        branch_name="ralph/wr-10",
        directive_number=2,
        verifier_summary="pytest failed",
        routing=ON_A100,
    )


# ------------------------------------------------------------------ the wire


def test_an_ack_without_tags_omits_the_key_so_a_runner_on_3_19_still_parses_it() -> None:
    assert "tags" not in _ack(None).model_dump(mode="json")


def test_an_empty_tag_set_is_sent_because_it_says_the_runner_has_no_tags() -> None:
    body = _ack({}).model_dump(mode="json")

    assert body["tags"] == {}
    assert HeartbeatAck.model_validate(body).tags == {}


def test_the_acked_tags_round_trip_and_obey_the_bootstrap_tag_rules() -> None:
    assert HeartbeatAck.model_validate(_ack({"gpu": "a100"}).model_dump(mode="json")).tags == {
        "gpu": "a100"
    }
    with pytest.raises(ValueError, match="pattern"):
        _ack({"GPU": "a100"})


def test_a_directive_routed_on_an_added_tag_holds_against_the_acked_tags() -> None:
    with pytest.raises(RoutingRefusedError):
        assert_routed(ON_A100, BOOTSTRAPPED)

    assert_routed(ON_A100, RunnerRoutingIdentity(RUNNER_ID, {"gpu": "a100"}))


# ------------------------------------------------------------------ the Runner


@pytest.mark.asyncio
async def test_the_runner_accepts_work_routed_on_a_tag_the_console_added() -> None:
    link = HeartbeatLink(_Stream(_ack({"region": "eu", "gpu": "a100"})))
    activities = RunnerRalphActivities(
        _NoEvidence(), routing_identity=BOOTSTRAPPED, heartbeat_link=link
    )
    # Before the first ack the Runner checks the Tags it bootstrapped with.
    with pytest.raises(RoutingRefusedError, match="selector"):
        await activities._assert_routed(_directive())

    await link.exchange()

    await activities._assert_routed(_directive())


@pytest.mark.asyncio
async def test_a_removed_tag_is_dropped_and_an_ack_without_tags_keeps_the_last_set() -> None:
    link = HeartbeatLink(_Stream(_ack({"gpu": "a100"}), _ack(None), _ack({})))
    activities = RunnerRalphActivities(
        _NoEvidence(), routing_identity=BOOTSTRAPPED, heartbeat_link=link
    )

    await link.exchange()
    await link.exchange()
    await activities._assert_routed(_directive())

    await link.exchange()
    with pytest.raises(RoutingRefusedError, match="selector"):
        await activities._assert_routed(_directive())


@pytest.mark.asyncio
async def test_a_runner_with_no_operator_in_its_state_learns_it_from_the_ack() -> None:
    """QTS-2378: a Runner a person's token registered before Spec: Runners rev 14 kept only
    its retired host party; the ack names its operator, and subscriptions follow it."""

    ack = _ack(None).model_copy(update={"connections": [], "operator_user_profile_id": UUID(int=7)})
    link = HeartbeatLink(_Stream(ack))
    activities = RunnerRalphActivities(
        _NoEvidence(), routing_identity=BOOTSTRAPPED, heartbeat_link=link
    )
    assert activities._operated() is False

    await link.exchange()

    assert link.operated is True
    assert activities._operated() is True
