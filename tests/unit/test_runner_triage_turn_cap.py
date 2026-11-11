"""A triage turn's one completion stops at its Reserve (QTS-2455, Spec: chat intake rev 2).

The proxy refuses only a call that starts with the Reserve already spent, so the cap on
the completion itself is what keeps a Listener's ``token_cap`` from being overrun. The
turn reports what it spent, so a firing's second turn is given only what is left
(QTS-2462).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agentic_runner.llm_proxy import CredentialSlot, LlmProxy, SlotStore
from agentic_runner.triage_activities import run_triage_turn

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")


class _Proxy:
    @contextlib.asynccontextmanager
    async def attempt(self, **_admitted: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(
            attempt_id="x", base_url="http://proxy.test/a/x/v1", token="attempt-bearer"
        )

    def reserve_spent(self, _attempt_id: str) -> int:
        return 0


async def _sent_body(reserve_max_tokens: int | None) -> dict[str, Any]:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "own"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await run_triage_turn(
            _Proxy(),  # type: ignore[arg-type]
            model="gpt-5-mini",
            timeout_seconds=5,
            prompt="triage this",
            directive_id="triage:1",
            contract_id=None,
            agent_id=None,
            reserve_max_tokens=reserve_max_tokens,
            client=client,
        )
    [body] = bodies
    return body


@pytest.mark.asyncio
async def test_a_turn_with_a_reserve_asks_the_provider_for_at_most_that_many_tokens() -> None:
    body = await _sent_body(500)

    assert body["max_completion_tokens"] == 500
    assert "max_tokens" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve_max_tokens", [None, 0])
async def test_a_turn_without_a_reserve_sends_no_cap(reserve_max_tokens: int | None) -> None:
    body = await _sent_body(reserve_max_tokens)

    assert "max_completion_tokens" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "raw_text"), [("own", "own"), (None, None)], ids=["answer", "malformed"]
)
async def test_a_turn_reports_what_the_proxy_counted_against_its_reserve(
    content: str | None, raw_text: str | None
) -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async def always_valid(_: CredentialSlot) -> bool:
        return True

    slots = SlotStore(probe=always_valid)
    await slots.put(
        CONTRACT,
        CredentialSlot(
            reference="contract_llm_key",
            key_id="key-a",
            provider_name="openrouter",
            base_url="https://provider.test/v1",
            value="sk-first",
            runtime_kind="codex_cli",
        ),
    )
    async with LlmProxy(
        slots=slots, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ) as proxy:
        turn = await run_triage_turn(
            proxy,
            model="gpt-5-mini",
            timeout_seconds=5,
            prompt="triage this",
            directive_id="triage:1",
            contract_id=CONTRACT,
            agent_id=None,
            reserve_max_tokens=500,
        )

    assert turn.raw_text == raw_text
    assert turn.spent_tokens == 120
