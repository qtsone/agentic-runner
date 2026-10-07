"""The Runner's pushed Grant state and its liveness gate (PRD issue 44).

Four properties, each the reason a line of ``heartbeat_link`` exists:

* a pushed snapshot is applied on receipt and **replaces** what the Agent held, so the
  next verb evaluation -- not the next Directive -- sees the narrowing;
* a Runner holding two Contracts' Agents never reads one Agent's snapshot for another;
* three minutes without an acknowledged exchange refuses every privileged verb, and one
  successful exchange lets the retry through;
* a suspend/resume forces one exchange *before* the next privileged verb, so a laptop
  that slept never acts on a snapshot from before the sleep.
"""

from __future__ import annotations

import random
from uuid import UUID, uuid4

import pytest

from agentic_runner.heartbeat_link import (
    HEARTBEAT_STALE_REASON,
    SNAPSHOT_MISSING_REASON,
    HeartbeatLink,
    SeamUnavailableError,
)
from agentic_runner.llm_proxy import CeilingExhaustedError, CeilingStore
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.grants import PR_MERGE_VERB, PUSH_VERB, Decision, decide_verb
from agentic_runner_contracts.runner_registration import (
    HEARTBEAT_STALE_AFTER,
    SNAPSHOT_PUSH_MAX,
    AppliedSnapshot,
    CeilingPush,
    FloorState,
    GrantPush,
    HeartbeatAck,
    HeartbeatEnvelope,
)

RUNNER_ID = UUID("7c6d5e4f-3a2b-4c1d-8e9f-0a1b2c3d4e5f")
AGENT = UUID("8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f")
CONTRACT = UUID("1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d")
REPOSITORY = "qts/agentic-os"

# Everything but `applied_snapshots`, so a test can prove the ack it built is a body the
# control plane would actually accept.
_ENVELOPE = {
    "runner_version": "0.1.0",
    "contracts_version": contracts_version,
    "resource_pressure": {"cpu_percent": 1.0, "memory_percent": 2.0, "disk_percent": 3.0},
    "max_concurrent_directives": 1,
    "current_load": 0,
    "egress_posture": "allowlisted",
    "isolation_mode": "none",
    "tag_set_version": 1,
    "hosted_task_queue": "runner.test",
}


def _grant(**verbs: str) -> dict[str, object]:
    return {"entries": [{"resource_type": "repo", "selector": REPOSITORY, "verbs": dict(verbs)}]}


def _snapshot(agent_id: UUID, contract_id: UUID, **verbs: str) -> dict[str, object]:
    wide = dict.fromkeys((PUSH_VERB, PR_MERGE_VERB), "allow")
    return {
        "agent_id": str(agent_id),
        "contract_id": str(contract_id),
        "contract_state": "active",
        "dispatchable": True,
        "root_grant": _grant(**wide),
        "agent_grant": _grant(**{**wide, **verbs}),
        "persona_allow_list": _grant(**wide),
        "resources": [
            {
                "resource_type": "repo",
                "selector": REPOSITORY,
                "in_contract_scope": True,
                "required_human_approvals": 1,
            }
        ],
    }


def _ack(*pushes: GrantPush, ceilings: tuple[CeilingPush, ...] = ()) -> HeartbeatAck:
    return HeartbeatAck(
        runner_id=RUNNER_ID,
        floor_state=FloorState.OK,
        contracts_floor=contracts_version,
        tag_set_version=1,
        accepts_new_directives=True,
        grant_pushes=list(pushes),
        ceiling_pushes=list(ceilings),
    )


class FakeStream:
    """The control plane's half of the heartbeat: a queue of acks and a call log."""

    def __init__(self, *acks: HeartbeatAck) -> None:
        self.queued = list(acks)
        self.calls: list[tuple[AppliedSnapshot, ...]] = []

    async def exchange(self, applied):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(applied))
        return self.queued.pop(0) if self.queued else _ack()


