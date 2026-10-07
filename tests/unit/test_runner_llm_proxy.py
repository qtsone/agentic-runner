"""The relocated LLM proxy, on the Runner (PRD issue 43).

Exercised as it actually runs: a real loopback server, a real HTTP client speaking to it,
and a fake provider behind ``httpx.MockTransport`` so every assertion is about what the
proxy decided rather than about a mock's call list.

What each group pins is the issue's own list -- per-request slot resolution, the
probe-gated swap, the two ceilings and their typed refusal, the per-attempt bearer, and
the outbox's at-least-once delivery.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from agentic_runner.llm_proxy import (
    Ceilings,
    CeilingStore,
    CredentialSlot,
    LlmProxy,
    SlotStore,
    UsageOutbox,
    attempt_env,
    models_list_probe,
)
from agentic_runner.tiny_http import HttpRequest, read_request, write_json
from agentic_runner_contracts.llm_usage import ModelPrice
from agentic_runner_contracts.runner_registration import SlotProbe

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_CONTRACT = UUID("22222222-2222-4222-8222-222222222222")
AGENT = UUID("33333333-3333-4333-8333-333333333333")
WORK_RECORD = UUID("44444444-4444-4444-8444-444444444444")
DIRECTIVE = "wr-1:d1"

PRICES = {
    "openrouter/gpt-5": ModelPrice(
        prompt_usd_per_token=Decimal("0.000002"),
        completion_usd_per_token=Decimal("0.00001"),
    )
}


def slot(value: str, *, key_id: str = "key-a") -> CredentialSlot:
    return CredentialSlot(
        reference="contract_llm_key",
        key_id=key_id,
        provider_name="openrouter",
        base_url="https://provider.test/v1",
        value=value,
        runtime_kind="codex_cli",
    )


class FakeProvider:
    """One upstream, remembering only what the proxy is allowed to have sent it."""

    def __init__(self, *, usage: Mapping[str, Any] | None = None) -> None:
        self.keys_seen: list[str] = []
        self.headers_seen: list[Mapping[str, str]] = []
        self.stream_options: list[Any] = []
        self.calls = 0
        self.usage = dict(usage or {"prompt_tokens": 100, "completion_tokens": 20})

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.keys_seen.append(request.headers.get("authorization", ""))
            self.headers_seen.append(dict(request.headers))
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": []})
            self.calls += 1
            body = json.loads(request.content or b"{}")
            self.stream_options.append(body.get("stream_options"))
            if body.get("stream"):
                frames = (
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b'data: {"choices":[],"usage":' + json.dumps(self.usage).encode() + b"}\n\n"
                    b"data: [DONE]\n\n"
                )
                return httpx.Response(200, content=frames)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                    "usage": self.usage,
                },
            )

        return httpx.MockTransport(handle)


@pytest_asyncio.fixture
async def provider() -> FakeProvider:
    return FakeProvider()


@pytest_asyncio.fixture
async def slots() -> SlotStore:
    async def always_valid(_: CredentialSlot) -> bool:
        return True

    store = SlotStore(probe=always_valid)
    await store.put(CONTRACT, slot("sk-first"))
    return store


@pytest_asyncio.fixture
async def proxy(provider: FakeProvider, slots: SlotStore) -> AsyncIterator[LlmProxy]:
    async with LlmProxy(
        slots=slots,
        prices=PRICES,
        client=httpx.AsyncClient(transport=provider.transport()),
    ) as running:
        yield running


async def complete(
    handle: Any, *, token: str | None = None, stream: bool = False
) -> httpx.Response:
    bearer = token if token is not None else handle.token
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{handle.base_url}/v1/chat/completions",
            json={
                "model": "gpt-5",
                "messages": [{"role": "user", "content": "go"}],
                "stream": stream,
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )


def open_attempt(proxy: LlmProxy, *, contract_id: UUID | None = CONTRACT) -> Any:
    return proxy.attempt(
        directive_id=DIRECTIVE,
        contract_id=contract_id,
        agent_id=AGENT,
        work_record_id=WORK_RECORD,
    )


# ------------------------------------------------------------- per-request resolution


@pytest.mark.asyncio
async def test_each_request_resolves_the_slot_at_the_call_not_at_directive_start(
    proxy: LlmProxy, provider: FakeProvider, slots: SlotStore
) -> None:
    """22 A8: one Directive, two calls, a replacement in between — the second uses it.

    Resolving at Directive start would strand a running Directive on a key the funder has
    already revoked at the provider, which is the whole reason the lookup is per request.
    """

    async with open_attempt(proxy) as handle:
        assert (await complete(handle)).status_code == 200
        await slots.put(CONTRACT, slot("sk-second", key_id="key-b"))
        assert (await complete(handle)).status_code == 200

    assert provider.keys_seen[-2:] == ["Bearer sk-first", "Bearer sk-second"]


@pytest.mark.asyncio
async def test_a_value_that_fails_the_probe_is_not_swapped_in(provider: FakeProvider) -> None:
    """22 A10: the old value keeps serving, and the failure shows on the slot fields."""

    rejected: list[str] = []

    async def probe(candidate: CredentialSlot) -> bool:
        if candidate.value == "sk-dead":
            rejected.append(candidate.value)
            return False
        return True

    slots = SlotStore(probe=probe)
    await slots.put(CONTRACT, slot("sk-first"))

    assert await slots.put(CONTRACT, slot("sk-dead", key_id="key-dead")) is SlotProbe.INVALID

    async with (
        LlmProxy(slots=slots, client=httpx.AsyncClient(transport=provider.transport())) as running,
        open_attempt(running) as handle,
    ):
        assert (await complete(handle)).status_code == 200

    assert rejected == ["sk-dead"]
    assert provider.keys_seen[-1] == "Bearer sk-first"
    [status] = slots.statuses()
    assert (status.present, status.key_id, status.probe) == (True, "key-a", SlotProbe.INVALID)


@pytest.mark.asyncio
async def test_a_leaf_contract_resolves_the_funding_contracts_slot(
    provider: FakeProvider,
) -> None:
    """22 A6: the mapping is issue 34's data; the proxy only follows it."""

    async def always_valid(_: CredentialSlot) -> bool:
        return True

    slots = SlotStore(probe=always_valid)
    await slots.put(ACCOUNT_CONTRACT, slot("sk-account"))
    slots.fund_from(CONTRACT, ACCOUNT_CONTRACT)

    async with (
        LlmProxy(slots=slots, client=httpx.AsyncClient(transport=provider.transport())) as running,
        open_attempt(running) as handle,
    ):
        assert (await complete(handle)).status_code == 200

    assert provider.keys_seen[-1] == "Bearer sk-account"


