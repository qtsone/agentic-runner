"""Dispatch one Codex Directive to a Runner, as the platform's loop would (local-agents 04b).

The Learning Directive is the one Runner activity that runs the Agent Runtime with no
clone and no GitHub, so it is the Directive this test can run end to end on kind. The
workflow here only does what the Ralph Loop does for it: call the activity by name on the
Runner's own queue, with the routing the control plane matched it on.

    python run-directive.py <temporal address> <namespace> <runner id>
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

DRIVER_QUEUE = "chart-test-driver"


@workflow.defn(name="ChartTestDirective")
class ChartTestDirective:
    @workflow.run
    async def run(self, runner_id: str, request: dict[str, Any]) -> Any:
        return await workflow.execute_activity(
            "execute_learning_directive",
            request,
            task_queue=f"runner.{runner_id}",
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def main(address: str, namespace: str, runner_id: str) -> None:
    client = await Client.connect(address, namespace=namespace)
    work_record_id = str(uuid4())
    request = {
        "work_record_id": work_record_id,
        "repository": "chart-test/repo",
        "base_ref": "main",
        "directive_number": 1,
        "learner_agent_id": "00000000-0000-4000-8000-0000000000a1",
        "ending_kind": "merged",
        "routing": {"runner_id": runner_id, "host_party": "organisation"},
    }
    async with Worker(client, task_queue=DRIVER_QUEUE, workflows=[ChartTestDirective]):
        result = await client.execute_workflow(
            ChartTestDirective.run,
            args=[runner_id, request],
            id=f"chart-test-directive-{work_record_id}",
            task_queue=DRIVER_QUEUE,
            execution_timeout=timedelta(minutes=6),
        )
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
