from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from agentic_runner_contracts.runtime_context import (
    SKILL_BODY_LIMIT_BYTES,
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


def _skill(**overrides: Any) -> dict[str, Any]:
    body = "---\nname: review\ndescription: How we review.\n---\nRead the diff first.\n"
    return {
        "slug": "review",
        "version": 3,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "body": body,
    } | overrides


def test_a_context_from_a_control_plane_without_skills_still_parses() -> None:
    assert WorkerRuntimeContext.model_validate(valid_context_payload()).skills == []


def test_attached_skills_round_trip_through_the_context() -> None:
    payload = valid_context_payload() | {"skills": [_skill()]}

    context = WorkerRuntimeContext.model_validate(payload)

    assert WorkerRuntimeContext.model_validate(context.model_dump()) == context
    assert context.skills[0].model_dump() == _skill()


@pytest.mark.parametrize("slug", ["../escape", "a/b", ".system", "Review", ""])
def test_a_skill_slug_that_is_not_a_safe_directory_name_is_refused(slug: str) -> None:
    with pytest.raises(ValidationError):
        WorkerRuntimeContext.model_validate(
            valid_context_payload() | {"skills": [_skill(slug=slug)]}
        )


def test_a_skill_body_over_64_kib_is_refused() -> None:
    body = "é" * (SKILL_BODY_LIMIT_BYTES // 2 + 1)

    with pytest.raises(ValidationError):
        WorkerRuntimeContext.model_validate(
            valid_context_payload() | {"skills": [_skill(body=body)]}
        )


def test_cli_kind_admits_any_harness_the_registration_pattern_does() -> None:
    """Local-agents 12 item 6: which runtime serves a kind is the Runner's choice."""

    payload = valid_context_payload()

    assert WorkerRuntimeContext.model_validate({**payload, "cli_kind": "gemini_cli"}).cli_kind == (
        "gemini_cli"
    )
    with pytest.raises(ValidationError):
        WorkerRuntimeContext.model_validate({**payload, "cli_kind": "Gemini-CLI"})


@pytest.mark.parametrize(
    "acp_command",
    [[], [""], ["/usr/bin/gemini", *["--flag"] * 32], ["/usr/bin/gemini", "x" * 1025]],
)
def test_an_acp_command_out_of_bounds_is_refused(acp_command: list[str]) -> None:
    with pytest.raises(ValidationError):
        WorkerRuntimeContext.model_validate(valid_context_payload() | {"acp_command": acp_command})
