"""The Runner's pushed Grant state and its liveness clock (PRD issue 44, ADR-0011 §11).

Issue 09 fetched a Grant snapshot at every Directive boundary, so a narrowing bit at the
*next Directive*. Here the control plane pushes a snapshot whenever a link changes and the
Runner applies it on receipt, so a narrowing bites at the **next verb** -- and a Runner
that has lost the control plane stops executing privileged verbs whether or not anything
changed.

Three things live together because they are one fact about "may this verb run now":

* **The per-Agent, per-Contract store.** Snapshots are keyed by Agent and the Contract
  they decide for, and read only by that Agent's verb evaluations on a Work Record under
  that Contract (17's structural rule, QTS-1706): a Runner shared by many Contracts never
  mixes them -- not even for an Organisation Agent working under two Owners' Contracts at
  once -- and a push *replaces* a snapshot rather than merging into it.
* **The acknowledgement.** Each snapshot carries a version, and every heartbeat says which
  versions this Runner holds, which is how the control plane tells "applied" from "sent".
* **The liveness clock.** Staleness is a property of the *link*, not a per-snapshot TTL
  (map ticket 03): three minutes without an acknowledged exchange refuses every privileged
  verb, because a Runner that cannot hear a narrowing must not act on what it last heard.

This is the one place the Runner keeps state between activities, and it is a deliberate
exception to ADR-0013 §3: what is cached is not a fetch result the activity could carry on
its own input, it *is* the control plane's current word, delivered out of band. Everything
else an activity learns still travels on its input and output.

A Protected Path (ADR-0011 §13): the seams read their snapshot from here, so a PR touching
this module always hits a human gate.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from agentic_runner.llm_proxy import Ceilings, CeilingStore
from agentic_runner_contracts.grants import GrantSnapshot
from agentic_runner_contracts.runner_registration import (
    HEARTBEAT_INTERVAL,
    HEARTBEAT_STALE_AFTER,
    SNAPSHOT_PUSH_MAX,
    AppliedSnapshot,
    HeartbeatAck,
)

__all__ = [
    "HEARTBEAT_STALE_REASON",
    "SNAPSHOT_MISSING_REASON",
    "HeartbeatLink",
    "HeartbeatStream",
    "SeamUnavailableError",
]

HEARTBEAT_STALE_REASON = "heartbeat_stale"
SNAPSHOT_MISSING_REASON = "snapshot_missing"

# How far the wall clock may run ahead of the monotonic clock before this process decides
# it was suspended. `time.monotonic()` does not advance across a suspend on Linux, so a
# closed lid shows up as exactly this divergence; one heartbeat interval of slack keeps a
# leap second or an NTP step from reading as a wake (23 item 4).
WAKE_DIVERGENCE = HEARTBEAT_INTERVAL.total_seconds()


class SeamUnavailableError(RuntimeError):
    """A privileged verb cannot be decided right now; ``reason`` is the stable code.

    Not a ``deny``: the chain did not refuse this verb, the Runner is in no position to
    ask it. The activity fails the attempt and Temporal retries it on the same
    ``runner.{runner_id}`` queue (ADR-0013 §8) once the heartbeat is back -- which is why
    this is a plain exception and never a ``WorkerFastApiClientRejectionError``, the type
    the retry policy declares non-retryable.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class HeartbeatStream(Protocol):
    """One heartbeat exchange: what this Runner holds out, what it is pushed back.

    The transport is the Runner process's (``registration.RunnerRegistrationClient``
    signs an envelope over HTTPS); the link only needs the acknowledgement in and the ack
    out, which is also what makes it drivable by a fake in a test.
    """

    async def exchange(self, applied: Sequence[AppliedSnapshot]) -> HeartbeatAck: ...


# (agent_id, contract_id); the Contract is None for a push from a control plane below 3.23.
_Key = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class _Held:
    version: str
    payload: dict[str, Any]
    snapshot: GrantSnapshot


