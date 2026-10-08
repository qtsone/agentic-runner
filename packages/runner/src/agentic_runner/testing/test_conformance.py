"""The conformance scenarios: ``pytest --pyargs agentic_runner.testing``.

Each one starts the installed Runner as a process against the fake control plane and a
local Temporal dev server, and checks one thing an operator relies on: it registers once,
it heartbeats signed, it runs a Directive dispatched to its own queue, it stops when
revoked, and it drains on SIGTERM. A fork that passes these has a Runner the platform can
drive; ``docs/conformance.md`` says what they do not cover.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from agentic_runner import __version__ as runner_version
from agentic_runner.registration import RUNNER_REVOKED_REASON
from agentic_runner.testing.plugin import (
    RunnerLauncher,
    ServedControlPlane,
    start_temporal,
    until_ready,
)
from agentic_runner.testing.workflow import RoundTripWorkflow
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.activity_io import ContractResidueInput
from agentic_runner_contracts.public_metadata import runner_task_queue
from agentic_runner_contracts.routing import DirectiveRouting
from agentic_runner_contracts.runner_registration import IsolationMode, LifecycleKind


@pytest.mark.asyncio
async def test_a_runner_registers_once_and_a_restart_is_the_same_runner(
    fake_control_plane: ServedControlPlane, runner_launcher: RunnerLauncher
) -> None:
    plane = fake_control_plane.plane
    async with await start_temporal(plane.namespace) as temporal:
        address = temporal.client.service_client.config.target_host
        first = runner_launcher.start(
            control_plane_url=fake_control_plane.url, temporal_address=address
        )
        await until_ready(fake_control_plane, first)

        [bootstrap] = plane.bootstraps
        assert bootstrap.isolation_mode is IsolationMode.NONE
        assert bootstrap.runner_version == runner_version
        assert bootstrap.contracts_version == contracts_version
        assert first.terminate() == 0

        beats = len(plane.heartbeats)
        second = runner_launcher.start(
            control_plane_url=fake_control_plane.url, temporal_address=address
        )
        await until_ready(fake_control_plane, second, beats=beats + 1)
        assert second.terminate() == 0

    # The identity is read back from the state directory: no second registration.
    assert len(plane.bootstraps) == 1
    assert set(plane.heartbeat_runner_ids) == set(plane.runner_ids)


@pytest.mark.asyncio
async def test_a_runner_heartbeats_signed_from_its_own_queue(
    fake_control_plane: ServedControlPlane, runner_launcher: RunnerLauncher
) -> None:
    plane = fake_control_plane.plane
    async with await start_temporal(plane.namespace) as temporal:
        runner = runner_launcher.start(
            control_plane_url=fake_control_plane.url,
            temporal_address=temporal.client.service_client.config.target_host,
        )
        await until_ready(fake_control_plane, runner, beats=3)
        assert runner.terminate() == 0

    [runner_id] = plane.runner_ids
    assert set(plane.heartbeat_runner_ids) == {runner_id}
    # Every beat verified against the identity the fake handed out; none was refused.
    assert plane.refusals == []
    assert {beat.hosted_task_queue for beat in plane.heartbeats} == {runner_task_queue(runner_id)}
    assert {beat.contracts_version for beat in plane.heartbeats} == {contracts_version}


@pytest.mark.asyncio
async def test_a_directive_dispatched_to_the_runner_queue_comes_back_answered(
    fake_control_plane: ServedControlPlane, runner_launcher: RunnerLauncher
) -> None:
    plane = fake_control_plane.plane
    async with await start_temporal(plane.namespace) as temporal:
        runner = runner_launcher.start(
            control_plane_url=fake_control_plane.url,
            temporal_address=temporal.client.service_client.config.target_host,
        )
        await until_ready(fake_control_plane, runner)
        [runner_id] = plane.runner_ids
        contract_id = str(uuid4())
        request = ContractResidueInput(
            contract_id=contract_id,
            routing=DirectiveRouting(runner_id=str(runner_id), host_party=plane.host_party),
        )

        # The workflow is this suite's, not the platform's; the kit's own modules pass
        # through the sandbox rather than being re-imported inside it.
        async with Worker(
            temporal.client,
            task_queue="conformance-workflows",
            workflows=[RoundTripWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "agentic_runner", "agentic_runner_contracts"
                )
            ),
        ):
            answer = await temporal.client.execute_workflow(
                RoundTripWorkflow.run,
                args=[runner_task_queue(runner_id), request],
                id=f"conformance-{uuid4()}",
                task_queue="conformance-workflows",
            )
        assert runner.terminate() == 0

    assert answer.contract_id == contract_id
    assert answer.workspaces_wiped == 0


@pytest.mark.asyncio
async def test_a_revoked_runner_stops(
    fake_control_plane: ServedControlPlane, runner_launcher: RunnerLauncher
) -> None:
    plane = fake_control_plane.plane
    async with await start_temporal(plane.namespace) as temporal:
        runner = runner_launcher.start(
            control_plane_url=fake_control_plane.url,
            temporal_address=temporal.client.service_client.config.target_host,
        )
        await until_ready(fake_control_plane, runner)
        [runner_id] = plane.runner_ids

        plane.revoke(runner_id)
        exit_code = runner.wait(timeout=60)

    assert exit_code != 0
    assert "revoked" in runner.output()
    assert {refusal.reason for refusal in plane.refusals} == {RUNNER_REVOKED_REASON}


@pytest.mark.asyncio
async def test_sigterm_drains_to_a_clean_exit_and_reports_the_stop(
    fake_control_plane: ServedControlPlane, runner_launcher: RunnerLauncher
) -> None:
    plane = fake_control_plane.plane
    async with await start_temporal(plane.namespace) as temporal:
        runner = runner_launcher.start(
            control_plane_url=fake_control_plane.url,
            temporal_address=temporal.client.service_client.config.target_host,
        )
        await until_ready(fake_control_plane, runner)

        assert runner.terminate() == 0

    # The last beat is the one the drain sends on its way out.
    assert LifecycleKind.STOP in {event.kind for event in plane.heartbeats[-1].lifecycle}
