"""One machine, many connections (QTS-2252, Spec: Runners rev 14).

What this pins:

* the machine has one concurrency limit, shared by every connection's Worker: the first
  work in takes a free slot, and a slot in use on one connection is not free on another;
* an idle poll on one connection does not starve another (Temporal reserves before it
  polls, so a reservation must not count against the machine);
* one connection alone never exceeds the limit;
* a paused connection reserves nothing, and resuming it lets it poll again;
* the Workers follow the ack: a new connection starts one, a renewed token reaches its
  client, a paused one pauses, and a gone one drains.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.worker import SlotPermit, SlotReleaseContext

from agentic_runner.connections import ConnectionSlots, ConnectionWorkers, MachineSlots
from agentic_runner_contracts.runner_registration import RunnerConnection

_RESERVE: Any = SimpleNamespace(slot_type="activity", task_queue="runner.x", is_sticky=False)


async def _reserve(slots: ConnectionSlots) -> SlotPermit:
    return await asyncio.wait_for(slots.reserve_slot(_RESERVE), timeout=1)


async def _blocks(slots: ConnectionSlots) -> asyncio.Task[SlotPermit]:
    task = asyncio.create_task(slots.reserve_slot(_RESERVE))
    await asyncio.sleep(0.02)
    assert not task.done()
    return task


def _use(slots: ConnectionSlots, permit: SlotPermit) -> None:
    slots.mark_slot_used(SimpleNamespace(permit=permit, slot_info=None))  # type: ignore[arg-type]


def _release(slots: ConnectionSlots, permit: SlotPermit) -> None:
    slots.release_slot(SlotReleaseContext(slot_info=None, permit=permit))


@pytest.mark.asyncio
async def test_a_slot_in_use_on_one_connection_is_not_free_on_another() -> None:
    machine = MachineSlots(1)
    employer, own = machine.connection(), machine.connection()

    working = await _reserve(employer)
    _use(employer, working)
    waiting = await _blocks(own)

    _release(employer, working)

    await asyncio.wait_for(waiting, timeout=1)
    assert machine.used == 0


@pytest.mark.asyncio
async def test_an_idle_poll_on_one_connection_does_not_starve_another() -> None:
    machine = MachineSlots(1)
    employer, own = machine.connection(), machine.connection()

    await _reserve(employer)  # polling an empty queue, slot not used

    permit = await _reserve(own)
    _use(own, permit)
    assert machine.used == 1


@pytest.mark.asyncio
async def test_one_connection_alone_never_exceeds_the_limit() -> None:
    machine = MachineSlots(2)
    only = machine.connection()

    first, second = await _reserve(only), await _reserve(only)
    third = await _blocks(only)
    _use(only, first)
    _use(only, second)
    assert machine.used == 2

    _release(only, first)

    await asyncio.wait_for(third, timeout=1)


@pytest.mark.asyncio
async def test_an_unused_reservation_given_back_frees_its_place() -> None:
    machine = MachineSlots(1)
    only = machine.connection()
    unused = await _reserve(only)
    waiting = await _blocks(only)

    _release(only, unused)

    await asyncio.wait_for(waiting, timeout=1)
    assert machine.used == 0


@pytest.mark.asyncio
async def test_a_paused_connection_reserves_nothing_until_it_resumes() -> None:
    machine = MachineSlots(4)
    slots = machine.connection()
    slots.paused = True

    waiting = await _blocks(slots)
    assert slots.try_reserve_slot(_RESERVE) is None

    slots.paused = False

    await asyncio.wait_for(waiting, timeout=1)


# ------------------------------------------------------------------ following the ack


class _Client:
    def __init__(self, namespace: str, token: str) -> None:
        self.namespace = namespace
        self.api_key: str | None = token


class _Worker:
    def __init__(self, client: _Client, task_queue: str, slots: ConnectionSlots) -> None:
        self.client, self.task_queue, self.slots = client, task_queue, slots
        self._stopped = asyncio.Event()
        self.shut_down = False

    async def run(self) -> None:
        await self._stopped.wait()

    async def shutdown(self) -> None:
        self.shut_down = True
        self._stopped.set()


def _workers() -> tuple[ConnectionWorkers, list[_Worker]]:
    built: list[_Worker] = []

    async def connect_client(namespace: str, token: str) -> _Client:
        return _Client(namespace, token)

    def build_worker(client: Any, task_queue: str, slots: ConnectionSlots) -> _Worker:
        worker = _Worker(client, task_queue, slots)
        built.append(worker)
        return worker

    return ConnectionWorkers(MachineSlots(1), connect_client, build_worker), built


def _connection(organisation_id: UUID, token: str | None = "t1", **fields: Any) -> RunnerConnection:
    return RunnerConnection(
        organisation_id=organisation_id,
        temporal_namespace=f"org-{organisation_id}",
        task_queue="runner.x",
        runner_token=token,
        **fields,
    )


@pytest.mark.asyncio
async def test_each_connection_gets_a_worker_on_its_own_namespace_and_token() -> None:
    workers, built = _workers()
    employer, own = uuid4(), uuid4()

    await workers.reconcile(
        [_connection(employer, "employer-token"), _connection(own, "own-token")]
    )

    assert {(w.client.namespace, w.client.api_key) for w in built} == {
        (f"org-{employer}", "employer-token"),
        (f"org-{own}", "own-token"),
    }
    # One machine: both Workers draw from the same limit.
    assert built[0].slots._machine is built[1].slots._machine
    await workers.shutdown()


@pytest.mark.asyncio
async def test_a_renewed_token_and_a_pause_reach_the_running_worker() -> None:
    workers, built = _workers()
    organisation = uuid4()
    await workers.reconcile([_connection(organisation, "t1")])

    await workers.reconcile([_connection(organisation, "t2", paused=True)])

    [worker] = built
    assert (worker.client.api_key, worker.slots.paused) == ("t2", True)
    # A held Runner's ack carries no token: the one held is kept until it expires.
    await workers.reconcile([_connection(organisation, None, paused=False)])
    assert (worker.client.api_key, worker.slots.paused) == ("t2", False)
    await workers.shutdown()


@pytest.mark.asyncio
async def test_a_connection_gone_from_the_ack_drains_and_the_others_keep_polling() -> None:
    workers, built = _workers()
    employer, own = uuid4(), uuid4()
    await workers.reconcile([_connection(employer), _connection(own)])

    await workers.reconcile([_connection(own)])
    await asyncio.sleep(0)

    gone, kept = built
    assert (gone.shut_down, kept.shut_down) == (True, False)
    assert set(workers.polled) == {own}
    await workers.shutdown()
    assert kept.shut_down


@pytest.mark.asyncio
async def test_after_shutdown_an_ack_starts_nothing() -> None:
    workers, built = _workers()
    await workers.shutdown()

    await workers.reconcile([_connection(uuid4())])

    assert built == []


class _FailingWorker(_Worker):
    async def run(self) -> None:
        raise RuntimeError("poller died")


@pytest.mark.asyncio
async def test_a_worker_that_fails_on_its_own_is_the_process_failure() -> None:
    async def connect_client(namespace: str, token: str) -> _Client:
        return _Client(namespace, token)

    workers = ConnectionWorkers(MachineSlots(1), connect_client, _FailingWorker)  # type: ignore[arg-type]

    await workers.reconcile([_connection(uuid4())])

    await asyncio.wait_for(workers.failed.wait(), timeout=1)
    assert isinstance(workers.error, RuntimeError)