class FakeClock:
    """A monotonic and a wall clock that can be moved independently.

    Moving only the wall clock is exactly what a suspend looks like to a process:
    ``time.monotonic()`` does not advance while the host is asleep.
    """

    def __init__(self) -> None:
        self.monotonic = 1_000.0
        self.wall = 1_700_000_000.0

    def __call__(self) -> tuple[float, float]:
        return self.monotonic, self.wall

    def tick(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds

    def sleep(self, seconds: float) -> None:
        """The lid closes: wall time passes, the monotonic clock does not."""

        self.wall += seconds


@pytest.mark.asyncio
async def test_a_pushed_snapshot_replaces_the_previous_one_and_is_acknowledged() -> None:
    agent, contract = uuid4(), uuid4()
    stream = FakeStream(
        _ack(GrantPush(agent_id=agent, version="version-one", snapshot=_snapshot(agent, contract))),
        _ack(
            GrantPush(
                agent_id=agent,
                version="version-two",
                snapshot=_snapshot(agent, contract, **{PR_MERGE_VERB: "deny"}),
            )
        ),
    )
    link = HeartbeatLink(stream, clock=FakeClock())

    await link.exchange()
    first = link.snapshot_for(str(agent))
    assert first is not None
    assert decide_verb(first, verb=PR_MERGE_VERB, identifier=REPOSITORY).decision is Decision.ALLOW

    await link.exchange()

    # The narrowed snapshot is in force, and the wide one it replaced is gone: a merge
    # of the two would leave the allowing entry standing.
    narrowed = link.snapshot_for(str(agent))
    assert narrowed is not None
    assert (
        decide_verb(narrowed, verb=PR_MERGE_VERB, identifier=REPOSITORY).decision is Decision.DENY
    )
    assert decide_verb(narrowed, verb=PUSH_VERB, identifier=REPOSITORY).decision is Decision.ALLOW
    assert link.applied() == [AppliedSnapshot(agent_id=agent, version="version-two")]
    # ...and the second exchange told the control plane which version it was holding, so
    # "sent" and "applied" are distinguishable on that side.
    assert stream.calls == [(), (AppliedSnapshot(agent_id=agent, version="version-one"),)]


@pytest.mark.asyncio
async def test_one_runner_holding_two_contracts_never_reads_the_other_agents_snapshot() -> None:
    """17's structural rule, over random pushes: snapshots are keyed by Agent."""

    contracts = [uuid4(), uuid4()]
    agents = {uuid4(): contracts[index % 2] for index in range(6)}
    link = HeartbeatLink(FakeStream(), clock=FakeClock())
    rng = random.Random(44)

    for round_number in range(200):
        pushed = rng.sample(sorted(agents, key=str), rng.randint(1, len(agents)))
        link.apply(
            _ack(
                *(
                    GrantPush(
                        agent_id=agent,
                        version=f"version-{round_number:04d}",
                        snapshot=_snapshot(
                            agent,
                            agents[agent],
                            **{PR_MERGE_VERB: rng.choice(("allow", "deny"))},
                        ),
                    )
                    for agent in pushed
                )
            )
        )
        for agent, contract in agents.items():
            snapshot = link.snapshot_for(str(agent))
            if snapshot is None:
                continue
            assert snapshot.agent_id == str(agent)
            assert snapshot.contract_id == str(contract)


@pytest.mark.asyncio
async def test_a_stale_link_refuses_a_privileged_verb_until_a_heartbeat_lands() -> None:
    agent, contract = uuid4(), uuid4()
    clock = FakeClock()
    stream = FakeStream(
        _ack(GrantPush(agent_id=agent, version="version-one", snapshot=_snapshot(agent, contract))),
    )
    link = HeartbeatLink(stream, clock=clock)
    await link.exchange()

    clock.tick(HEARTBEAT_STALE_AFTER.total_seconds() + 1)
    with pytest.raises(SeamUnavailableError) as refused:
        await link.require_ready(str(agent), verb=PUSH_VERB)
    assert refused.value.reason == HEARTBEAT_STALE_REASON
    # No heartbeat was attempted: the link went quiet, it did not wake up.
    assert len(stream.calls) == 1

    await link.exchange()
    await link.require_ready(str(agent), verb=PUSH_VERB)


@pytest.mark.asyncio
async def test_a_wake_forces_one_exchange_before_the_next_privileged_verb() -> None:
    agent, contract = uuid4(), uuid4()
    clock = FakeClock()
    stream = FakeStream(
        _ack(GrantPush(agent_id=agent, version="version-one", snapshot=_snapshot(agent, contract))),
        _ack(
            GrantPush(
                agent_id=agent,
                version="version-two",
                snapshot=_snapshot(agent, contract, **{PUSH_VERB: "deny"}),
            )
        ),
    )
    link = HeartbeatLink(stream, clock=clock)
    await link.exchange()

    clock.sleep(600)  # the lid was shut for ten minutes
    await link.require_ready(str(agent), verb=PUSH_VERB)

    # Exactly one extra exchange, and it happened *before* the verb was decided -- so the
    # narrowing that landed while the host slept is what the verb is evaluated against.
    assert len(stream.calls) == 2
    snapshot = link.snapshot_for(str(agent))
    assert snapshot is not None
    assert decide_verb(snapshot, verb=PUSH_VERB, identifier=REPOSITORY).decision is Decision.DENY

    # And no second exchange for the second verb: the wake was answered once.
    await link.require_ready(str(agent), verb=PR_MERGE_VERB)
    assert len(stream.calls) == 2


@pytest.mark.asyncio
async def test_a_wake_whose_heartbeat_fails_goes_on_refusing() -> None:
    class BrokenStream(FakeStream):
        async def exchange(self, applied):  # type: ignore[no-untyped-def]
            self.calls.append(tuple(applied))
            if len(self.calls) > 1:
                raise ConnectionError("the control plane is unreachable")
            return _ack(
                GrantPush(
                    agent_id=AGENT, version="version-one", snapshot=_snapshot(AGENT, CONTRACT)
                )
            )

    clock = FakeClock()
    link = HeartbeatLink(BrokenStream(), clock=clock)
    await link.exchange()

    clock.sleep(600)
    for _ in range(2):
        with pytest.raises(SeamUnavailableError) as refused:
            await link.require_ready(str(AGENT), verb=PUSH_VERB)
        assert refused.value.reason == HEARTBEAT_STALE_REASON


@pytest.mark.asyncio
async def test_an_agent_with_no_pushed_snapshot_is_refused_never_run_unattenuated() -> None:
    link = HeartbeatLink(FakeStream(), clock=FakeClock())
    await link.exchange()

    with pytest.raises(SeamUnavailableError) as refused:
        await link.require_ready(str(uuid4()), verb=PUSH_VERB)
    assert refused.value.reason == SNAPSHOT_MISSING_REASON

    # A Work Record with no Agent bound has no chain to be attenuated by and is not
    # refused here (Release 1's Organisation #1 loop, ADR-0011 §8).
    await link.require_ready(None, verb=PUSH_VERB)


@pytest.mark.asyncio
async def test_the_same_channel_carries_the_contract_and_organisation_ceilings() -> None:
    """12 B4: the ceilings the relocated proxy enforces per call arrive here (issue 43)."""

    contract = uuid4()
    ceilings = CeilingStore()
    link = HeartbeatLink(
        FakeStream(
            _ack(
                ceilings=(
                    CeilingPush(
                        contract_id=contract,
                        contract_limit=1_000,
                        contract_used=1_000,
                        organisation_limit=10_000,
                        organisation_used=10,
                        org_funded=True,
                    ),
                )
            )
        ),
        ceilings=ceilings,
        clock=FakeClock(),
    )

    await link.exchange()

    with pytest.raises(CeilingExhaustedError) as exhausted:
        ceilings.authorize(contract)
    assert exhausted.value.ceiling == "contract"


@pytest.mark.asyncio
async def test_the_store_stays_within_what_one_envelope_may_acknowledge() -> None:
    """A long-lived Runner outlives the Agents it held, and must still be able to beat.

    ``HeartbeatEnvelope.applied_snapshots`` caps the ack at ``SNAPSHOT_PUSH_MAX``, so a
    store that grew past it would build a body the schema rejects -- the heartbeat could
    never be sent again, the link would go stale and every privileged verb would be
    refused until the process restarted. Eviction is by *read*: the Agent whose Directive
    is running is read at every verb and stays, the finished one falls out.
    """

    contract = uuid4()
    agents = [uuid4() for _ in range(SNAPSHOT_PUSH_MAX + 4)]
    working, finished = agents[0], agents[1]
    link = HeartbeatLink(FakeStream(), clock=FakeClock())

    link.apply(
        _ack(
            *(
                GrantPush(
                    agent_id=agent,
                    version=f"version-{index:04d}",
                    snapshot=_snapshot(agent, contract),
                )
                for index, agent in enumerate(agents[: SNAPSHOT_PUSH_MAX - 1])
            )
        )
    )
    # This Runner is working for one of them, and keeps reading its snapshot.
    assert link.snapshot_for(str(working)) is not None

    link.apply(
        _ack(
            *(
                GrantPush(
                    agent_id=agent,
                    version=f"version-{index:04d}",
                    snapshot=_snapshot(agent, contract),
                )
                for index, agent in enumerate(agents[SNAPSHOT_PUSH_MAX - 1 :])
            )
        )
    )

    assert len(link.applied()) == SNAPSHOT_PUSH_MAX
    HeartbeatEnvelope.model_validate(
        {
            **_ENVELOPE,
            "applied_snapshots": [entry.model_dump(mode="json") for entry in link.applied()],
        }
    )
    assert link.snapshot_for(str(working)) is not None
    assert link.snapshot_for(str(finished)) is None