@pytest.mark.asyncio
async def test_a_contract_with_no_slot_is_refused_before_any_provider_call(
    provider: FakeProvider, slots: SlotStore
) -> None:
    events: list[tuple[str, Mapping[str, object]]] = []

    async def evidence(source: str, payload: Mapping[str, object]) -> None:
        events.append((source, payload))

    async with (
        LlmProxy(
            slots=slots,
            evidence=evidence,
            client=httpx.AsyncClient(transport=provider.transport()),
        ) as running,
        open_attempt(running, contract_id=uuid4()) as handle,
    ):
        response = await complete(handle)

    assert response.status_code == 503
    assert provider.calls == 0
    [(source, payload)] = events
    assert source == "llm_call_refused"
    assert payload["reason"] == "slot_missing"
    # Ids only: nothing about the request, nothing about a credential.
    assert set(payload) == {"reason", "directive_id", "contract_id", "agent_id"}


# ------------------------------------------------------------------------- ceilings


@pytest.mark.asyncio
async def test_a_call_over_the_contract_ceiling_is_refused_with_the_typed_error(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    proxy.ceilings.push(CONTRACT, Ceilings(contract_limit=1_000, contract_used=1_000))

    async with open_attempt(proxy) as handle:
        response = await complete(handle)

    assert response.status_code == 402
    assert response.json()["error"] == {
        "message": "contract monthly token ceiling exhausted: 1000 of 1000 tokens spent",
        "type": "ceiling_exhausted",
        "ceiling": "contract",
        "limit": 1_000,
        "used": 1_000,
    }
    # A refused call costs nothing: the provider is never reached, and no model is
    # quietly swapped for a cheaper one to fit under the ceiling (map 12 B5).
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_the_organisation_ceiling_ignores_a_user_funded_contract(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """Map 12 B3: a user-funded Contract spends the user's own key, not the org's budget."""

    proxy.ceilings.push(
        CONTRACT,
        Ceilings(organisation_limit=10, organisation_used=10, org_funded=False),
    )
    async with open_attempt(proxy) as handle:
        assert (await complete(handle)).status_code == 200

    proxy.ceilings.push(
        CONTRACT,
        Ceilings(organisation_limit=10, organisation_used=10, org_funded=True),
    )
    async with open_attempt(proxy) as handle:
        refused = await complete(handle)

    assert refused.status_code == 402
    assert refused.json()["error"]["ceiling"] == "organisation"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_ceiling_raised_between_two_calls_admits_the_second(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    proxy.ceilings.push(CONTRACT, Ceilings(contract_limit=100, contract_used=100))

    async with open_attempt(proxy) as handle:
        assert (await complete(handle)).status_code == 402
        proxy.ceilings.push(CONTRACT, Ceilings(contract_limit=5_000, contract_used=100))
        assert (await complete(handle)).status_code == 200

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_metered_call_spends_against_the_pushed_ceiling(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """The Runner adds what it meters to the control plane's figure, so a Contract cannot
    outrun its ceiling between two pushes."""

    proxy.ceilings.push(CONTRACT, Ceilings(contract_limit=120, contract_used=0))

    async with open_attempt(proxy) as handle:
        assert (await complete(handle)).status_code == 200  # spends 100 + 20
        second = await complete(handle)

    assert second.status_code == 402
    assert second.json()["error"]["used"] == 120
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_the_learning_reserve_caps_one_attempt_on_top_of_the_ceilings(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """PRD issue 55, map 12 B6: a Learning Directive's attempt is refused once its own
    spend reaches the reserve -- with no Contract ceiling in play -- and the next attempt
    starts from zero."""

    async with proxy.attempt(
        directive_id=DIRECTIVE, contract_id=CONTRACT, agent_id=AGENT, reserve_max_tokens=120
    ) as handle:
        assert (await complete(handle)).status_code == 200  # spends 100 + 20
        refused = await complete(handle)
    async with open_attempt(proxy) as ordinary:
        assert (await complete(ordinary)).status_code == 200

    assert refused.status_code == 402
    assert refused.json()["error"]["type"] == "reserve_exhausted"
    assert provider.calls == 2


# -------------------------------------------------------------------- the bearer


@pytest.mark.asyncio
async def test_a_request_without_a_bearer_is_refused_before_any_provider_call(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    async with open_attempt(proxy) as handle, httpx.AsyncClient() as client:
        response = await client.post(
            f"{handle.base_url}/v1/chat/completions", json={"model": "gpt-5"}
        )

    assert response.status_code == 401
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_another_attempts_bearer_does_not_open_this_attempt(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """The path segment is what makes this a refusal rather than a mis-attribution: the
    other token is live, and still opens nothing here."""

    async with open_attempt(proxy) as mine, open_attempt(proxy) as theirs:
        response = await complete(mine, token=theirs.token)

    assert response.status_code == 401
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_an_attempts_bearer_dies_with_the_attempt(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    async with open_attempt(proxy) as handle:
        assert (await complete(handle)).status_code == 200
    after = await complete(handle)

    assert after.status_code == 401
    assert provider.calls == 1


def test_the_attempt_environment_carries_a_url_and_the_attempt_bearer_only() -> None:
    """ADR-0011 §9: the subprocess gets the proxy, never a provider key."""

    assert attempt_env("claude_code", base_url="http://127.0.0.1:9/a/x", token="t") == {
        # Claude Code appends `/v1/messages` to this.
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/a/x",
        "ANTHROPIC_AUTH_TOKEN": "t",
    }
    assert attempt_env("codex_cli", base_url="http://127.0.0.1:9/a/x", token="t") == {
        "OPENAI_BASE_URL": "http://127.0.0.1:9/a/x/v1",
        "OPENAI_API_KEY": "t",
    }


# ---------------------------------------------------------------- Usage Records out


@pytest.mark.asyncio
async def test_a_call_writes_one_usage_record_with_every_key_of_issue_13(
    proxy: LlmProxy,
) -> None:
    async with open_attempt(proxy) as handle:
        await complete(handle)

    [record] = proxy.outbox.pending()
    assert record.directive_id == DIRECTIVE
    assert record.sequence == 0
    assert record.contract_id == CONTRACT
    assert record.agent_id == AGENT
    assert record.work_record_id == WORK_RECORD
    assert record.provider_name == "openrouter"
    assert record.model_name == "gpt-5"
    assert (record.prompt_tokens, record.completion_tokens) == (100, 20)
    assert record.applied_prompt_price_usd == Decimal("0.000002")
    assert record.applied_completion_price_usd == Decimal("0.00001")
    assert record.cost_usd == Decimal("0.0004")
    assert record.key == f"{DIRECTIVE}:0"


@pytest.mark.asyncio
async def test_a_streamed_call_meters_the_usage_block_off_the_stream(
    proxy: LlmProxy,
) -> None:
    """Without this a streamed Directive would meter zero and spend nothing against its
    ceiling — which is how a Contract would stream past a ceiling it has exhausted."""

    async with open_attempt(proxy) as handle:
        response = await complete(handle, stream=True)

    assert b"data: [DONE]" in response.content
    [record] = proxy.outbox.pending()
    assert (record.prompt_tokens, record.completion_tokens) == (100, 20)


def test_the_outbox_keeps_a_record_until_its_key_is_acknowledged() -> None:
    """At-least-once: a dropped acknowledgement costs a re-send, never a lost row."""

    outbox = UsageOutbox()
    for _ in range(3):
        outbox.record(_record(outbox))

    batch = outbox.pending()
    assert [record.key for record in batch] == [f"{DIRECTIVE}:{n}" for n in range(3)]

    # The ack for the middle record never arrives.
    assert outbox.acknowledge([batch[0].key, batch[2].key]) == 2
    assert [record.key for record in outbox.pending()] == [f"{DIRECTIVE}:1"]

    # Re-sent under the same key, so the ledger's de-duplication has something to bite on.
    assert outbox.pending()[0].request_id == f"runner:{DIRECTIVE}:1"
    assert outbox.acknowledge([f"{DIRECTIVE}:1"]) == 1
    assert outbox.pending() == []


def test_a_ceiling_store_with_no_pushed_value_refuses_nothing() -> None:
    """The control plane is the authority on limits; inventing one here would refuse work
    nobody capped."""

    CeilingStore().authorize(CONTRACT)


def _record(outbox: UsageOutbox) -> Any:
    from agentic_runner_contracts.llm_usage import UsageRecord

    return UsageRecord(
        directive_id=DIRECTIVE,
        sequence=outbox.next_sequence(DIRECTIVE),
        provider_name="openrouter",
        model_name="gpt-5",
        prompt_tokens=1,
        completion_tokens=1,
    )


@pytest.mark.asyncio
async def test_a_swap_into_service_and_a_refused_one_are_both_evidence_on_the_contract() -> None:
    """Issue 43: the swap is an Evidence Event on the Contract, the refusal names why.

    Ids, the key label and the probe result — never the value, which is the whole reason
    the funder can read this trail at all.
    """

    events: list[tuple[str, Mapping[str, object]]] = []

    async def evidence(source: str, payload: Mapping[str, object]) -> None:
        events.append((source, payload))

    async def probe(candidate: CredentialSlot) -> bool:
        return candidate.value != "sk-dead"

    slots = SlotStore(probe=probe)
    await slots.put(CONTRACT, slot("sk-first"), evidence=evidence)
    await slots.put(CONTRACT, slot("sk-dead", key_id="key-dead"), evidence=evidence)

    assert [source for source, _ in events] == ["llm_slot_swapped", "llm_slot_refused"]
    swapped, refused = (payload for _, payload in events)
    assert swapped == {
        "contract_id": str(CONTRACT),
        "runtime_kind": "codex_cli",
        "key_id": "key-a",
        "reference": "contract_llm_key",
        "probe": "valid",
    }
    assert refused["reason"] == "probe_invalid"
    assert refused["probe"] == "invalid"
    assert "sk-dead" not in json.dumps(dict(refused))


def test_the_proxy_refuses_a_bind_anyone_but_this_process_could_reach() -> None:
    """17 A11: loopback TCP because the CLIs need a URL — not because it is a service."""

    with pytest.raises(ValueError, match="loopback only"):
        LlmProxy(slots=SlotStore(), host="0.0.0.0")  # noqa: S104 - the refusal under test


@pytest.mark.asyncio
async def test_the_anthropic_wire_goes_through_the_same_attempt_and_meters_the_same_row(
    slots: SlotStore,
) -> None:
    """Claude Code is one of the two harnesses the platform ships, and it speaks
    ``/v1/messages`` with Anthropic's own usage spelling. Two wires, one Usage Record —
    otherwise that harness would still need a provider key of its own (ADR-0011 §9)."""

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 90,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 20,
                },
            },
        )

    async with (
        LlmProxy(
            slots=slots,
            prices=PRICES,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as running,
        open_attempt(running) as handle_,
    ):
        env = attempt_env("claude_code", base_url=handle_.base_url, token=handle_.token)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
                json={"model": "gpt-5", "messages": [{"role": "user", "content": "go"}]},
                headers={"Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"},
            )

    assert response.status_code == 200
    assert seen == ["/v1/messages"]
    [record] = running.outbox.pending()
    # Cache writes are new input the model had to encode, so they count as prompt.
    assert (record.prompt_tokens, record.completion_tokens, record.cached_tokens) == (100, 20, 4)


# ------------------------------------------------------------------ the upstream wire


ANTHROPIC_STREAM = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000,'
    b'"output_tokens":1}}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","usage":{"output_tokens":20}}\n\n'
)


async def message(handle: Any, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{handle.base_url}/v1/messages",
            json={
                "model": "gpt-5",
                "messages": [{"role": "user", "content": "go"}],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {handle.token}", **(headers or {})},
        )


@pytest.mark.asyncio
async def test_a_streamed_anthropic_call_meters_the_counts_split_across_two_frames(
    slots: SlotStore,
) -> None:
    """Claude Code streams by default, and Anthropic splits one call's counts: the input
    tokens ride `message_start`, the output tokens `message_delta`. Keeping only the last
    block seen would meter every streamed Directive with no prompt tokens -- the dominant
    term -- so the ledger row would be wrong and the per-call ceiling unenforceable."""

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ANTHROPIC_STREAM)

    async with LlmProxy(
        slots=slots, prices=PRICES, client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as running:
        running.ceilings.push(CONTRACT, Ceilings(contract_limit=1_500))
        async with open_attempt(running) as attempt:
            statuses = [(await message(attempt)).status_code for _ in range(3)]

    # 1020 tokens a call: the third is over the ceiling the first two spent.
    assert statuses == [200, 200, 402]
    first = running.outbox.pending()[0]
    assert (first.prompt_tokens, first.completion_tokens) == (1_000, 20)


@pytest.mark.asyncio
async def test_a_streamed_openai_call_asks_for_the_usage_block_it_meters(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """An OpenAI-compatible endpoint omits usage from a stream unless it is asked for."""

    async with open_attempt(proxy) as handle:
        assert (await complete(handle, stream=True)).status_code == 200

    assert provider.stream_options == [{"include_usage": True}]


@pytest.mark.asyncio
async def test_the_harnesss_own_headers_reach_the_provider(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    """`anthropic-beta` is how Claude Code asks for the features it is built against; a
    proxy that rebuilt the header set would answer a different call than the Agent made.
    The attempt's own bearer is the one header replaced -- by the slot's credential."""

    async with open_attempt(proxy) as handle:
        await message(handle, headers={"anthropic-beta": "context-1m-2025-08-07"})

    sent = provider.headers_seen[-1]
    assert sent["anthropic-beta"] == "context-1m-2025-08-07"
    assert sent["authorization"] == "Bearer sk-first"


def test_a_setup_token_bearer_still_carries_the_anthropic_version() -> None:
    """A `claude setup-token` bearer is the same wire as an `x-api-key`: without the
    version header the provider refuses it, so the models-list probe would mark a good
    value permanently invalid (22 A10)."""

    bearer = CredentialSlot(
        reference="contract_llm_key",
        key_id="key-a",
        provider_name="anthropic",
        base_url="https://api.anthropic.test/v1",
        value="sk-ant-oat",
        runtime_kind="claude_code",
    )

    assert bearer.headers() == {
        "Authorization": "Bearer sk-ant-oat",
        "anthropic-version": "2023-06-01",
    }


@pytest.mark.asyncio
async def test_the_models_route_relays_the_providers_own_list(
    proxy: LlmProxy, provider: FakeProvider
) -> None:
    async with open_attempt(proxy) as handle, httpx.AsyncClient() as client:
        response = await client.get(
            f"{handle.base_url}/v1/models",
            headers={"Authorization": f"Bearer {handle.token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


@pytest.mark.asyncio
async def test_an_unknown_route_is_refused_before_the_slot_is_even_resolved(
    provider: FakeProvider, slots: SlotStore
) -> None:
    """A mistyped path is not a funding failure, so it writes no Evidence."""

    events: list[tuple[str, Mapping[str, object]]] = []

    async def evidence(source: str, payload: Mapping[str, object]) -> None:
        events.append((source, payload))

    async with (
        LlmProxy(
            slots=slots,
            evidence=evidence,
            client=httpx.AsyncClient(transport=provider.transport()),
        ) as running,
        open_attempt(running, contract_id=uuid4()) as handle,
        httpx.AsyncClient() as client,
    ):
        response = await client.post(
            f"{handle.base_url}/v1/nonsense",
            json={},
            headers={"Authorization": f"Bearer {handle.token}"},
        )

    assert response.status_code == 404
    assert events == []


@pytest.mark.asyncio
async def test_the_models_list_probe_answers_off_a_real_provider_response() -> None:
    """The probe as production runs it: its own client, a real GET, a real answer.

    Every other probe test injects a `SlotProber`, which proves the swap semantics and
    nothing about the mechanism that decides them.
    """

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await read_request(reader)
        assert isinstance(request, HttpRequest)
        live = request.headers.get("x-api-key") == "sk-live"
        await write_json(writer, 200 if live else 401, {"object": "list", "data": []})
        writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]

    def anthropic(value: str) -> CredentialSlot:
        return CredentialSlot(
            reference="contract_llm_key",
            key_id="key-a",
            provider_name="anthropic",
            base_url=f"http://127.0.0.1:{port}/v1",
            value=value,
            runtime_kind="claude_code",
            auth_style="x-api-key",
        )

    async with server:
        assert await models_list_probe(anthropic("sk-live")) is True
        assert await models_list_probe(anthropic("sk-dead")) is False

    # Nothing listening now: a network fault answers *no*, so an unverified value never
    # displaces the one already serving.
    assert await models_list_probe(anthropic("sk-live")) is False


@pytest.mark.asyncio
async def test_a_streamed_call_the_provider_refused_records_its_status(slots: SlotStore) -> None:
    """A refused stream meters no tokens, so without the status the row reads as a free
    successful call -- the same code the non-streaming branch already records."""

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":{"message":"slow down"}}')

    async with (
        LlmProxy(
            slots=slots, client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        ) as running,
        open_attempt(running) as attempt,
    ):
        assert (await message(attempt)).status_code == 429

    [record] = running.outbox.pending()
    assert (record.error_class, record.prompt_tokens) == ("http_429", None)
