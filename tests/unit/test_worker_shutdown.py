"""Unit coverage for the worker's SIGTERM drain (multi-org-release-1 issue 02).

Python's default SIGTERM disposition kills the process outright; without an explicit
handler an in-flight Directive would die mid-run instead of finishing. These tests
exercise the real OS-signal wiring (self-signalled, safe on the main thread) against a
fake Worker, never a live Temporal connection.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from agentic_runner.service import graceful_shutdown_timeout, run_worker_until_terminated
from agentic_runner.workers.settings import WorkerSettings


class FakeGracefulWorker:
    """Mirrors temporalio.worker.Worker's shutdown contract: run() blocks until shutdown()
    is called, and shutdown() itself does not return until run()'s cleanup — here, the
    "in-flight activity" — has actually finished."""

    def __init__(self) -> None:
        self.shutdown_called = False
        self.activity_completed = False
        self._stop_requested = asyncio.Event()

    async def run(self) -> None:
        await self._stop_requested.wait()
        await asyncio.sleep(0)  # the "in-flight activity" finishing its last step
        self.activity_completed = True

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self._stop_requested.set()
        while not self.activity_completed:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sigterm_drains_in_flight_activity_before_returning() -> None:
    worker = FakeGracefulWorker()
    task = asyncio.create_task(run_worker_until_terminated(worker))
    await asyncio.sleep(0)  # let the task install its signal handler and start polling

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, timeout=2)
    assert worker.shutdown_called
    assert worker.activity_completed


@pytest.mark.asyncio
async def test_no_sigterm_never_calls_shutdown() -> None:
    worker = FakeGracefulWorker()
    task = asyncio.create_task(run_worker_until_terminated(worker))
    await asyncio.sleep(0)

    assert not task.done()
    assert not worker.shutdown_called

    worker._stop_requested.set()  # let the fake's own run() finish so the test can clean up
    await asyncio.wait_for(task, timeout=2)


def test_graceful_shutdown_timeout_covers_one_directive_and_undercuts_the_pod_grace_period() -> (
    None
):
    settings = WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        CODEX_CLI_TIMEOUT_SECONDS=900,
        CLAUDE_CLI_TIMEOUT_SECONDS=900,
    )

    timeout = graceful_shutdown_timeout(settings)

    # One Directive's CLI run (900s) plus the verifier's own 20-minute activity ceiling,
    # minus a minute of headroom before the Runner chart's
    # terminationGracePeriodSeconds would SIGKILL the pod.
    assert timeout.total_seconds() == 900 + 20 * 60 - 60
