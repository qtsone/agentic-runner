"""The control plane's half of a Directive round trip, as small as one can be.

The platform's workflows live in the platform; this one stands in for them in the
conformance suite. It dispatches one Runner activity to ``runner.{runner_id}`` -- the
queue a real workflow routes to -- and returns what the Runner answered. The activity is
``wipe_contract_residue``: it touches no workspace, no Agent Runtime and no GitHub, so its
answer depends only on the Runner having picked the task up off its own queue.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agentic_runner_contracts.activity_io import ContractResidueInput, ContractResidueOutput

ROUND_TRIP_ACTIVITY = "wipe_contract_residue"


@workflow.defn(name="agentic_runner_conformance_round_trip")
class RoundTripWorkflow:
    @workflow.run
    async def run(self, task_queue: str, request: ContractResidueInput) -> ContractResidueOutput:
        answer: ContractResidueOutput = await workflow.execute_activity(
            ROUND_TRIP_ACTIVITY,
            request,
            task_queue=task_queue,
            result_type=ContractResidueOutput,
            start_to_close_timeout=timedelta(seconds=30),
            # One attempt: a Runner that cannot answer should fail the scenario, not
            # retry it into the test's timeout.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return answer