class HeartbeatLink:
    """What the control plane last pushed, and whether it can still be heard."""

    def __init__(
        self,
        stream: HeartbeatStream,
        *,
        ceilings: CeilingStore | None = None,
        clock: Callable[[], tuple[float, float]] | None = None,
        stale_after: float = HEARTBEAT_STALE_AFTER.total_seconds(),
    ) -> None:
        self._stream = stream
        self._ceilings = ceilings
        # (monotonic, wall). One callable rather than two so a test moves both together
        # -- a clock that only advances one of them is precisely a suspend, and that must
        # be deliberate on both sides.
        self._clock = clock or (lambda: (time.monotonic(), time.time()))
        self._stale_after = stale_after
        # Least-recently-read first, capped at what one envelope may acknowledge: a
        # store larger than `SNAPSHOT_PUSH_MAX` would build an `applied()` list that
        # `HeartbeatEnvelope` rejects, and the heartbeat could then never be sent again.
        # Eviction is by read rather than by push because an Agent whose Work Record
        # finished is never read again, while one under a running Directive is read at
        # every verb -- and `max_concurrent_directives` is itself bounded by this number.
        self._held: OrderedDict[_Key, _Held] = OrderedDict()
        self._tags: dict[str, str] | None = None
        self._last_ack: float | None = None
        self._seen_monotonic, self._seen_wall = self._clock()
        self._woke = False

    # ------------------------------------------------------------------ the store

    def snapshot_for(self, agent_id: str | None, contract_id: str | None) -> GrantSnapshot | None:
        """This Agent's pushed snapshot for this Contract, or ``None`` if none was pushed."""

        held = self._touch(agent_id, contract_id)
        return held.snapshot if held else None

    def payload_for(self, agent_id: str | None, contract_id: str | None) -> dict[str, Any]:
        """The snapshot exactly as the control plane wrote it (ADR-0013 §3)."""

        held = self._touch(agent_id, contract_id)
        return dict(held.payload) if held else {}

    def _touch(self, agent_id: str | None, contract_id: str | None) -> _Held | None:
        found = self._find(agent_id, contract_id)
        if found is None:
            return None
        key, held = found
        self._held.move_to_end(key)
        return held

    def _find(self, agent_id: str | None, contract_id: str | None) -> tuple[_Key, _Held] | None:
        """The snapshot deciding this Agent's verbs on a Work Record under this Contract.

        Never another Contract's: a snapshot an older control plane pushed without a
        Contract answers only for the Contract it names itself, or for none when it is the
        held snapshot that refuses every verb.
        """

        if agent_id is None:
            return None
        if contract_id is not None and (agent_id, contract_id) in self._held:
            return (agent_id, contract_id), self._held[(agent_id, contract_id)]
        unkeyed = self._held.get((agent_id, None))
        # The payload, not the parsed snapshot: `from_payload` reads a null as "None".
        if unkeyed is None or unkeyed.payload.get("contract_id") not in (None, contract_id):
            return None
        return (agent_id, None), unkeyed

    @property
    def tags(self) -> dict[str, str] | None:
        """This Runner's Tags as the last ack carried them (ADR-0022 §13).

        ``None`` until an ack carries them, from a control plane below contracts 3.20 or
        before the first beat: routing then checks the Tags this Runner bootstrapped with.
        """

        return None if self._tags is None else dict(self._tags)

    def applied(self) -> list[AppliedSnapshot]:
        return [
            AppliedSnapshot(
                agent_id=UUID(agent_id),
                contract_id=None if contract_id is None else UUID(contract_id),
                version=held.version,
            )
            for (agent_id, contract_id), held in sorted(
                self._held.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ]

    def apply(self, ack: HeartbeatAck) -> None:
        """Apply one ack's pushes: snapshots per Agent and Contract, ceilings per Contract,
        the Tags.

        Replacement, never a merge. A narrowed Grant that merged into what it narrowed
        would leave the wider entry standing, which is the one mistake this whole layer
        exists to prevent. For the same reason a push without a Contract, from a control
        plane below 3.23, replaces every snapshot of its Agent, and a push with one
        replaces the Agent's snapshot that had none.
        """

        if ack.tags is not None:
            self._tags = dict(ack.tags)
        for push in ack.grant_pushes:
            agent_id = str(push.agent_id)
            key: _Key = (agent_id, None if push.contract_id is None else str(push.contract_id))
            replaced = (
                [entry for entry in self._held if entry[0] == agent_id]
                if push.contract_id is None
                else [(agent_id, None)]
            )
            for stale in replaced:
                self._held.pop(stale, None)
            self._held[key] = _Held(
                version=push.version,
                payload=dict(push.snapshot),
                snapshot=GrantSnapshot.from_payload(push.snapshot),
            )
            self._held.move_to_end(key)
        while len(self._held) > SNAPSHOT_PUSH_MAX:
            self._held.popitem(last=False)
        if self._ceilings is None:
            return
        for ceiling in ack.ceiling_pushes:
            self._ceilings.push(
                ceiling.contract_id,
                Ceilings(
                    contract_limit=ceiling.contract_limit,
                    contract_used=ceiling.contract_used,
                    organisation_limit=ceiling.organisation_limit,
                    organisation_used=ceiling.organisation_used,
                    org_funded=ceiling.org_funded,
                ),
            )

    # ------------------------------------------------------------------ the link

    async def exchange(self) -> HeartbeatAck:
        """One heartbeat: acknowledge what is held, apply what comes back, stamp liveness."""

        ack = await self._stream.exchange(self.applied())
        self.apply(ack)
        self._last_ack, self._seen_wall = self._clock()
        self._seen_monotonic = self._last_ack
        self._woke = False
        return ack

    async def require_ready(
        self, agent_id: str | None, contract_id: str | None, *, verb: str
    ) -> None:
        """Refuse a privileged verb this Runner is in no position to decide.

        Order matters: a wake is answered with a heartbeat *before* staleness is read, so
        a laptop that slept re-hears the control plane instead of acting on a snapshot
        from before the sleep (23 item 4); and a Runner holding no snapshot for an Agent
        under this Work Record's Contract refuses rather than running it unattenuated, or
        on another Contract's chain -- absence is never permission.
        """

        if self._observe_wake():
            try:
                await self.exchange()
            except Exception as error:  # noqa: BLE001 - any failed exchange is a lost link
                raise SeamUnavailableError(
                    HEARTBEAT_STALE_REASON,
                    f"{verb!r} is refused: this Runner woke and could not reach the "
                    f"control plane ({error})",
                ) from error
        age = self._age()
        if age is None or age > self._stale_after:
            raise SeamUnavailableError(
                HEARTBEAT_STALE_REASON,
                f"{verb!r} is refused: the last acknowledged heartbeat is "
                f"{'none' if age is None else f'{age:.0f}s'} old, past the "
                f"{self._stale_after:.0f}s liveness bound",
            )
        if agent_id is not None and self._find(agent_id, contract_id) is None:
            raise SeamUnavailableError(
                SNAPSHOT_MISSING_REASON,
                f"{verb!r} is refused: no Grant snapshot has been pushed for Agent {agent_id} "
                f"under Contract {contract_id}",
            )

    def _age(self) -> float | None:
        if self._last_ack is None:
            return None
        monotonic, _ = self._clock()
        return monotonic - self._last_ack

    def _observe_wake(self) -> bool:
        """Whether this process was suspended since the last look.

        Sticky until an exchange succeeds: a wake whose heartbeat failed must go on
        refusing, or a retry would quietly proceed on the pre-sleep snapshot.
        """

        monotonic, wall = self._clock()
        divergence = (wall - self._seen_wall) - (monotonic - self._seen_monotonic)
        self._seen_monotonic, self._seen_wall = monotonic, wall
        self._woke = self._woke or divergence > WAKE_DIVERGENCE
        return self._woke
