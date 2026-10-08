"""A laptop lid closes mid-Directive (PRD issue 47, map ticket 23 item 4).

Sleep is an outage, never a suspension. The whole machine stops -- here the Runner
process takes ``SIGSTOP`` -- while the Temporal test server keeps time. What must
follow, and what this pins:

* the attempt whose heartbeat stopped is failed by the server on its heartbeat timeout;
* on wake, the orphaned CLI subprocess of that attempt is killed (the Runner's next
  activity heartbeat learns the attempt is gone, and the cancellation kills the CLI's
  whole process group -- ``_runtime_support.run_subprocess_exec``);
* the retry lands on the same ``runner.{runner_id}`` queue, run by the same Runner;
* the wall-clock the retry reports -- the figure the Ralph Loop charges the Work
  Record's Budget with -- spans the gap, because every beat carries the first attempt's
  start as heartbeat details (``activities._liveness_heartbeats``).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import TimeoutType
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

HEARTBEAT_TIMEOUT = timedelta(seconds=2)

# The Runner stand-in: a real Temporal worker in its own process, running a Directive
# the way the Runner's activities do -- a CLI subprocess inside `_liveness_heartbeats`,
# its duration measured the way `directive_started` is.
RUNNER = textwrap.dedent(
    """
    import asyncio, sys, time
    from pathlib import Path
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Worker
    import agentic_runner.activities as runner_activities
    from agentic_runner.workers._runtime_support import run_subprocess_exec

    runner_activities._HEARTBEAT_INTERVAL_SECONDS = 0.2
    address, queue, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    CLI = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); " \\
          "time.sleep(3600 if sys.argv[2] == '1' else 0)"

    @activity.defn(name="sleepy_directive")
    async def sleepy_directive() -> dict:
        info = activity.info()
        async with runner_activities._liveness_heartbeats() as earlier_attempts_seconds:
            directive_started = time.monotonic() - earlier_attempts_seconds
            await run_subprocess_exec(
                argv=[sys.executable, "-c", CLI, str(work / f"cli-{info.attempt}.pid"),
                      str(info.attempt)],
                cwd=work, env={}, stdin=None, timeout_seconds=3600, output_limit_bytes=1024,
            )
            return {
                "attempt": info.attempt,
                "task_queue": info.task_queue,
                "duration_seconds": time.monotonic() - directive_started,
            }

    async def main() -> None:
        client = await Client.connect(address)
        await Worker(client, task_queue=queue, activities=[sleepy_directive]).run()

    asyncio.run(main())
    """
)


@workflow.defn(sandboxed=False)
class DirectiveOnRunner:
    @workflow.run
    async def run(self, runner_queue: str) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "sleepy_directive",
            task_queue=runner_queue,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=200)),
        )
        return result


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(predicate: Any, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_a_lid_close_fails_the_attempt_kills_the_orphan_and_retries_on_the_same_runner(
    tmp_path: Path,
) -> None:
    runner_queue = f"runner.{uuid4()}"
    script = tmp_path / "runner.py"
    script.write_text(RUNNER)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        address = env.client.service_client.config.target_host
        runner = subprocess.Popen(
            [sys.executable, str(script), address, runner_queue, str(tmp_path)]
        )
        try:
            async with Worker(env.client, task_queue="loop", workflows=[DirectiveOnRunner]):
                handle = await env.client.start_workflow(
                    DirectiveOnRunner.run, runner_queue, id=f"wr-{uuid4()}", task_queue="loop"
                )
                first_pidfile = tmp_path / "cli-1.pid"

                async def cli_started() -> bool:
                    return first_pidfile.is_file() and first_pidfile.read_text() != ""

                await _wait_for(cli_started)
                orphan = int(first_pidfile.read_text())
                attempt_started = time.monotonic()

                # The lid closes. Only the Runner takes SIGSTOP: a real sleep freezes the
                # kernel, so no parent ever sees its child *stop*, but a SIGSTOP'd CLI does
                # get seen. On macOS, Python 3.14's asyncio child watcher takes that
                # CLD_STOPPED for an exit (`waitid(WEXITED|WNOWAIT)` returns on a stop
                # there) and reaps with a blocking `waitpid` on the event loop, wedging the
                # Runner until the CLI exits (QTS-1093). The CLI running on through the gap
                # is indistinguishable to the Runner from a frozen one.
                runner.send_signal(signal.SIGSTOP)
                closed = time.monotonic()

                async def failed_on_heartbeat() -> bool:
                    pending = (await handle.describe()).raw_description.pending_activities
                    return bool(pending) and (
                        pending[0].last_failure.timeout_failure_info.timeout_type
                        == TimeoutType.TIMEOUT_TYPE_HEARTBEAT
                    )

                await _wait_for(failed_on_heartbeat)
                await asyncio.sleep(1.0)  # the gap outlives the timeout
                # Wake.
                runner.send_signal(signal.SIGCONT)
                gap = time.monotonic() - closed

                result = await asyncio.wait_for(handle.result(), timeout=60)

                async def orphan_killed() -> bool:
                    return not _alive(orphan)

                await _wait_for(orphan_killed, timeout=10)
        finally:
            runner.kill()
            runner.wait()

    assert result["attempt"] == 2
    assert result["task_queue"] == runner_queue
    # The retry ran on the same Runner -- the only process polling that queue.
    assert (tmp_path / "cli-2.pid").is_file()
    # The Work Record's wall-clock kept running through the sleep: the retry's charge
    # starts at the first attempt, not at its own.
    assert result["duration_seconds"] >= gap
    assert result["duration_seconds"] >= closed - attempt_started + gap
