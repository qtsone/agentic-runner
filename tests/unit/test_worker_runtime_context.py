from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agentic_runner_contracts.runtime_context import (
    WorkerRuntimeContext,
    WorkerRuntimeContextResolver,
)


def valid_context_payload() -> dict[str, Any]:
    return {
        "work_record_id": "wr_123",
        "profile_slug": "codex-default",
        "cli_kind": "codex_cli",
        "repo": "qtsone/agentic-os",
        "base_branch": "main",
        "reviewer": "@ada",
        "task_queue": "engineer-codex-default",
        "worker_secret_refs": ["codex-subscription-token"],
        "command_policy": {"allow": ["uv run pytest"]},
        "network_policy": {"egress": ["github.com"]},
        "product_verifier_command_source": {
            "source": "work_record_payload",
            "available": False,
            "metadata": {"reason": "not_configured"},
        },
    }


@pytest.mark.parametrize(
    "secret_value_field",
    [
        "secret_value",
        "token_value",
        "api_key_value",
        "private_key_value",
        "password_value",
    ],
)
def test_worker_runtime_context_rejects_secret_value_fields(
    secret_value_field: str,
) -> None:
    payload = valid_context_payload()
    payload[secret_value_field] = "raw-secret-material"

    with pytest.raises(ValidationError):
        WorkerRuntimeContext.model_validate(payload)


@pytest.mark.asyncio
async def test_runtime_context_resolver_fetches_context_through_fastapi_client() -> None:
    class StubFastApiClient:
        def __init__(self) -> None:
            self.requested_work_record_ids: list[str] = []

        async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
            self.requested_work_record_ids.append(work_record_id)
            return valid_context_payload() | {"work_record_id": work_record_id}

    client = StubFastApiClient()
    resolver = WorkerRuntimeContextResolver(client)

    context = await resolver.resolve("wr_123")

    assert client.requested_work_record_ids == ["wr_123"]
    assert context == WorkerRuntimeContext.model_validate(
        valid_context_payload() | {"work_record_id": "wr_123"}
    )
