"""Which keys the Triage turn may spend on this Runner (QTS-1751).

A pool Runner's store key stays out of the turn until an ack names the Contracts, the same
rule a Directive that names no access follows (QTS-1736): a control plane below 3.25 caps
none of that spend. ``tests/unit/test_runner_llm_proxy.py`` holds the proxy half, that a
``delivered_only`` call never resolves the store's key.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agentic_runner.triage_activities import RunnerTriageActivities
from agentic_runner_contracts.activity_io import TriageTurnInput
from agentic_runner_contracts.runner_registration import ContractLlmTerms

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")


class _Proxy:
    def __init__(self) -> None:
        self.admitted: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def attempt(self, **admitted: Any) -> AsyncIterator[Any]:
        self.admitted.append(admitted)
        yield SimpleNamespace(base_url="http://proxy.test/a/x/v1", token="attempt-bearer")


def _completion(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "own"}}]})


@pytest.mark.parametrize(
    ("operated", "named", "delivered_only"),
    [
        (False, None, True),
        (False, [ContractLlmTerms(contract_id=CONTRACT, subscription="no_right")], False),
        (False, [], False),
        (True, None, False),
    ],
)
@pytest.mark.asyncio
async def test_a_pool_runner_keeps_triage_off_its_store_key_until_an_ack_names_access(
    operated: bool, named: list[ContractLlmTerms] | None, delivered_only: bool
) -> None:
    proxy = _Proxy()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_completion)) as client:
        output = await RunnerTriageActivities(
            proxy=proxy,  # type: ignore[arg-type]
            model="gpt-5-mini",
            timeout_seconds=5,
            operated=operated,
            llm_contracts=lambda: named,
            client=client,
        ).run_triage_turn(
            TriageTurnInput(
                prompt="triage this",
                directive_id="triage:1",
                intake_lead_agent_id=str(CONTRACT),
                intake_lead_contract_id=str(CONTRACT),
                reserve_max_tokens=500,
            )
        )

    assert output.raw_text == "own"
    [admitted] = proxy.admitted
    assert admitted["delivered_only"] is delivered_only
