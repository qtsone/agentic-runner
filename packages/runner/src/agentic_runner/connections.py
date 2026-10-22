"""One machine, many connections (Spec: Runners rev 14).

A person's Runner takes work from every Organisation they connected it to: one Temporal
Worker per connection, each polling ``runner.{runner_id}`` in its own namespace with its own
Runner Token, all reconciled from the heartbeat ack. The machine has one concurrency limit,
from its own config, shared by all of them: the first work in takes a free slot.

**The shared limit.** Each Worker gets a :class:`ConnectionSlots`, a custom slot supplier
over one :class:`MachineSlots`. Temporal reserves a slot *before* it polls, and an idle
poll holds its reservation for up to a minute; counting reservations against the machine
would let one idle connection's poll starve every other. So a reservation is granted while
the slots in use on the machine, plus this connection's own unused reservations, are below
the limit. One connection alone therefore never exceeds the limit, exactly as
``max_concurrent_activities`` did; across connections, polls already outstanding can each
bring an activity in the same instant, so N connections can briefly run limit + N - 1. The
spec puts the machine-wide limit in routing ("Routing counts the Runner's load across all
its connections ... so least-loaded and the limit see the whole machine"): the control
plane does not send a Directive to a full machine. This is the backstop.

**Pause.** A paused connection makes no new reservation, so its Worker stops polling once
the polls already outstanding return, while what it already runs finishes. The control
plane routes nothing to a paused connection, so those polls come back empty.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from temporalio.worker import (
    CustomSlotSupplier,
    FixedSizeSlotSupplier,
    SlotMarkUsedContext,
    SlotPermit,
    SlotReleaseContext,
    SlotReserveContext,
    WorkerTuner,
)

from agentic_runner_contracts.runner_registration import ContractLlmTerms, RunnerConnection

__all__ = [
    "ConnectionSlots",
    "ConnectionWorkers",
    "MachineSlots",
    "contract_refusal",
]

_logger = logging.getLogger(__name__)


def contract_refusal(
    organisation: UUID, terms: Sequence[ContractLlmTerms], contract_id: UUID | None
) -> str | None:
    """Why a connection to ``organisation`` must not serve ``contract_id``, or None.

    One process holds every connected Organisation's credentials, harness roots and
    Workspaces, so a task delivered in one Organisation's namespace is served only under a
    Contract the ack places in that Organisation. Only the ack can say whose a Contract is:
    one it does not name, or names without an Organisation, is refused too.
    """

    served = next((term for term in terms if term.contract_id == contract_id), None)
    if served is None or served.organisation_id is None:
        return "contract_not_served"
    if served.organisation_id != organisation:
        return "contract_of_another_organisation"
    return None


class MachineSlots:
    """The machine's one concurrency limit, counted across every connection's Worker.

    Mutated from Temporal's callbacks, which need not run on the event loop's thread, so
    the counts sit behind a lock and a waiting reservation is woken thread-safely.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def used(self) -> int:
        return self._used

    def connection(self) -> ConnectionSlots:
        return ConnectionSlots(self)

    def tuner(self, slots: ConnectionSlots) -> WorkerTuner:
        """A Worker's tuner: its activity slots from ``slots``. The Runner hosts no
        workflow, so the other kinds get the smallest fixed supply."""

        return WorkerTuner.create_composite(
            workflow_supplier=FixedSizeSlotSupplier(1),
            activity_supplier=slots,
            local_activity_supplier=FixedSizeSlotSupplier(1),
            nexus_supplier=FixedSizeSlotSupplier(1),
        )

    def _try_reserve(self, slots: ConnectionSlots) -> bool:
        with self._lock:
            if slots.paused or self._used + slots._reserved >= self.limit:
                return False
            slots._reserved += 1
            return True

    def _mark_used(self, slots: ConnectionSlots) -> None:
        with self._lock:
            slots._reserved -= 1
            self._used += 1

    def _release(self, slots: ConnectionSlots, *, used: bool) -> None:
        with self._lock:
            if used:
                self._used -= 1
            else:
                slots._reserved -= 1
        self._wake()

    def _wake(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, event in waiters:
            with contextlib.suppress(RuntimeError):  # a loop already closed waits for nothing
                loop.call_soon_threadsafe(event.set)

    async def _reserve(self, slots: ConnectionSlots) -> None:
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.add(waiter)
        try:
            while not self._try_reserve(slots):
                await waiter[1].wait()
                waiter[1].clear()
        finally:
            with self._lock:
                self._waiters.discard(waiter)


class _Permit(SlotPermit):
    def __init__(self) -> None:
        self.used = False


class ConnectionSlots(CustomSlotSupplier):
    """One connection's Worker's activity slots, drawn from the machine's."""

    def __init__(self, machine: MachineSlots) -> None:
        self._machine = machine
        self._reserved = 0
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = value
        self._machine._wake()

    async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
        await self._machine._reserve(self)
        return _Permit()

    def try_reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit | None:
        return _Permit() if self._machine._try_reserve(self) else None

    def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
        permit = ctx.permit
        if isinstance(permit, _Permit) and not permit.used:
            permit.used = True
            self._machine._mark_used(self)

    def release_slot(self, ctx: SlotReleaseContext) -> None:
        permit = ctx.permit
        self._machine._release(self, used=isinstance(permit, _Permit) and permit.used)


class GracefulWorker(Protocol):
    async def run(self) -> None: ...
    async def shutdown(self) -> None: ...


class TemporalClient(Protocol):
    api_key: str | None


# Opens a Temporal client for one namespace with one Runner Token.
ConnectClient = Callable[[str, str], Awaitable[TemporalClient]]
# Builds one connection's Worker from its client, the connection and its slot supplier.
BuildWorker = Callable[[TemporalClient, RunnerConnection, ConnectionSlots], GracefulWorker]


@dataclass
class _Running:
    connection: RunnerConnection
    client: TemporalClient
    slots: ConnectionSlots
    worker: GracefulWorker
    task: asyncio.Task[None]


@dataclass
class ConnectionWorkers:
    """The Workers this Runner runs, one per connection the last ack named.

    :meth:`reconcile` starts a Worker for a new connection, hands a renewed Runner Token to
    a running one's client, pauses or resumes it, and drains one whose connection is gone:
    it stops polling and lets what it runs finish, as a SIGTERM drains the process.
    """

    machine: MachineSlots
    connect_client: ConnectClient
    build_worker: BuildWorker
    _running: dict[UUID, _Running] = field(default_factory=dict)
    _draining: set[asyncio.Task[None]] = field(default_factory=set)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None
    _closed: bool = False

    @property
    def polled(self) -> Mapping[UUID, RunnerConnection]:
        return {key: running.connection for key, running in self._running.items()}

    async def reconcile(self, connections: Sequence[RunnerConnection]) -> None:
        """Follow one ack. A connection that cannot open is logged and retried on the next
        beat; it never holds back the others, whose tokens must keep being renewed."""

        if self._closed:
            # Beats go on through a drain; their acks start nothing new.
            return
        wanted = {connection.organisation_id: connection for connection in connections}
        for key in set(self._running) - set(wanted):
            self._drain(key)
        for key, connection in wanted.items():
            if (running := self._running.get(key)) is not None:
                self._update(running, connection)
        for key, connection in wanted.items():
            # A connection's first ack always carries its token; one that did not is retried
            # on the next beat rather than dialled without one.
            if key not in self._running and connection.runner_token is not None:
                try:
                    await self._start(connection)
                except Exception as error:  # noqa: BLE001 - retried on the next beat
                    _logger.warning("cannot poll %s yet: %s", connection.temporal_namespace, error)

    def _update(self, running: _Running, connection: RunnerConnection) -> None:
        if connection.runner_token is not None and (
            connection.runner_token != running.client.api_key
        ):
            running.client.api_key = connection.runner_token
        if connection.paused != running.slots.paused:
            running.slots.paused = connection.paused
        running.connection = connection

    async def shutdown(self) -> None:
        """Drain every Worker; returns once each has finished what it was running."""

        self._closed = True
        for key in list(self._running):
            self._drain(key)
        if self._draining:
            await asyncio.gather(*self._draining)

    async def _start(self, connection: RunnerConnection) -> None:
        assert connection.runner_token is not None
        client = await self.connect_client(connection.temporal_namespace, connection.runner_token)
        if self._closed:
            # Shut down while the client opened: a Worker started now would never drain.
            return
        slots = self.machine.connection()
        slots.paused = connection.paused
        worker = self.build_worker(client, connection, slots)
        task = asyncio.create_task(worker.run())
        task.add_done_callback(self._watch)
        self._running[connection.organisation_id] = _Running(
            connection=connection, client=client, slots=slots, worker=worker, task=task
        )
        _logger.info("polling %s in %s", connection.task_queue, connection.temporal_namespace)

    def _drain(self, key: UUID) -> None:
        running = self._running.pop(key)
        _logger.info("draining %s: its connection is gone", running.connection.temporal_namespace)

        async def drain() -> None:
            await running.worker.shutdown()
            try:
                await running.task
            except Exception:
                # Gone already, so nothing waits on it; the drain is still the process's
                # own, and the reason must reach its log.
                _logger.exception(
                    "the Worker for %s failed while draining", running.connection.temporal_namespace
                )

        task = asyncio.create_task(drain())
        self._draining.add(task)
        task.add_done_callback(self._draining.discard)

    def _watch(self, task: asyncio.Task[Any]) -> None:
        # A Worker that stops on its own -- not drained -- ends the process, as the one
        # Worker's `run()` returning or raising did before connections.
        if task.cancelled() or all(r.task is not task for r in self._running.values()):
            return
        self.error = task.exception()
        self.failed.set()
